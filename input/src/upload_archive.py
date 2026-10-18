"""Upload circular PDFs to archive.org (one item per website entry), or refresh item metadata.

Usage:
    upload_archive.py [--scope new|all] [--window 7d] [--limit N] [--collection opensource]
    upload_archive.py --update-metadata [--collection opensource]

Item identifier: in.gov.pmc.circular.<doc_id>, file <doc_id>.<ext> (pdf, or jpg / png / doc / docx as PMC posted it).
A replaced file (new fid) is uploaded under the same name; archive.org keeps the old one in history/files/.
--scope new  only circulars first seen within --window (the daily run); all = the whole backlog.
--update-metadata only calls modify_metadata for items whose metadata hash changed.
"""

import argparse
import hashlib
import html
import json
import sys

import internetarchive as ia

from common import (
    PMC_EN,
    SOURCE_PAGE,
    active,
    effective,
    ensure_file,
    file_ext,
    identifier_for,
    is_new,
    load_env,
    parse_window,
    read_records,
    require_env,
    save_records,
    utc_now,
    write_records,
)

DONE = ("downloaded", "seeded")
MAX_CONSECUTIVE_FAILURES = 5


def build_metadata(rec, d, wb, depts, overrides, collection):
    date, _, dept_en, dept_mr = effective(rec, depts, overrides)
    title_en_site = rec["title_en"] if rec.get("title_en_is_english") else ""
    title = title_en_site or rec.get("title_mr") or rec.get("title_en") or rec["doc_id"]
    wayback_url = wb.get("archive_url", "") if wb.get("link_success") else ""
    dept = f"{dept_en} ({dept_mr})" if dept_mr and dept_mr != dept_en else dept_en

    def row(label, value):
        return f'<tr><td style="vertical-align: top"><b>{label}</b>:&nbsp;</td><td style="vertical-align: top">{value}</td></tr>'

    def link(href, text):
        return f'<a href="{html.escape(href)}">{html.escape(text)}</a>'

    rows = []
    if title_en_site:
        rows.append(row("Title (English)", html.escape(title_en_site)))
    if rec.get("title_mr"):
        rows.append(row("Title (Marathi)", html.escape(rec["title_mr"])))
    rows += [
        row("Department", html.escape(dept or "")),
        row("Date", date.isoformat() if date else ""),
        row("Source URL", link(rec["file_url"], rec["file_url"])),
        row("Source page", link(SOURCE_PAGE, SOURCE_PAGE)),
    ]
    if wayback_url:
        rows.append(row("Wayback URL", link(wayback_url, "web.archive.org")))
    if d.get("download_time_utc"):
        rows.append(row("Downloaded", html.escape(d["download_time_utc"])))
    if rec.get("same_file_as"):
        rows.append(row("Same file also posted as", ", ".join(
            link(f"https://archive.org/details/{identifier_for(i)}", i) for i in rec["same_file_as"])))
    description = (
        f"<p>Circular of the {html.escape(dept or 'Pune Municipal Corporation')}, {PMC_EN}"
        f"{', dated ' + date.strftime('%d %B %Y') if date else ''}. Published at {link(SOURCE_PAGE, SOURCE_PAGE)}.</p>\n"
        "<table>\n" + "\n".join(rows) + "\n</table>"
    )

    metadata = {
        "collection": collection,
        "mediatype": "texts",
        "title": title,
        "title_mr": rec.get("title_mr") or "",
        "title_en_site": title_en_site,
        "date": date.isoformat() if date else "",
        "year": str(date.year) if date else "",
        "creator": f"{dept}, {PMC_EN}" if dept else PMC_EN,
        "publisher": PMC_EN,
        "language": "mar",
        "subject": [s for s in [PMC_EN, "PMC", "Pune", "circular", dept_en, str(date.year) if date else ""] if s],
        "source": rec["file_url"],
        "source_page": SOURCE_PAGE,
        "pmc_doc_id": rec["doc_id"],
        "pmc_nid": str(rec["nid"]),
        "pmc_fid": str(rec["fid"]),
        "sha1": d.get("sha1", ""),
        "pmc_file_format": file_ext(d),
        "pmc_same_file_as": rec.get("same_file_as") or [],
        "wayback_url": wayback_url,
        "description": description,
    }
    return {k: v for k, v in metadata.items() if v}


