"""Shared paths, constants and helpers for the punePMC import scripts."""

import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import time
import urllib.parse
import zipfile
from collections import defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

PUB = "circulars"  # publication type; other PMC publications later sit beside it
HF_PREFIX = f"publications/{PUB}/pdfs"  # same layout in LFS/punePMC and on HF
LFS_DIR = ROOT / "LFS" / "punePMC"
PDFS_DIR = LFS_DIR / HF_PREFIX
API_DUMP_DIR = LFS_DIR / "api" / PUB
DOCS_DIR = ROOT / "input" / "documents" / "publications" / PUB
LINKS_DIR = DOCS_DIR / "pdfs"
CHANGES_DIR = DOCS_DIR / "changes"

BASE_URL = "https://webadmin.pmc.gov.in"
NODE_API = "/jsonapi/node/circular"
DEPT_API = "/jsonapi/taxonomy_term/department"
SOURCE_PAGE = "https://www.pmc.gov.in/en/l/circular"
USER_AGENT = "Mozilla/5.0"
API_DELAY = 0.5  # seconds between API pages

IA_PREFIX = "in.gov.pmc.circular."
PMC_EN = "Pune Municipal Corporation"
PMC_MR = "पुणे महानगरपालिका"
DEFAULT_HF_REPO_ID = "Orgpedia/punePMC"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MIN_YEAR = 1950

# Record key fields per state file in DOCS_DIR
KEYS = {
    "departments": ("tid",),
    "overrides": ("nid",),
    "circulars": ("nid",),
    "downloads": ("fid",),
    "wayback": ("fid",),
    "archive": ("doc_id", "fid"),
}


# ---------------------------------------------------------------- environment

def load_env():
    load_dotenv(ROOT / ".env")  # existing env vars (GitHub Actions secrets) win


def require_env(*names):
    values = [os.environ.get(n, "").strip() for n in names]
    missing = [n for n, v in zip(names, values) if not v]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)} (set them in .env or as secrets)")
    return values


def hf_settings():
    repo_id = os.environ.get("HF_REPO_ID", "").strip() or DEFAULT_HF_REPO_ID
    token = os.environ.get("HF_TOKEN", "").strip() or None
    return repo_id, token


# ---------------------------------------------------------------- time

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z%z")


def parse_utc(s):
    """Parse a utc_now() string (or an ISO timestamp) into an aware datetime, None if empty."""
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return datetime.datetime.fromisoformat(s)


def parse_window(s):
    """'7d' / '12h' / '30' (days) -> timedelta"""
    m = re.fullmatch(r"\s*(\d+)\s*([dh]?)\s*", s or "")
    if not m:
        raise SystemExit(f"Bad time window: {s!r} (use e.g. 7d or 12h)")
    n, unit = int(m.group(1)), m.group(2) or "d"
    return datetime.timedelta(days=n) if unit == "d" else datetime.timedelta(hours=n)


def within(s, window):
    t = parse_utc(s)
    return bool(t) and datetime.datetime.now(datetime.timezone.utc) - t <= window


# ---------------------------------------------------------------- state files

def record_key(name, r):
    fields = KEYS[name]
    return r[fields[0]] if len(fields) == 1 else tuple(r[f] for f in fields)


def read_records(name):
    """Read DOCS_DIR/<name>.json as a dict keyed by the file's key field(s)."""
    path = DOCS_DIR / f"{name}.json"
    if not path.exists():
        return {}
    return {record_key(name, r): r for r in json.loads(path.read_text())}


def write_records(name, records):
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCS_DIR / f"{name}.json"
    ordered = [records[k] for k in sorted(records)]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    _last_write[name] = time.monotonic()


_last_write = {}


def save_records(name, records, every=10):
    """write_records at most every `every` seconds; call write_records at the end (and in finally)."""
    if time.monotonic() - _last_write.get(name, 0) >= every:
        write_records(name, records)


