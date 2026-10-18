"""Update circulars.json from PMC's Drupal JSON:API and assign doc_ids.

Usage:
    fetch.py                    # new only: nodes with nid above the highest known nid (the daily run)
    fetch.py --full             # walk every node: update existing ones, mark removed ones, refresh departments.json

The default run never re-reads or changes known circulars; edits PMC makes to old circulars are picked up
by --full (`make backfill WHAT=crawl`).

doc_ids (<dept_code>.<YYYY-Mon-DD>.<NN>) are assigned once and never changed; see SPEC.md §2.
"""

import argparse
import datetime
from collections import Counter, defaultdict

from common import (
    API_DUMP_DIR,
    BASE_URL,
    DEPT_API,
    NODE_API,
    ChangeLog,
    effective,
    fmt_date,
    group_by_fid,
    is_english,
    iter_api,
    read_records,
    save_records,
    utc_now,
    write_records,
)

NODE_FIELDS = "drupal_internal__nid,title,langcode,field_circular_date,created,changed,field_circular_upload,field_department"
TRACKED = ("title_en", "circular_date", "tid")  # a change here is a metadata_changed event (title_mr: in the mr pass)
MIN_FULL_RATIO = 0.9  # don't mark nodes removed if a full walk saw fewer than this share of known nodes


def nid_filter(after):
    if after is None:
        return {}
    return {
        "filter[n][condition][path]": "drupal_internal__nid",
        "filter[n][condition][operator]": ">",
        "filter[n][condition][value]": str(after),
    }


def node_params(after):
    return {
        "page[limit]": "50",
        "sort": "drupal_internal__nid",
        "include": "field_circular_upload,field_department",
        "fields[node--circular]": NODE_FIELDS,
        "fields[file--file]": "drupal_internal__fid,filename,filesize,uri",
        "fields[taxonomy_term--department]": "drupal_internal__tid,name",
        **nid_filter(after),
    }


def title_params(after):
    return {
        "page[limit]": "50",
        "sort": "drupal_internal__nid",
        "fields[node--circular]": "drupal_internal__nid,title,langcode,changed",
        **nid_filter(after),
    }


def first(data):
    return (data[0] if data else None) if isinstance(data, list) else data


def parse_nodes(page):
    included = {x["id"]: x.get("attributes", {}) for x in page.get("included", [])}
    for n in page.get("data", []):
        a, rel = n["attributes"], n.get("relationships", {})
        f = first((rel.get("field_circular_upload") or {}).get("data"))
        fa = included.get(f["id"], {}) if f else {}
        dp = first((rel.get("field_department") or {}).get("data"))
        da = included.get(dp["id"], {}) if dp else {}
        uri = (fa.get("uri") or {}).get("url")
        yield {
            "nid": a["drupal_internal__nid"],
            "uuid": n["id"],
            "title_en": a.get("title"),
            "circular_date": a.get("field_circular_date"),
            "tid": da.get("drupal_internal__tid") or ((dp or {}).get("meta") or {}).get("drupal_internal__target_id"),
            "department": da.get("name"),
            "created": a.get("created"),
            "changed": a.get("changed"),
            "fid": fa.get("drupal_internal__fid") or ((f or {}).get("meta") or {}).get("drupal_internal__target_id"),
            "file_url": BASE_URL + uri if uri else None,
            "filename": fa.get("filename"),
            "filesize": fa.get("filesize"),
        }


def new_record(node, now):
    return {
        "nid": node["nid"],
        "doc_id": None,
        "uuid": node["uuid"],
        "title_en": node["title_en"],
        "title_mr": None,
        "title_en_is_english": is_english(node["title_en"]),
        "circular_date": node["circular_date"],
        "tid": node["tid"],
        "department": node["department"],
        "created": node["created"],
        "changed": node["changed"],
        "fid": node["fid"],
        "file_url": node["file_url"],
        "filename": node["filename"],
        "filesize": node["filesize"],
        "same_file_as": [],
        "status": None,
        "first_seen_utc": now,
        "last_seen_utc": now,
        "removed_utc": None,
    }


def upsert(circulars, node, now, log, new_nids):
    nid = node["nid"]
    rec = circulars.get(nid)
    if rec is None:
        circulars[nid] = new_record(node, now)
        new_nids.add(nid)
        log.add("new", nid)
        return

    doc_id = rec.get("doc_id")
    if rec.get("removed_utc"):
        log.add("reappeared", nid, doc_id)
        rec["removed_utc"] = None
    if node["fid"] != rec.get("fid"):
        log.add("file_replaced", nid, doc_id,
                old={"fid": rec.get("fid"), "url": rec.get("file_url")},
                new={"fid": node["fid"], "url": node["file_url"]})
        print(f"\tfile replaced: nid {nid} fid {rec.get('fid')} -> {node['fid']}")
    changed = [f for f in TRACKED if rec.get(f) != node[f]]
    if changed:
        log.add("metadata_changed", nid, doc_id, fields=changed)

    for k, v in node.items():
        rec[k] = v
    rec["title_en_is_english"] = is_english(node["title_en"])
    rec["last_seen_utc"] = now


def update_mr(circulars, page, log, new_nids):
    for n in page.get("data", []):
        a = n["attributes"]
        rec = circulars.get(a["drupal_internal__nid"])
        if not rec:
            continue
        title = a.get("title") if a.get("langcode") == "mr" else None  # no translation: endpoint falls back to en
        if rec.get("title_mr") != title:
            if rec["nid"] not in new_nids and rec.get("title_mr") is not None:
                log.add("metadata_changed", rec["nid"], rec.get("doc_id"), fields=["title_mr"])
            rec["title_mr"] = title


