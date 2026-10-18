"""Print pipeline counts and compare the site with archive.org.

Usage: status.py [--no-ia] [--check-codes]

--check-codes  exit 1 if any circular is held for a missing department code (used by `make setup`
               before any doc_id is frozen).
"""

import argparse
import sys
from collections import Counter

from common import IA_PREFIX, active, file_ext, identifier_for, read_records


def archive_org_identifiers():
    import internetarchive as ia

    return {r["identifier"] for r in ia.search_items(f"identifier:{IA_PREFIX}*", fields=["identifier"])}


def main(args):
    circulars, downloads = read_records("circulars"), read_records("downloads")
    wayback, archive = read_records("wayback"), read_records("archive")

    on_site = [r for r in circulars.values() if not r.get("removed_utc")]
    statuses = Counter(r.get("status") for r in on_site)
    act = [r for r in on_site if active(r)]
    fids = {r["fid"] for r in act}
    done = {f for f in fids if downloads.get(f, {}).get("status") in ("downloaded", "seeded")}
    failed = {f for f in fids if (downloads.get(f, {}).get("status") or "").startswith("failed")}
    linked = {f for f in done if wayback.get(f, {}).get("link_success")}
    uploaded = [r for r in act if archive.get((r["doc_id"], r["fid"]), {}).get("upload_success")]

    print("*** Circulars")
    print(f"\ton site: {len(on_site)}   removed from site: {len(circulars) - len(on_site)}")
    for s, n in sorted(statuses.items(), key=lambda x: str(x[0])):
        print(f"\t{s}: {n}")
    print(f"*** Files (unique files of ok circulars): {len(fids)}")
    print(f"\tdownloaded: {len(done)} / {len(fids)}   failed: {len(failed)}   pending: {len(fids) - len(done) - len(failed)}")
    print(f"\tdownloaded by type: {dict(Counter(file_ext(downloads[f]) for f in done).most_common())}")
    reasons = Counter(downloads[f]["status"] for f in failed)
    if reasons:
        print(f"\tfailed by reason: {dict(reasons)}")
    print(f"*** Wayback: linked {len(linked)} / {len(done)}   pending {len(done) - len(linked)}")
    print(f"*** archive.org: uploaded {len(uploaded)} / {len(act)}   pending {len(act) - len(uploaded)}")

    held = sorted((r["nid"], r["status"]) for r in on_site if (r.get("status") or "").startswith("held"))
    if held:
        print(f"*** Held ({len(held)}), fix in overrides.json / departments.json: {held}")

    if not args.no_ia:
        expected = {identifier_for(r["doc_id"]) for r in act}
        print("*** Site vs archive.org")
        print(f"\tentries on site {len(on_site)} - no_file {statuses.get('no_file', 0)} - held {len(held)} "
              f"= expected items {len(expected)}")
        try:
            found = archive_org_identifiers()
            missing, extra = sorted(expected - found), sorted(found - expected)
            print(f"\titems on archive.org: {len(found)}   missing: {len(missing)}   not expected: {len(extra)}")
            if missing:
                print(f"\tmissing (first 20): {missing[:20]}")
            if extra:
                print(f"\tnot expected (first 20): {extra[:20]}")
            print("\t(archive.org search can lag behind new uploads by a few hours)")
        except Exception as e:
            print(f"\tcould not search archive.org: {e}")

    if args.check_codes:
        no_code = [nid for nid, s in held if s == "held:no_dept_code"]
        if no_code:
            print(f">>> {len(no_code)} circulars held for a missing department code: fill in departments.json "
                  f"(or overrides.json) and run `make fetch` before continuing. nids: {no_code[:20]}")
            sys.exit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-ia", action="store_true", help="skip the archive.org search")
    p.add_argument("--check-codes", action="store_true", help="exit 1 if circulars are held for missing dept codes")
    main(p.parse_args())