def _sigterm(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


signal.signal(signal.SIGTERM, _sigterm)  # CI cancellation -> KeyboardInterrupt -> finally blocks save state


class ChangeLog:
    """Buffer events and append them to changes/<YYYY-MM-DD>.json on flush()."""

    def __init__(self):
        self.pending = []

    def add(self, event, nid, doc_id=None, **extra):
        e = {"time_utc": utc_now(), "nid": nid, "doc_id": doc_id, "event": event}
        e.update(extra)
        self.pending.append(e)

    def flush(self):
        if not self.pending:
            return
        CHANGES_DIR.mkdir(parents=True, exist_ok=True)
        path = CHANGES_DIR / f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}.json"
        events = json.loads(path.read_text()) if path.exists() else []
        events.extend(self.pending)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(events, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(path)
        self.pending = []


# ---------------------------------------------------------------- http / JSON:API

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def get_json(url, params=None, retries=4):
    error = None
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=90)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            error = e
            print(f"\tattempt {attempt}/{retries} failed: {e}")
            time.sleep(5 * attempt)
    raise RuntimeError(f"GET {url} failed: {error}")


def iter_api(path, params, lang="en", dump_dir=None):
    """Yield JSON:API pages, following links.next (rewritten to https)."""
    url = BASE_URL + ("" if lang == "en" else f"/{lang}") + path
    page = 0
    while url:
        data = get_json(url, params if page == 0 else None)
        if dump_dir:
            dump_dir.mkdir(parents=True, exist_ok=True)
            (dump_dir / f"{lang}-page-{page:03d}.json").write_text(json.dumps(data, ensure_ascii=False))
        yield data
        nxt = (data.get("links") or {}).get("next")
        href = nxt.get("href") if isinstance(nxt, dict) else nxt
        url = href.replace("http://", "https://", 1) if href else None
        page += 1
        time.sleep(API_DELAY)


def same_url(a, b):
    return bool(a) and bool(b) and urllib.parse.unquote(a) == urllib.parse.unquote(b)


# ---------------------------------------------------------------- naming

def valid_date(s):
    """'YYYY-MM-DD' -> date if plausible (MIN_YEAR .. today+30d), else None."""
    try:
        d = datetime.date.fromisoformat(s or "")
    except ValueError:
        return None
    if d.year < MIN_YEAR or d > datetime.date.today() + datetime.timedelta(days=30):
        return None
    return d


def fmt_date(d):
    return f"{d.year:04d}-{MONTHS[d.month - 1]}-{d.day:02d}"


def is_english(title):
    t = title or ""
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    deva = sum(1 for c in t if "ऀ" <= c <= "ॿ")
    return latin > deva


def effective(rec, depts, overrides):
    """(date, dept_code, dept_en, dept_mr) for a circular after overrides; date/code may be None."""
    ov = overrides.get(rec["nid"], {})
    d = valid_date(ov.get("date") or rec.get("circular_date"))
    code = ov.get("dept_code")
    if code:
        dept = next((t for t in depts.values() if t.get("code") == code), {})
    else:
        dept = depts.get(rec.get("tid"), {})
        code = dept.get("code")
    return d, code, dept.get("name_en") or rec.get("department"), dept.get("name_mr")


def doc_year(doc_id):
    return doc_id.split(".")[1][:4]


def file_ext(download):
    """Extension of a fid's file: its downloads.json `format` (pdf, jpg, png, doc, docx); records without one are PDFs."""
    return (download or {}).get("format") or "pdf"


def rel_path(doc_id, ext="pdf"):
    return f"{HF_PREFIX}/{doc_year(doc_id)}/{doc_id}.{ext}"


def doc_path(doc_id, ext="pdf"):
    return LFS_DIR / rel_path(doc_id, ext)


def identifier_for(doc_id):
    return IA_PREFIX + doc_id


def active(rec):
    """A circular that should have a PDF, an archive item and a Wayback link."""
    return rec.get("status") == "ok" and rec.get("doc_id") and rec.get("fid") and not rec.get("removed_utc")


def is_new(rec, window):
    """The daily run's scope: an active circular first seen within the window (e.g. 7d)."""
    return bool(active(rec)) and within(rec.get("first_seen_utc"), window)


def group_by_fid(circulars):
    """{fid: [active circulars sorted by nid]}"""
    groups = defaultdict(list)
    for nid in sorted(circulars):
        rec = circulars[nid]
        if active(rec):
            groups[rec["fid"]].append(rec)
    return dict(sorted(groups.items()))


# ---------------------------------------------------------------- files

def sha1_file(path):
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