def refresh_departments(depts):
    params = {
        "page[limit]": "50",
        "sort": "drupal_internal__tid",
        "fields[taxonomy_term--department]": "drupal_internal__tid,name,field_slug",
    }
    for lang in ("en", "mr"):
        for page in iter_api(DEPT_API, params, lang):
            for t in page.get("data", []):
                a = t["attributes"]
                tid = a["drupal_internal__tid"]
                rec = depts.get(tid)
                if rec is None:
                    rec = depts[tid] = {"tid": tid, "name_en": None, "name_mr": None, "slug": None, "code": None}
                    print(f"*** WARNING new department tid={tid} {a.get('name')!r}: add a code to departments.json")
                rec[f"name_{lang}"] = a.get("name")
                if lang == "en":
                    rec["slug"] = (a.get("field_slug") or "").strip() or None
    write_records("departments", depts)
    print(f"*** departments.json: {len(depts)} departments, {sum(1 for d in depts.values() if not d.get('code'))} without code")


def mark_removed(circulars, seen, now, log):
    known = [nid for nid, r in circulars.items() if not r.get("removed_utc")]
    if len(seen) < MIN_FULL_RATIO * len(known):
        print(f"*** WARNING full walk saw only {len(seen)} of {len(known)} known nodes; not marking any as removed")
        return
    for nid in known:
        if nid not in seen:
            rec = circulars[nid]
            rec["removed_utc"] = now
            log.add("removed", nid, rec.get("doc_id"))
            print(f"\tremoved from site: nid {nid} {rec.get('doc_id')}")


def assign(circulars, depts, overrides, log):
    """Set status, assign doc_ids to new entries (in nid order), refresh same_file_as."""
    used = defaultdict(int)  # (code, date) -> highest NN in use
    for rec in circulars.values():
        if rec.get("doc_id"):
            code, ds, nn = rec["doc_id"].split(".")
            used[(code, ds)] = max(used[(code, ds)], int(nn))

    for nid in sorted(circulars):
        rec = circulars[nid]
        old = rec.get("status")
        if not rec.get("file_url"):
            status = "no_file"
        elif rec.get("doc_id"):
            status = "ok"  # frozen, even if PMC later edits date or department
        else:
            d, code, _, _ = effective(rec, depts, overrides)
            if not d:
                status = "held:bad_date"
            elif not code:
                status = "held:no_dept_code"
            else:
                ds = fmt_date(d)
                used[(code, ds)] += 1
                rec["doc_id"] = f"{code}.{ds}.{used[(code, ds)]:02d}"
                status = "ok"
        if status != old:
            if status.startswith("held"):
                log.add("held", nid, reason=status)
            elif old and old.startswith("held") and status == "ok":
                log.add("released", nid, rec["doc_id"])
        rec["status"] = status

    groups = group_by_fid(circulars)
    for rec in circulars.values():
        ids = [r["doc_id"] for r in groups.get(rec.get("fid"), [])] if rec.get("doc_id") else []
        rec["same_file_as"] = [i for i in ids if i != rec.get("doc_id")]

    for e in log.pending:  # events logged before the doc_id existed
        if not e.get("doc_id"):
            e["doc_id"] = circulars.get(e["nid"], {}).get("doc_id")


def main(args):
    circulars = read_records("circulars")
    depts = read_records("departments")
    overrides = read_records("overrides")
    if not depts:
        raise SystemExit("departments.json is missing or empty")

    full = args.full or not circulars
    now = utc_now()
    dump_dir = API_DUMP_DIR / datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    log, new_nids, seen = ChangeLog(), set(), set()

    if full:
        refresh_departments(depts)
        after = None
        print("*** Full walk of all circulars")
    else:
        after = max(circulars)  # nids only grow; known circulars are not re-read
        print(f"*** New circulars only: nid > {after}")

    try:
        for idx, page in enumerate(iter_api(NODE_API, node_params(after), "en", dump_dir)):
            for node in parse_nodes(page):
                upsert(circulars, node, now, log, new_nids)
                seen.add(node["nid"])
            save_records("circulars", circulars)
            print(f"\ten page {idx + 1}: {len(seen)} nodes")

        mr_seen = 0
        for idx, page in enumerate(iter_api(NODE_API, title_params(after), "mr", dump_dir)):
            update_mr(circulars, page, log, new_nids)
            mr_seen += len(page.get("data", []))
            save_records("circulars", circulars)
            print(f"\tmr page {idx + 1}: {mr_seen} nodes")
    finally:
        write_records("circulars", circulars)  # pages are in nid order, so a partial run resumes cleanly

    if full:
        mark_removed(circulars, seen, now, log)
    assign(circulars, depts, overrides, log)
    write_records("circulars", circulars)

    events = Counter(e["event"] for e in log.pending)
    log.flush()

    statuses = Counter(r["status"] for r in circulars.values() if not r.get("removed_utc"))
    held = sorted(nid for nid, r in circulars.items() if (r.get("status") or "").startswith("held") and not r.get("removed_utc"))
    print(f"*** Nodes seen this run: {len(seen)}; events: {dict(events) or 'none'}")
    print(f"*** circulars.json: {len(circulars)} records, status {dict(statuses)}")
    if held:
        print(f"*** HELD ({len(held)}), fix in overrides.json / departments.json:")
        for nid in held:
            r = circulars[nid]
            print(f"\t{nid} {r['status']} date={r.get('circular_date')} tid={r.get('tid')} {(r.get('title_en') or '')[:70]}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--full", action="store_true", help="walk all nodes, mark removed ones, refresh departments")
    main(p.parse_args())
