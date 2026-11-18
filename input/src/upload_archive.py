"""Upload circular PDFs to archive.org (one item per website entry), or refresh item metadata.

Usage:
    upload_archive.py [--scope new|all] [--window 7d] [--limit N] [--collection pune-pmc] [--delay 5] [--retry-failed]
    upload_archive.py --update-metadata [--limit N] [--collection pune-pmc] [--delay 5]
    upload_archive.py ... --only DOC_ID[,DOC_ID]   (just these doc_ids, for testing)

Item identifier: in.gov.pmc.circular.<doc_id>, file <doc_id>.<ext> (pdf, or jpg / png / doc / docx as PMC posted it).
A replaced file (new fid) is uploaded under the same name; archive.org keeps the old one in history/files/.
--scope new  only circulars first seen within --window (the daily run); all = the whole backlog.
--update-metadata only calls modify_metadata for items whose metadata hash changed.

Gentle on archive.org: one item at a time, --delay seconds between items, and before each item a wait while IA-S3 says
it is overloaded or this account has many tasks queued. A "reduce your request rate" / 503 / timeout error backs off
5, 10, 20 minutes and retries the item; if that still fails the run stops (re-run later to resume).
"""

import argparse
import hashlib
import html
import json
import sys
import time

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
BACKOFF_MINUTES = (5, 10, 20)
# wait while this account's queued archive.org tasks exceed this share of its limit (derive: 500, modify_xml: 1000)
MAX_INFLIGHT_SHARE = {"derive.php": 0.8, "modify_xml.php": 0.1}
CAPACITY_WAIT = 60  # seconds between capacity checks
CAPACITY_GIVE_UP = 30 * 60
REJECTED = "unacceptable"  # archive.org refused the file itself (e.g. a broken PDF); retried only with --retry-failed
TRANSIENT = ("reduce your request rate", "slowdown", "slow down", "over_limit", "overloaded", "timed out", "timeout",
             "connection", "http 429", "http 500", "http 502", "http 503", "http 504", " 503 ", " 429 ")
# fields this script used to set: removed from an item when no longer built (e.g. year, an empty wayback_url)
OLD_FIELDS = ("year",)


class Busy(Exception):
    """archive.org stayed busy; stop the run."""


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
        "creator": PMC_EN,
        "publisher": PMC_EN,
        "language": "mar",
        "subject": [s for s in ["Circular", dept_en] if s],
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


def wait_for_capacity(session, identifier, cmd, access_key):
    """Block while IA-S3 is overloaded (uploads) or this account has many `cmd` tasks queued."""
    waited = 0
    while True:
        try:
            busy = []
            if cmd == "derive.php" and session.s3_is_overloaded(identifier=identifier, access_key=access_key):
                busy.append("S3 overloaded")
            limit = session.get_tasks_api_rate_limit(cmd=cmd).get("value", {})
            inflight, cap = limit.get("tasks_inflight", 0), limit.get("tasks_limit", 500)
            if inflight > cap * MAX_INFLIGHT_SHARE[cmd]:
                busy.append(f"{inflight} {cmd} tasks queued, limit {cap}")
        except Exception as e:
            busy = [f"capacity check failed: {e}"]
        if not busy:
            return
        if waited >= CAPACITY_GIVE_UP:
            raise Busy(f"archive.org busy for {waited // 60} min ({'; '.join(busy)})")
        print(f"\tarchive.org busy ({'; '.join(busy)}), waiting {CAPACITY_WAIT}s")
        time.sleep(CAPACITY_WAIT)
        waited += CAPACITY_WAIT


def is_transient(e):
    text = f" {e} ".lower()
    return any(t in text for t in TRANSIENT)


def with_backoff(fn, *args):
    """Run fn; on a rate-limit / overload / timeout error back off and retry, then give up with Busy."""
    for minutes in (*BACKOFF_MINUTES, None):
        try:
            return fn(*args)
        except Exception as e:
            if not is_transient(e):
                raise
            if minutes is None:
                raise Busy(f"still failing after backing off {sum(BACKOFF_MINUTES)} min: {str(e)[:200]}") from e
            print(f"\tarchive.org pushed back ({str(e)[:120]}); backing off {minutes} min")
            time.sleep(minutes * 60)