FORMATS = ("pdf", "jpg", "png", "doc", "docx")  # file types PMC posts; anything else is failed:unknown_type


def file_format(path):
    """One of FORMATS, from the file's first bytes (not its name), or None (e.g. an HTML error page)."""
    with path.open("rb") as f:
        head = f.read(1024)
    if b"%PDF" in head:
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "doc"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as z:
                if any(n.startswith("word/") for n in z.namelist()):
                    return "docx"
        except zipfile.BadZipFile:
            pass
    return None


def looks_like_pdf(path):
    return file_format(path) == "pdf"


def hardlink(src, dst):
    """Make dst the same file as src (hardlink, or copy across devices)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if os.path.samefile(src, dst):
            return
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def link_file(doc_id, ext="pdf"):
    """Create/repair input/documents/.../pdfs/<YYYY>/<doc_id>.<ext> -> LFS/punePMC/.../<doc_id>.<ext>"""
    link = LINKS_DIR / doc_year(doc_id) / f"{doc_id}.{ext}"
    link.parent.mkdir(parents=True, exist_ok=True)
    for old in link.parent.glob(f"{doc_id}.*"):  # the file changed type (e.g. a JPEG replaced by a PDF)
        if old != link and old.is_symlink():
            old.unlink()
    target = os.path.relpath(doc_path(doc_id, ext), link.parent)
    if link.is_symlink() and os.readlink(link) == target:
        return
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def total_size(r):
    if r.status_code == 206:
        m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
        return int(m.group(1)) if m else None
    length = r.headers.get("Content-Length")
    return int(length) if length else None


def fetch_file(url, dest, retries=5):
    """Download url via dest.part, resuming on retries. Returns (info, error).

    The file is saved as dest with the extension of its actual type (info["format"], info["path"]);
    error is "404", "unknown_type" (not one of FORMATS) or the last exception.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    error = ""
    for attempt in range(1, retries + 1):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with SESSION.get(url, headers=headers, stream=True, timeout=120) as r:
                if r.status_code == 404:
                    return None, "404"
                if r.status_code == 416:  # stale .part, start over
                    part.unlink()
                    continue
                r.raise_for_status()
                if r.status_code == 200:
                    offset = 0  # server ignored Range
                total = total_size(r)
                info = {"last_modified": r.headers.get("Last-Modified"), "etag": r.headers.get("ETag")}
                with part.open("ab" if offset else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)

            size = part.stat().st_size
            if total and size != total:
                raise OSError(f"size mismatch {size} vs {total}")
            fmt = file_format(part)
            if not fmt:
                part.unlink()
                return None, "unknown_type"
            final = dest.with_suffix(f".{fmt}")
            part.replace(final)
            info.update(size=size, format=fmt, path=final)
            return info, ""
        except (requests.RequestException, OSError) as e:
            error = str(e)
            print(f"\tattempt {attempt}/{retries} failed: {error}")
            time.sleep(5 * attempt)
    return None, error


def ensure_file(doc_id, download):
    """Return a local path for doc_id's current file, fetching it if LFS doesn't have it (e.g. on CI).

    Order: any local copy of the same fid (hardlink) -> HF dataset -> PMC.
    """
    dest = doc_path(doc_id, file_ext(download))
    if dest.exists():
        return dest
    paths = download.get("paths") or [rel_path(doc_id, file_ext(download))]
    for p in paths:
        if (LFS_DIR / p).exists():
            hardlink(LFS_DIR / p, dest)
            return dest

    from huggingface_hub import hf_hub_download

    repo_id, token = hf_settings()
    for p in paths:
        try:
            hf_hub_download(repo_id=repo_id, repo_type="dataset", filename=p, local_dir=LFS_DIR, token=token)
        except Exception:
            continue
        if (LFS_DIR / p).exists() and sha1_file(LFS_DIR / p) == download.get("sha1"):
            if LFS_DIR / p != dest:
                hardlink(LFS_DIR / p, dest)
            return dest

    info, error = fetch_file(download["url"], dest)
    if info and info["path"] == dest and sha1_file(dest) == download.get("sha1"):
        return dest
    if info and info["path"].exists():
        info["path"].unlink()
    raise RuntimeError(f"cannot get the file for {doc_id} (not on HF, PMC: {error or 'sha1 differs'})")
