"""One-time: MOVE already downloaded PDFs from ../pmccirc into LFS and record them in downloads.json.

Usage: onetime_seed.py <pmccirc documents dir>      (default ../pmccirc/import/documents)

A pmccirc file <year>/<nid>.pdf is moved only if pmccirc downloaded it successfully, its URL is
the circular's current file_url, its size matches the API filesize and it is a PDF. For a PDF
shared by several circulars the first copy is moved; further copies are deleted from pmccirc
once their sha1 matches (download.py hardlinks the other doc_id paths). Everything that does not
match stays in pmccirc, which is never deleted.
"""

import datetime
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

from common import (
    group_by_fid,
    looks_like_pdf,
    pdf_path,
    read_records,
    rel_path,
    same_url,
    save_records,
    sha1_file,
    write_records,
)


def load_pmccirc(seed_dir):
    """{nid: pmccirc pdfs.json record} for successful downloads."""
    path = seed_dir / "pdfs.json"
    if not path.exists():
        raise SystemExit(f"{path} not found")
    ok = {}
    for r in json.loads(path.read_text()):
        if r.get("download_success"):
            ok[int(r["nid"])] = r
    return ok


def source_file(seed_dir, nid, prec):
    target = prec.get("target_path")  # e.g. documents/2024/9835.pdf, relative to seed_dir's parent
    if target and (seed_dir.parent / target).is_file():
        return seed_dir.parent / target
    found = sorted(seed_dir.glob(f"*/{nid}.pdf"))
    return found[0] if found else None


def check(rec, prec, src):
    """Reason the pmccirc file can't be used, or '' if it can."""
    if not prec:
        return "not downloaded by pmccirc"
    if not src:
        return "file missing in pmccirc"
    if not same_url(prec.get("url"), rec["file_url"]):
        return "url changed since pmccirc"
    if rec.get("filesize") and src.stat().st_size != rec["filesize"]:
        return "size differs from API filesize"
    if not looks_like_pdf(src):
        return "not a pdf"
    return ""


def move(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dest)  # a rename on the same disk


def main(seed_dir):
    if not seed_dir.is_dir():
        print(f"*** {seed_dir} not found, nothing to seed")
        return

    pmccirc = load_pmccirc(seed_dir)
    circulars = read_records("circulars")
    downloads = read_records("downloads")
    print(f"*** pmccirc: {len(pmccirc)} downloaded PDFs in {seed_dir}")

    moved, deduped, left = 0, 0, Counter()
    try:
        for fid, recs in group_by_fid(circulars).items():
            if downloads.get(fid, {}).get("status") in ("downloaded", "seeded"):
                continue

            first, first_sha1, first_src = None, None, None
            for rec in recs:  # a copy already moved by an interrupted run counts as the first one
                dest = pdf_path(rec["doc_id"])
                if dest.is_file() and (not rec.get("filesize") or dest.stat().st_size == rec["filesize"]) and looks_like_pdf(dest):
                    first, first_sha1, first_src = dest, sha1_file(dest), "LFS (earlier seed run)"
                    break

            for rec in recs:
                prec = pmccirc.get(rec["nid"])
                src = source_file(seed_dir, rec["nid"], prec or {})
                reason = check(rec, prec, src)
                if reason:
                    if src:
                        left[reason] += 1
                    continue
                if first is None:
                    dest = pdf_path(rec["doc_id"])
                    move(src, dest)
                    first, first_sha1, first_src = dest, sha1_file(dest), src
                    moved += 1
                    if moved % 500 == 0:
                        print(f"\tmoved {moved}")
                elif sha1_file(src) == first_sha1:
                    src.unlink()  # identical copy; download.py hardlinks this doc_id to `first`
                    deduped += 1
                else:
                    left["differs from the other copy of the same fid"] += 1

            if first is None:
                continue
            mtime = datetime.datetime.fromtimestamp(first.stat().st_mtime, tz=datetime.timezone.utc)
            downloads[fid] = {
                "fid": fid,
                "nids": [r["nid"] for r in recs],
                "url": recs[0]["file_url"],
                "paths": [rel_path(r["doc_id"]) for r in recs],
                "size": first.stat().st_size,
                "sha1": first_sha1,
                "last_modified": None,
                "etag": None,
                "download_time_utc": mtime.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
                "status": "seeded",
                "attempts": 0,
                "current": True,
                "seeded_from": str(first_src),
            }
            save_records("downloads", downloads)  # hardlinks for the other doc_ids and symlinks: download.py
    finally:
        write_records("downloads", downloads)

    print(f"*** Moved: {moved} PDFs (one per file id)")
    print(f"*** Deleted from pmccirc as identical copies of a moved PDF: {deduped}")
    print(f"*** Left in pmccirc: {sum(left.values())} {dict(left)}")
    print("*** Next: make download (hardlinks shared PDFs and downloads the rest)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../pmccirc/import/documents"))