def metadata_hash(metadata):
    return hashlib.sha1(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def upload(identifier, filename, path, metadata, keys):
    responses = ia.upload(
        identifier,
        files={filename: str(path)},
        metadata=metadata,
        access_key=keys[0],
        secret_key=keys[1],
        checksum=True,  # skip if the same file is already in the item
        retries=5,
        retries_sleep=30,
        validate_identifier=True,
    )
    bad = [r for r in responses if r.status_code != 200]
    if bad:
        raise RuntimeError(f"HTTP {bad[0].status_code}: {bad[0].text[:200]}")
    if not responses:
        print("\tfile already present in item (checksum match)")


def modify(identifier, metadata, keys):
    r = ia.modify_metadata(identifier, metadata, access_key=keys[0], secret_key=keys[1])
    if r.status_code == 200 or "no changes" in r.text.lower():
        return
    raise RuntimeError(f"modify_metadata HTTP {r.status_code}: {r.text[:200]}")


def select(circulars, downloads, archive, args):
    window = parse_window(args.window)
    todo = []
    for nid in sorted(circulars):
        rec = circulars[nid]
        d = downloads.get(rec.get("fid"), {})
        if not active(rec) or d.get("status") not in DONE:
            continue
        if archive.get((rec["doc_id"], rec["fid"]), {}).get("upload_success"):
            continue
        if args.scope == "new" and not is_new(rec, window):
            continue
        todo.append(rec)
    return todo[: args.limit] if args.limit else todo


def run_upload(args, keys, circulars, downloads, wayback, archive, depts, overrides):
    todo = select(circulars, downloads, archive, args)
    pending = len(select(circulars, downloads, archive, argparse.Namespace(scope="all", window="1d", limit=0)))
    print(f"*** To upload this run: {len(todo)} (scope={args.scope}, limit={args.limit or '-'}, "
          f"collection={args.collection}; {pending} pending overall)")

    uploaded, failed, consecutive = [], [], 0
    have_item = {k[0] for k, a in archive.items() if a.get("upload_success")}
    for idx, rec in enumerate(todo):
        doc_id, fid = rec["doc_id"], rec["fid"]
        identifier = identifier_for(doc_id)
        d = downloads[fid]
        print(f"**** {doc_id} [{idx + 1}/{len(todo)}] nid {rec['nid']}")
        record = {"doc_id": doc_id, "fid": fid, "identifier": identifier, "collection": args.collection,
                  "sha1": d.get("sha1"), "time_utc": utc_now()}
        try:
            path = ensure_file(doc_id, d)
            metadata = build_metadata(rec, d, wayback.get(fid, {}), depts, overrides, args.collection)
            upload(identifier, f"{doc_id}.{file_ext(d)}", path, metadata, keys)
            if doc_id in have_item:  # metadata in an upload only applies when the item is created
                modify(identifier, metadata, keys)
            have_item.add(doc_id)
            record.update(upload_success=True, archive_url=f"https://archive.org/details/{identifier}",
                          metadata_sha1=metadata_hash(metadata), message="")
            uploaded.append(doc_id)
            consecutive = 0
            print(f"\tUPLOADED: {record['archive_url']}")
        except Exception as e:
            record.update(upload_success=False, message=str(e)[:500])
            failed.append(doc_id)
            consecutive += 1
            print(f"\tFAILED: {record['message']}")

        archive[(doc_id, fid)] = record
        save_records("archive", archive)
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            print(">>> Stopping: too many consecutive failures, re-run later")
            break
    return uploaded, failed


def run_update_metadata(args, keys, circulars, downloads, wayback, archive, depts, overrides):
    updated, failed, todo, uploaded = [], [], [], 0
    for nid in sorted(circulars):
        rec = circulars[nid]
        a = archive.get((rec.get("doc_id"), rec.get("fid")))
        if not active(rec) or not a or not a.get("upload_success"):
            continue
        uploaded += 1
        metadata = build_metadata(rec, downloads.get(rec["fid"], {}), wayback.get(rec["fid"], {}), depts, overrides,
                                  args.collection)
        h = metadata_hash(metadata)
        if h != a.get("metadata_sha1"):
            todo.append((rec, a, metadata, h))
    print(f"*** Metadata changed: {len(todo)} of {uploaded} items on archive.org")

    for idx, (rec, a, metadata, h) in enumerate(todo):
        try:
            modify(a["identifier"], metadata, keys)
            a.update(metadata_sha1=h, collection=args.collection, metadata_time_utc=utc_now())
            updated.append(rec["doc_id"])
            print(f"{rec['doc_id']} [{idx + 1}/{len(todo)}]: metadata updated")
        except Exception as e:
            failed.append(rec["doc_id"])
            print(f"{rec['doc_id']} [{idx + 1}/{len(todo)}]: FAILED {e}")
        save_records("archive", archive)
    return updated, failed


def main(args):
    load_env()
    keys = require_env("IA_ACCESS_KEY", "IA_SECRET_KEY")
    circulars, downloads = read_records("circulars"), read_records("downloads")
    wayback, archive = read_records("wayback"), read_records("archive")
    depts, overrides = read_records("departments"), read_records("overrides")

    run = run_update_metadata if args.update_metadata else run_upload
    try:
        done, failed = run(args, keys, circulars, downloads, wayback, archive, depts, overrides)
    finally:
        write_records("archive", archive)

    print(f"*** {'Updated' if args.update_metadata else 'Uploaded'}: {len(done)}")
    print(f"*** Failed: {len(failed)} {failed[:20]}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scope", choices=("new", "all"), default="all")
    p.add_argument("--window", default="7d", help="what --scope new means")
    p.add_argument("--limit", type=int, default=0, help="max uploads this run (0 = no limit)")
    p.add_argument("--collection", default="opensource")
    p.add_argument("--update-metadata", action="store_true", help="re-apply changed metadata to uploaded items")
    main(p.parse_args())
