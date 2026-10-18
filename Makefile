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
# archive.org collection (opensource = Community Texts)
IA_COLLECTION ?= opensource
# 0 = status skips the site vs archive.org search
IA_CHECK ?= 1
# Already downloaded PDFs moved in by `make onetime_seed`
SEED_DIR ?= ../pmccirc/import/documents

LIMIT_ARG := $(if $(LIMIT),--limit $(LIMIT))
RETRY_ARG := $(if $(filter 1,$(RETRY_FAILED)),--retry-failed)
SCOPE_ARG = --scope $(SCOPE) --window $(NEW_WINDOW)
RUN = mkdir -p $(LOGS); set -o pipefail; $(PY) $(SRC)/$(1) | tee $(LOGS)/$@.log

.PHONY: help install fetch fetch_full check_codes onetime_seed download link_wayback upload_archive \
        update_archive_metadata sync_hf_push hf_card import status \
        setup daily backfill backfill_archive backfill_wayback backfill_crawl require_setup

help:
	@echo "Usage: make <target> [VAR=value ...]   (see SPEC.md)"
	@echo ""
	@echo "Modes"
	@echo "  setup                    first time, locally: fetch_full onetime_seed download sync_hf_push hf_card upload_archive (no wayback)"
	@echo "  daily                    new circulars only: fetch download link_wayback upload_archive sync_hf_push (SCOPE=new)"
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
	@echo "  sync_hf_push             push LFS/punePMC PDFs to the HF dataset      [SCOPE=$(SCOPE)]"
	@echo "  hf_card                  write and push the HF dataset README"
	@echo "  import                   pull PDFs from the HF dataset into LFS/punePMC"
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

sync_hf_push:
	$(call RUN,sync_hf.py push $(SCOPE_ARG))

hf_card:
	$(call RUN,sync_hf.py card)

import:
	$(call RUN,sync_hf.py pull)

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
	$(MAKE) --no-print-directory upload_archive SCOPE=all || rc=1; \
	$(MAKE) --no-print-directory status || rc=1; \
	exit $$rc

# daily/backfill run on GitHub Actions; never let them do a first-time setup there
require_setup:
	@test -s $(STATE)/circulars.json || { echo ">>> $(STATE)/circulars.json is missing: run 'make setup' locally first"; exit 1; }

# daily touches only circulars first seen within NEW_WINDOW; old ones (PMC edits, removals, retries of old
# failures, metadata updates) are left to backfill.
daily: require_setup
	@rc=0; \
	$(MAKE) --no-print-directory fetch || rc=1; \
	$(MAKE) --no-print-directory download SCOPE=new || rc=1; \
	$(MAKE) --no-print-directory link_wayback SCOPE=new || rc=1; \
	$(MAKE) --no-print-directory upload_archive SCOPE=new || rc=1; \
	$(MAKE) --no-print-directory sync_hf_push SCOPE=new || rc=1; \
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
	$(MAKE) --no-print-directory sync_hf_push SCOPE=all || rc=1; \
	$(MAKE) --no-print-directory status || rc=1; \
	exit $$rc
