"""Download the current file of every active circular, once per file id (fid).

Usage: download.py [--scope new|all] [--window 7d] [--retry-failed] [--delay 1]

--scope new  only files of circulars first seen within --window (the daily run); all = every current file.

The file goes to LFS/punePMC/publications/circulars/pdfs/<YYYY>/<doc_id>.<ext>, where ext is its actual
type (pdf, or jpg / png / doc / docx for the few circulars PMC posted that way); other doc_ids that
share the fid get hardlinks to it. A replaced file (new fid) overwrites the old version in place.
404 and unknown-type responses (e.g. HTML) are not retried until the fid changes, or with --retry-failed.
"""

import argparse
import os
from collections import Counter
import sys
import time

from common import (
    LFS_DIR,
    doc_path,
    fetch_file,
    file_ext,
    group_by_fid,
    hardlink,
    is_new,
    link_file,
    parse_window,
    read_records,
    rel_path,
    save_records,
    sha1_file,
    utc_now,
    write_records,
)

DONE = ("downloaded", "seeded")
PERSISTENT = ("failed:404", "failed:unknown_type")  # an old failed:not_pdf is retried: it may be a JPEG / Word file


def ensure_paths(d, recs):
    """Point every doc_id path of this fid at the same file (hardlinks), refresh symlinks."""
    ext = file_ext(d)
    paths = [rel_path(r["doc_id"], ext) for r in recs]
    present = [LFS_DIR / p for p in paths if (LFS_DIR / p).is_file()]
    source = next((p for p in present if p.stat().st_size == d.get("size")), None)
    if source:
        verified = False
        for p in paths:
            target = LFS_DIR / p
            if target.is_file() and os.path.samefile(source, target):
                continue
            if target.is_file():  # a different inode: maybe an older version of this doc_id's file
                if not verified:
                    if sha1_file(source) != d.get("sha1"):
                        print(f"\tWARNING fid {d['fid']}: {source.name} does not match recorded sha1")
                        break
                    verified = True
                if target.stat().st_size == d.get("size") and sha1_file(target) == d.get("sha1"):
                    continue
            hardlink(source, target)
    # else: file not in LFS (e.g. on CI); ensure_file() fetches it when a later step needs it
    for r in recs:
        link_file(r["doc_id"], ext)
    d["nids"], d["paths"], d["format"] = [r["nid"] for r in recs], paths, ext


def main(args):
    circulars = read_records("circulars")
    downloads = read_records("downloads")
    all_groups = group_by_fid(circulars)
    groups = all_groups
    if args.scope == "new":
        window = parse_window(args.window)
        groups = {fid: recs for fid, recs in all_groups.items() if any(is_new(r, window) for r in recs)}

    status = {fid: downloads.get(fid, {}).get("status") for fid in groups}
    todo = [fid for fid, s in status.items() if s not in DONE and (args.retry_failed or s not in PERSISTENT)]
    done = sum(1 for s in status.values() if s in DONE)
    print(f"*** scope={args.scope}: {len(groups)} files for {sum(map(len, groups.values()))} circulars; "
          f"{done} already downloaded, {len(todo)} to download now, {len(groups) - done - len(todo)} skipped (404 / unknown type)")

    downloaded, skipped_failed, not_found, failed = [], [], [], []
    try:
        for fid, recs in groups.items():
            d = downloads.get(fid)
            if d and d.get("status") in DONE:
                ensure_paths(d, recs)
                save_records("downloads", downloads)
                continue
            if d and d.get("status") in PERSISTENT and not args.retry_failed:
                skipped_failed.append(fid)
                continue

            first = recs[0]
            url = first["file_url"]
            print(f"fid {fid} [{len(downloaded) + len(failed) + len(not_found) + 1}/{len(todo)}] {first['doc_id']} {url}")
            info, error = fetch_file(url, doc_path(first["doc_id"]))
            attempts = (d or {}).get("attempts", 0) + 1

            if not info:
                downloads[fid] = {
                    "fid": fid,
                    "nids": [r["nid"] for r in recs],
                    "url": url,
                    "paths": [rel_path(r["doc_id"]) for r in recs],
                    "status": f"failed:{error}",
                    "attempts": attempts,
                    "current": True,
                    "last_attempt_utc": utc_now(),
                }
                (not_found if f"failed:{error}" in PERSISTENT else failed).append(fid)
                print(f"\tFAILED {error}")
                save_records("downloads", downloads)
                time.sleep(args.delay)
                continue

            if first.get("filesize") and info["size"] != first["filesize"]:
                print(f"\tWARNING size {info['size']} differs from API filesize {first['filesize']}")
            dest = info["path"]
            d = downloads[fid] = {
                "fid": fid,
                "nids": [],
                "url": url,
                "paths": [],
                "size": info["size"],
                "sha1": sha1_file(dest),
                "last_modified": info["last_modified"],
                "etag": info["etag"],
                "download_time_utc": utc_now(),
                "status": "downloaded",
                "format": info["format"],
                "attempts": attempts,
                "current": True,
            }
            ensure_paths(d, recs)
            downloaded.append(fid)
            print(f"\tok {info['format']} ({info['size']} bytes, {len(recs)} doc_id{'s' if len(recs) > 1 else ''})")
            save_records("downloads", downloads)
            time.sleep(args.delay)
    finally:
        for fid, d in downloads.items():
            d["current"] = fid in all_groups
        write_records("downloads", downloads)

    formats = Counter(downloads[f].get("format") for f in downloaded)
    print(f"*** Downloaded: {len(downloaded)} of {len(todo)} {dict(formats) if formats else ''}")
    print(f"*** Not retried (404 / unknown type, use RETRY_FAILED=1): {len(skipped_failed)}")
    print(f"*** 404 / unknown type this run: {len(not_found)} {not_found[:20]}")
    print(f"*** Failed: {len(failed)} {failed[:20]}")
    have = sum(1 for fid in groups if downloads.get(fid, {}).get("status") in DONE)
    print(f"*** Files downloaded: {have} of {len(groups)} (scope={args.scope}); not downloaded: {len(groups) - have}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scope", choices=("new", "all"), default="all")
    p.add_argument("--window", default="7d", help="what --scope new means")
    p.add_argument("--retry-failed", action="store_true", help="also retry 404 / not-a-PDF failures")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between downloads")
    main(p.parse_args())
