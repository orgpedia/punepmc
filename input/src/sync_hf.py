"""Sync LFS/punePMC with the Hugging Face dataset (files only, no parquet).

Usage: sync_hf.py pull|push|card [--scope new|all] [--window 7d]

push  uploads the PDFs present in LFS/punePMC/publications/circulars/pdfs (a partial folder on CI is fine;
      nothing is deleted on HF). Large pushes (the first one) use upload_large_folder, which is resumable.
      --scope new pushes only the PDFs of circulars first seen within --window (the daily run).
card  writes README.md (with `viewer: false`, so HF does not build a viewer or parquet files) and pushes it if changed.
"""

import argparse

from huggingface_hub import HfApi, hf_hub_download, snapshot_download

from common import (
    HF_PREFIX,
    IA_PREFIX,
    LFS_DIR,
    PDFS_DIR,
    SOURCE_PAGE,
    active,
    FORMATS,
    doc_path,
    file_ext,
    hf_settings,
    is_new,
    link_file,
    load_env,
    parse_window,
    read_records,
    rel_path,
)

ALLOW_PATTERNS = [f"{HF_PREFIX}/**"]
LARGE_PUSH = 1000  # files; above this use upload_large_folder
GITHUB_REPO = "https://github.com/orgpedia/punepmc"

CARD = f"""---
pretty_name: Pune Municipal Corporation (PMC) publications
language:
- mr
- en
tags:
- government
- india
- pune
- municipal-corporation
- marathi
- pdf
viewer: false
---

# punePMC: Pune Municipal Corporation publications

PDFs published by the Pune Municipal Corporation (पुणे महानगरपालिका), mirrored as plain files.
Most documents are scanned Marathi PDFs. This dataset holds the files only; there are no parquet or
metadata files here.

## Contents

| Path | Source |
|---|---|
| `{HF_PREFIX}/<YYYY>/<doc_id>.pdf` | circulars from {SOURCE_PAGE} |

A few circulars were posted by PMC as JPEG images or Word files; they are kept as they are, under the same
folder with their own extension (`.jpg`, `.png`, `.doc`, `.docx`).

## Naming

`doc_id = <dept_code>.<YYYY-Mon-DD>.<NN>`, e.g. `gad.2019-May-31.01`:

- `dept_code`: short code of the issuing PMC department (e.g. `gad` = General Administration)
- `YYYY-Mon-DD`: date of the circular
- `NN`: number of the circular among those of that department on that day (`01`, `02`, ...)

The same document is on archive.org as `{IA_PREFIX}<doc_id>`, e.g.
https://archive.org/details/{IA_PREFIX}gad.2019-May-31.01, and its PMC URL is captured in the Wayback Machine.

## Metadata and updates

The metadata (titles in English and Marathi, department, date, PMC node id, source URL, checksums,
Wayback and archive.org links) and the code that builds this dataset are at {GITHUB_REPO}
(`input/documents/publications/circulars/`). The dataset is updated daily. When PMC replaces the PDF of a
circular, the file is overwritten here and the previous version stays in the repository history.
"""


def pull(repo_id, token, args):
    print(f"*** Downloading {repo_id} -> {LFS_DIR}")
    snapshot_download(repo_id=repo_id, repo_type="dataset", local_dir=LFS_DIR, allow_patterns=ALLOW_PATTERNS, token=token)
    circulars, downloads, linked, missing = read_records("circulars"), read_records("downloads"), 0, []
    for rec in circulars.values():
        if not active(rec):
            continue
        ext = file_ext(downloads.get(rec["fid"]))
        if doc_path(rec["doc_id"], ext).exists():
            link_file(rec["doc_id"], ext)
            linked += 1
        else:
            missing.append(rec["doc_id"])
    print(f"*** Linked {linked} of {linked + len(missing)} files into input/documents")
    if missing:
        print(f"*** WARNING not on HF / not local: {len(missing)} {missing[:20]}")


def push(repo_id, token, args):
    if args.scope == "new":
        window = parse_window(args.window)
        downloads = read_records("downloads")
        paths = sorted({rel_path(r["doc_id"], file_ext(downloads.get(r["fid"])))
                        for r in read_records("circulars").values() if is_new(r, window)})
        paths = [p for p in paths if (LFS_DIR / p).is_file()]
        allow, files = paths, paths
    else:
        allow = ALLOW_PATTERNS
        files = [p for p in PDFS_DIR.rglob("*") if p.is_file() and p.suffix[1:] in FORMATS] if PDFS_DIR.exists() else []
    if not files:
        print(f"*** No files to push (scope={args.scope})")
        return
    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="dataset", exist_ok=True)
    print(f"*** Uploading {len(files)} local files -> {repo_id} (scope={args.scope}; unchanged files are skipped)")
    if len(files) > LARGE_PUSH:
        api.upload_large_folder(repo_id=repo_id, repo_type="dataset", folder_path=LFS_DIR, allow_patterns=allow)
        print("*** Done (upload_large_folder)")
    else:
        commit = api.upload_folder(repo_id=repo_id, repo_type="dataset", folder_path=LFS_DIR,
                                   allow_patterns=allow, commit_message="circulars: sync pdfs")
        print(f"*** Done: {commit}")


def card(repo_id, token, args):
    LFS_DIR.mkdir(parents=True, exist_ok=True)
    (LFS_DIR / "README.md").write_text(CARD)
    try:
        remote = open(hf_hub_download(repo_id=repo_id, repo_type="dataset", filename="README.md", token=token)).read()
    except Exception:
        remote = None
    if remote == CARD:
        print("*** README.md unchanged")
        return
    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="dataset", exist_ok=True)
    api.upload_file(repo_id=repo_id, repo_type="dataset", path_or_fileobj=CARD.encode(), path_in_repo="README.md",
                    commit_message="dataset card")
    print("*** README.md pushed")


def main(args):
    load_env()
    repo_id, token = hf_settings()
    if args.action in ("push", "card") and not token:
        raise SystemExit("Missing environment variable: HF_TOKEN (set it in .env or as a secret)")
    {"pull": pull, "push": push, "card": card}[args.action](repo_id, token, args)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=("pull", "push", "card"))
    p.add_argument("--scope", choices=("new", "all"), default="all", help="push: which PDFs")
    p.add_argument("--window", default="7d", help="what --scope new means")
    main(p.parse_args())
