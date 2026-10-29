.DEFAULT_GOAL := help
SHELL := /bin/bash

SRC  := input/src
LOGS := input/logs
STATE := input/documents/publications/circulars
PY   := uv run python -u

# backfill: what to work on (archive | wayback | crawl)
WHAT ?=
# Max items per run for link_wayback / upload_archive / backfill (empty = no limit)
LIMIT ?=
# new = only circulars first seen within NEW_WINDOW (daily); all = the whole backlog
SCOPE ?= all
NEW_WINDOW ?= 7d
DOWNLOAD_DELAY ?= 1
# 1 = also retry downloads that failed with 404 / not a PDF
RETRY_FAILED ?= 0
# Wayback SPN2: reuse an existing capture newer than this instead of re-capturing
WAYBACK_REUSE ?= 30d
# setup uploads only this many circulars to archive.org (oldest first; empty = all); the rest via `backfill WHAT=archive`
SETUP_ARCHIVE_LIMIT ?= 55
# archive.org collection (opensource = Community Texts)
IA_COLLECTION ?= opensource
# 0 = status skips the site vs archive.org search
IA_CHECK ?= 1
# 0 = daily skips Wayback (GitHub Actions: SPN2 blocks GitHub's IPs with HTTP 429, so Wayback runs locally)
WAYBACK ?= 1
# Already downloaded PDFs moved in by `make onetime_seed`
SEED_DIR ?= ../pmccirc/import/documents

# .env (local runs) supplies HF_TOKEN etc.; on GitHub Actions they come from secrets
ifneq (,$(wildcard .env))
include .env
endif
HF_REPO := $(or $(HF_REPO_ID),Orgpedia/punePMC)
HF_DIR := publications/circulars/pdfs
ENV_FILE := $(if $(wildcard .env),--env-file .env)

LIMIT_ARG := $(if $(LIMIT),--limit $(LIMIT))
RETRY_ARG := $(if $(filter 1,$(RETRY_FAILED)),--retry-failed)
SCOPE_ARG = --scope $(SCOPE) --window $(NEW_WINDOW)
RUN = mkdir -p $(LOGS); set -o pipefail; $(PY) $(SRC)/$(1) 2>&1 | tee $(LOGS)/$@.log
HF = mkdir -p $(LOGS); set -o pipefail; uv run $(ENV_FILE) hf $(1) 2>&1 | tee $(LOGS)/$@.log

.PHONY: help install fetch fetch_full check_codes onetime_seed download link_wayback upload_archive \
        update_archive_metadata sync_hf_push sync_hf_pull hf_card status \
        setup daily backfill backfill_archive backfill_wayback backfill_crawl require_setup git_pull

help:
	@echo "Usage: make <target> [VAR=value ...]   (see SPEC.md)"
	@echo ""
	@echo "Modes"
	@echo "  setup                    first time, locally: fetch_full onetime_seed download sync_hf_push hf_card upload_archive (no wayback)"
	@echo "                           uploads only the first SETUP_ARCHIVE_LIMIT=$(SETUP_ARCHIVE_LIMIT) circulars to archive.org"
	@echo "  daily                    new circulars only: fetch download link_wayback upload_archive sync_hf_push (SCOPE=new)"
	@echo "                           WAYBACK=0 skips link_wayback (used on GitHub Actions)"
	@echo "  backfill WHAT=archive    upload the archive.org backlog              [LIMIT=$(LIMIT)]"
	@echo "  backfill WHAT=wayback    capture the Wayback backlog                 [LIMIT=$(LIMIT)]"
	@echo "  backfill WHAT=crawl      re-crawl the whole site, fetch missing/changed PDFs, upload new ones"
	@echo ""
	@echo "Steps"
	@echo "  install                  set up the uv environment"
	@echo "  fetch                    add new circulars (nid above the highest known); never re-reads old ones"
	@echo "  fetch_full               re-read all circulars: update changed, mark removed, refresh departments.json"
	@echo "  check_codes              fail if circulars are held for a missing department code"
	@echo "  onetime_seed             one-time: move matching PDFs from $(SEED_DIR)"
	@echo "  download                 download PDFs not yet downloaded             [SCOPE=$(SCOPE) RETRY_FAILED=$(RETRY_FAILED)]"
	@echo "  link_wayback             Wayback SPN2 captures                        [SCOPE=$(SCOPE) LIMIT=$(LIMIT) WAYBACK_REUSE=$(WAYBACK_REUSE)]"
	@echo "  upload_archive           upload to archive.org                        [SCOPE=$(SCOPE) LIMIT=$(LIMIT) IA_COLLECTION=$(IA_COLLECTION)]"
	@echo "  update_archive_metadata  re-apply changed metadata to archive.org items"
	@echo "  sync_hf_push             hf upload: LFS/punePMC/$(HF_DIR) -> $(HF_REPO) (unchanged files skipped)"
	@echo "  hf_card                  hf upload: input/hf/README.md -> $(HF_REPO)"
	@echo "  sync_hf_pull             git pull, hf download: $(HF_REPO) -> LFS/punePMC, then relink (e.g. after a fresh clone)"
	@echo "  git_pull                 git pull --rebase the state files (run first by sync_hf_pull, daily, backfill; skipped on CI)"
	@echo "  status                   counts + site vs archive.org check           [IA_CHECK=$(IA_CHECK)]"
	@echo ""
	@echo "Credentials come from .env (see env.example) or the environment (GitHub Actions secrets)."

install: pyproject.toml
	uv sync