def upload(session, identifier, filename, path, metadata):
    item = session.get_item(identifier)
    responses = item.upload(
        files={filename: str(path)},
        metadata=metadata,
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


def modify(session, identifier, metadata):
    """Replace the item's metadata fields with `metadata`; drop fields we no longer set.

    Collections are only ever added: an item already in the target keeps its list (archive.org adds the parent,
    government-documents, next to pune-pmc), and one elsewhere (opensource) gets the target in front, since
    archive.org refuses "Not authorized to remove collection(s)"."""
    item = session.get_item(identifier)
    current = item.metadata
    patch = dict(metadata)
    have = current.get("collection", [])
    have = have if isinstance(have, list) else [have]
    if metadata.get("collection") in have:
        patch.pop("collection")
    elif have:
        patch["collection"] = [metadata["collection"]] + have
    for key in OLD_FIELDS + ("title_en_site", "title_mr", "pmc_same_file_as", "wayback_url"):
        if key in current and key not in patch:
            patch[key] = "REMOVE_TAG"
    r = item.modify_metadata(patch)
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
        if args.only and rec["doc_id"] not in args.only:
            continue
        prev = archive.get((rec["doc_id"], rec["fid"]), {})
        if prev.get("upload_success"):
            continue
        if REJECTED in prev.get("message", "").lower() and not args.retry_failed:
            continue
        if args.scope == "new" and not is_new(rec, window):
            continue
        todo.append(rec)
    return todo[: args.limit] if args.limit else todo


def run_upload(args, session, keys, circulars, downloads, wayback, archive, depts, overrides):
    todo = select(circulars, downloads, archive, args)
    everything = argparse.Namespace(scope="all", window="1d", limit=0, only=None, retry_failed=args.retry_failed)
    pending = len(select(circulars, downloads, archive, everything))
    print(f"*** To upload this run: {len(todo)} (scope={args.scope}, limit={args.limit or '-'}, "
          f"collection={args.collection}, delay={args.delay}s; {pending} pending overall)")

    uploaded, failed, consecutive = [], [], 0
    args.stopped = None
    have_item = {k[0] for k, a in archive.items() if a.get("upload_success")}
    for idx, rec in enumerate(todo):
        doc_id, fid = rec["doc_id"], rec["fid"]
        identifier = identifier_for(doc_id)
        d = downloads[fid]
        print(f"**** {doc_id} [{idx + 1}/{len(todo)}] nid {rec['nid']}")
        record = {"doc_id": doc_id, "fid": fid, "identifier": identifier, "collection": args.collection,
                  "sha1": d.get("sha1"), "time_utc": utc_now()}
        if idx:
            time.sleep(args.delay)
        try:
            path = ensure_file(doc_id, d)
            metadata = build_metadata(rec, d, wayback.get(fid, {}), depts, overrides, args.collection)
            wait_for_capacity(session, identifier, "derive.php", keys[0])
            with_backoff(upload, session, identifier, f"{doc_id}.{file_ext(d)}", path, metadata)
            if doc_id in have_item:  # metadata in an upload only applies when the item is created
                with_backoff(modify, session, identifier, metadata)
            have_item.add(doc_id)
            record.update(upload_success=True, archive_url=f"https://archive.org/details/{identifier}",
                          metadata_sha1=metadata_hash(metadata), message="")
            uploaded.append(doc_id)
            consecutive = 0
            print(f"\tUPLOADED: {record['archive_url']}")
        except Busy as e:
            args.stopped = f"{e}; {len(todo) - idx} left for the next run"
            break
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


def run_update_metadata(args, session, keys, circulars, downloads, wayback, archive, depts, overrides):
    updated, failed, todo, uploaded = [], [], [], 0
    args.stopped = None
    for nid in sorted(circulars):
        rec = circulars[nid]
        a = archive.get((rec.get("doc_id"), rec.get("fid")))
        if not active(rec) or not a or not a.get("upload_success"):
            continue
        if args.only and rec["doc_id"] not in args.only:
            continue
        uploaded += 1
        metadata = build_metadata(rec, downloads.get(rec["fid"], {}), wayback.get(rec["fid"], {}), depts, overrides,
                                  args.collection)
        h = metadata_hash(metadata)
        if h != a.get("metadata_sha1"):
            todo.append((rec, a, metadata, h))
    print(f"*** Metadata changed: {len(todo)} of {uploaded} items on archive.org "
          f"(this run: {min(len(todo), args.limit) if args.limit else len(todo)}, delay={args.delay}s)")
    todo = todo[: args.limit] if args.limit else todo

    for idx, (rec, a, metadata, h) in enumerate(todo):
        if idx:
            time.sleep(args.delay)
        try:
            wait_for_capacity(session, a["identifier"], "modify_xml.php", keys[0])
            with_backoff(modify, session, a["identifier"], metadata)
            a.update(metadata_sha1=h, collection=args.collection, metadata_time_utc=utc_now())
            updated.append(rec["doc_id"])
            print(f"{rec['doc_id']} [{idx + 1}/{len(todo)}]: metadata updated")
        except Busy as e:
            args.stopped = f"{e}; {len(todo) - idx} left for the next run"
            break
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
    session = ia.get_session(config={"s3": {"access": keys[0], "secret": keys[1]}})

    run = run_update_metadata if args.update_metadata else run_upload
    try:
        done, failed = run(args, session, keys, circulars, downloads, wayback, archive, depts, overrides)
    finally:
        write_records("archive", archive)

    print(f"*** {'Updated' if args.update_metadata else 'Uploaded'}: {len(done)}")
    print(f"*** Failed: {len(failed)} {failed[:20]}")
    if args.stopped:  # not a failure: archive.org asked us to wait
        print(f"*** Stopped early, archive.org busy: {args.stopped}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scope", choices=("new", "all"), default="all")
    p.add_argument("--window", default="7d", help="what --scope new means")
    p.add_argument("--limit", type=int, default=0, help="max items this run (0 = no limit)")
    p.add_argument("--collection", default="pune-pmc")
    p.add_argument("--delay", type=float, default=5, help="seconds to pause between items")
    p.add_argument("--retry-failed", action="store_true", help="also retry files archive.org rejected")
    p.add_argument("--only", type=lambda v: set(v.split(",")), help="only these doc_ids (comma separated)")
    p.add_argument("--update-metadata", action="store_true", help="re-apply changed metadata to uploaded items")
    main(p.parse_args())