# ---------------------------------------------------------------- steps

fetch:
	$(call RUN,fetch.py)

fetch_full:
	$(call RUN,fetch.py --full)

check_codes:
	$(call RUN,status.py --no-ia --check-codes)

onetime_seed:
	$(call RUN,onetime_seed.py $(SEED_DIR))

download:
	$(call RUN,download.py $(SCOPE_ARG) --delay $(DOWNLOAD_DELAY) $(RETRY_ARG))

link_wayback:
	$(call RUN,link_wayback.py $(SCOPE_ARG) --reuse $(WAYBACK_REUSE) $(LIMIT_ARG))

upload_archive:
	$(call RUN,upload_archive.py $(SCOPE_ARG) --collection $(IA_COLLECTION) $(LIMIT_ARG))

update_archive_metadata:
	$(call RUN,upload_archive.py --update-metadata --collection $(IA_COLLECTION))

# Pushes whatever is in LFS (on CI: only this run's new files); unchanged files are skipped, nothing is deleted on HF.
# Large folders are split into several commits; re-run to resume an interrupted push.
sync_hf_push:
	@if [ -d LFS/punePMC/$(HF_DIR) ]; then \
	  $(call HF,upload $(HF_REPO) LFS/punePMC/$(HF_DIR) $(HF_DIR) --repo-type dataset --exclude "*.part" --exclude ".DS_Store" --commit-message "circulars: sync files"); \
	else echo "*** no files in LFS/punePMC/$(HF_DIR), nothing to push"; fi

hf_card:
	$(call HF,upload $(HF_REPO) input/hf/README.md README.md --repo-type dataset --commit-message "dataset card")

sync_hf_pull: git_pull
	$(call HF,download $(HF_REPO) --repo-type dataset --local-dir LFS/punePMC --include "$(HF_DIR)/**")
	$(MAKE) --no-print-directory download

status:
	$(call RUN,status.py $(if $(filter 0,$(IA_CHECK)),--no-ia))

# ---------------------------------------------------------------- modes
# setup stops if department codes are missing (doc_ids would freeze wrongly); after that, and in the
# other modes, every step runs even if an earlier one failed, and the mode exits non-zero at the end.

setup: install
	$(MAKE) --no-print-directory fetch_full
	$(MAKE) --no-print-directory check_codes
	@rc=0; \
	$(MAKE) --no-print-directory onetime_seed || rc=1; \
	$(MAKE) --no-print-directory download || rc=1; \
	$(MAKE) --no-print-directory sync_hf_push || rc=1; \
	$(MAKE) --no-print-directory hf_card || rc=1; \
	$(MAKE) --no-print-directory upload_archive SCOPE=all LIMIT=$(SETUP_ARCHIVE_LIMIT) || rc=1; \
	$(MAKE) --no-print-directory status || rc=1; \
	exit $$rc

# GitHub Actions commits to the state files daily, so local runs start from the latest copy. --rebase replays unpushed
# local commits on top; it stops (before any step runs) on uncommitted changes, a conflict or untracked files in the way.
git_pull:
ifeq ($(GITHUB_ACTIONS),true)
	@true
else
	git pull --rebase
endif

# daily/backfill run on GitHub Actions; never let them do a first-time setup there
require_setup: git_pull
	@test -s $(STATE)/circulars.json || { echo ">>> $(STATE)/circulars.json is missing: run 'make setup' locally first"; exit 1; }

# daily touches only circulars first seen within NEW_WINDOW; old ones (PMC edits, removals, retries of old
# failures, metadata updates) are left to backfill.
daily: require_setup
	@rc=0; \
	$(MAKE) --no-print-directory fetch || rc=1; \
	$(MAKE) --no-print-directory download SCOPE=new || rc=1; \
	if [ "$(WAYBACK)" = 1 ]; then $(MAKE) --no-print-directory link_wayback SCOPE=new || rc=1; fi; \
	$(MAKE) --no-print-directory upload_archive SCOPE=new || rc=1; \
	$(MAKE) --no-print-directory sync_hf_push || rc=1; \
	$(MAKE) --no-print-directory status IA_CHECK=0 || rc=1; \
	exit $$rc

backfill:
	@case "$(WHAT)" in archive|wayback|crawl) ;; \
	  *) echo ">>> usage: make backfill WHAT=archive|wayback|crawl [LIMIT=n]"; exit 1 ;; esac
	@$(MAKE) --no-print-directory backfill_$(WHAT)

backfill_archive: require_setup
	@rc=0; \
	$(MAKE) --no-print-directory upload_archive SCOPE=all || rc=1; \
	$(MAKE) --no-print-directory status || rc=1; \
	exit $$rc

backfill_wayback: require_setup
	@rc=0; \
	$(MAKE) --no-print-directory link_wayback SCOPE=all || rc=1; \
	$(MAKE) --no-print-directory update_archive_metadata || rc=1; \
	$(MAKE) --no-print-directory status || rc=1; \
	exit $$rc

backfill_crawl: require_setup
	@rc=0; \
	$(MAKE) --no-print-directory fetch_full || rc=1; \
	$(MAKE) --no-print-directory download SCOPE=all || rc=1; \
	$(MAKE) --no-print-directory upload_archive SCOPE=all || rc=1; \
	$(MAKE) --no-print-directory update_archive_metadata || rc=1; \
	$(MAKE) --no-print-directory sync_hf_push || rc=1; \
	$(MAKE) --no-print-directory status || rc=1; \
	exit $$rc
