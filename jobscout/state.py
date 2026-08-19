#!/usr/bin/env python3
"""State + dedup + tracker store for Job Scout (SQLite, stdlib only).

Subcommands (most read JSON on stdin / write JSON or markdown on stdout):

    init                      create the database
    since                     print a Gmail date filter (e.g. "after:2026/05/19") from last run
    set-last-run              record today as the last successful run
    filter-threads            stdin: [{"thread_id":..,"source":..,"subject":..}]  -> stdout: unseen ones
    record-threads            stdin: same shape -> insert (idempotent)
    filter-jobs               stdin: [job,...] -> stdout: jobs not seen before (job_key added)
    record-jobs               stdin: [job,...] -> upsert (keeps highest score), idempotent
    render-tracker            stdout: Markdown tracker table
    set-status JOB_KEY STATUS stdout: update a job's status (e.g. applied)

A "job" dict uses: title, company, source, url, location, stage, score, reason, category, status.
Dedup key = the real job URL when present (Gradcracker etc.), normalised to host + path + any
non-tracking query params; otherwise a source|company|title signature (LinkedIn links are Gmail
permalinks, which aren't stable per-job, so we fall back).

`status` is the single source of truth for where an application stands, for BOTH boards:

    surfaced        seen and scored, nothing done yet (default)
    applied         sent
    interviewing    progressed past the first screen
    offer           offer in hand
    rejected        declined, at any stage
    parked          deliberately held back, e.g. waiting on another outcome at the same
                    employer, so it is neither dead nor actionable
    not-interested  dismissed by Ramy
    seeded          Trackr backlog, known so it never avalanches, never actually surfaced

`applied_at` is when it was sent and never moves once set. `status_at` is when the status last
changed, which is what the "no word in N weeks" nudge is measured from. render-tracker leaves
`seeded` and `not-interested` out of the table and counts them in the callout instead.

`tailoring` records what the CV was actually built from, so the boards can show it instead of it
living only in the prose of a report:

    jd          the employer's own posting text was fetched and used
    search      the JD was reconstructed from web search, the posting itself was unreachable
    title-only  no JD at any rung: built from the title, category and profile. NOT fabricated,
                but it needs a manual pass before the CV is sent.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

DEFAULT_DB = Path.home() / "Documents" / "job-scout" / "state.sqlite"

# Analytics params: they vary run to run for the same role, so they must never reach a dedup
# key or a link. fetch_trackr.py imports this list so both agree on what counts as noise.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "source", "gh_src", "lever-source", "ref", "referrer", "trk", "trackr",
    "gclid", "fbclid", "mc_cid", "mc_eid", "_ga",
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS threads (
            thread_id  TEXT PRIMARY KEY,
            source     TEXT,
            subject    TEXT,
            first_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS jobs (
            job_key    TEXT PRIMARY KEY,
            title      TEXT,
            company    TEXT,
            source     TEXT,
            url        TEXT,
            location   TEXT,
            stage      TEXT,
            score      INTEGER,
            reason     TEXT,
            status     TEXT DEFAULT 'surfaced',
            first_seen TEXT,
            applied_at TEXT,
            category   TEXT
        );
        """
    )
    # Migrations for databases created before a column existed. Adding a nullable column is
    # cheap and idempotent, so this can run on every connect; email-sourced rows keep NULL.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for column, decl in (("category", "TEXT"), ("status_at", "TEXT"), ("tailoring", "TEXT")):
        if column not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {decl}")
    conn.commit()


def job_key(rec: dict) -> str:
    url = (rec.get("url") or "").strip()
    if url and "mail.google.com" not in url and url.startswith("http"):
        parts = urlsplit(url)
        key = f"{parts.netloc}{parts.path}".rstrip("/").lower()
        # Keep the query, minus tracking noise. Several ATS serve a whole company's roles from
        # one path and identify the role only in the query (greenhouse .../embed/job_app?token=,
        # campusjobs.mlp.com/careers?pid=, ...?gh_jid=), so dropping it collapses distinct roles
        # onto one key and silently swallows them. Params are sorted so ordering can't fork a key.
        query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in TRACKING_PARAMS)
        return f"{key}?{urlencode(query)}".lower() if query else key
    base = "|".join(
        [(rec.get("source") or ""), (rec.get("company") or ""), (rec.get("title") or "")]
    ).lower()
    return "sig:" + re.sub(r"[^a-z0-9]+", "-", base).strip("-")


# ── cross-source duplicate detection ────────────────────────────────────────────────
# job_key alone cannot catch these, because it is derived from the URL (or, failing that,
# from source|company|title). The same opportunity arriving from two sources gets two keys.
# Measured on the 2026-08-10 run: Marshall Wace slipped through as `grnh.se/103h8wnc2us`
# against the canonical `job-boards.greenhouse.io/...` key, and Palantir slipped through
# because a LinkedIn alert said "Palantir Technologies" where Trackr said "Palantir".
#
# So identity is computed independently of URL and source: normalised company + normalised
# title tokens. It runs ALONGSIDE job_key and never replaces it, so no existing row is
# re-keyed and nothing re-surfaces.

# Only genuine noise. Note what is deliberately NOT here: 'graduate', 'internship', 'intern',
# 'programme', 'summer'. Revolut runs an "Internship Programme 2027: Software Engineer (Python)"
# AND a "Graduate Programme 2027: Software Engineer (Python)"; dropping those words collapses two
# different programmes into one and loses a real role.
_ID_NOISE = {"the", "a", "an", "of", "and", "for", "in", "at", "to", "with",
             "2024", "2025", "2026", "2027", "2028", "2029"}

_COMPANY_SUFFIX = re.compile(
    r"\b(ltd|llp|llc|inc|plc|limited|corp|corporation|co|group|holdings|"
    r"technologies|technology|international|partners|capital management)\b")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def company_ident(company: str) -> str:
    """'Palantir', 'Palantir Technologies' and 'Crédit Agricole CIB' vs 'Credit Agricole CIB'
    all have to land on one value, or the same role from two feeds reads as two roles."""
    n = re.sub(r"\s+", " ", _COMPANY_SUFFIX.sub("", _norm(company))).strip()
    return n.replace(" ", "")


def role_identity(company: str, title: str) -> str:
    """A URL-free, source-free fingerprint for 'is this the same opportunity'."""
    cid = company_ident(company)
    ctoks = set(_norm(company).split())
    # a feed that repeats the employer inside the title ("Tencent Agent Development
    # Internship") must fingerprint the same as one that does not
    toks = {t for t in _norm(title).split()
            if t and t not in _ID_NOISE and t not in ctoks}
    return f"{cid}|{' '.join(sorted(toks))}" if cid and toks else ""


def read_stdin_json() -> list:
    raw = sys.stdin.read().strip()
    if not raw:
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


def cmd_since(conn, _args) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key='last_run'").fetchone()
    if row and row["value"]:
        last = datetime.strptime(row["value"], "%Y-%m-%d").date()
    else:
        last = date.today() - timedelta(days=2)
    # one day of overlap so nothing slips through the cracks; dedup handles repeats
    start = last - timedelta(days=1)
    print(f"after:{start.strftime('%Y/%m/%d')}")


def cmd_set_last_run(conn, _args) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('last_run',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (date.today().strftime("%Y-%m-%d"),),
    )
    conn.commit()
    print(date.today().strftime("%Y-%m-%d"))


def cmd_filter_threads(conn, _args) -> None:
    known = {r["thread_id"] for r in conn.execute("SELECT thread_id FROM threads")}
    out = [t for t in read_stdin_json() if t.get("thread_id") not in known]
    print(json.dumps(out))


def cmd_record_threads(conn, _args) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for t in read_stdin_json():
        if not t.get("thread_id"):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO threads(thread_id,source,subject,first_seen) VALUES(?,?,?,?)",
            (t["thread_id"], t.get("source"), t.get("subject"), now),
        )
        n += 1
    conn.commit()
    print(json.dumps({"recorded": n}))


def cmd_filter_jobs(conn, _args) -> None:
    known = {r["job_key"] for r in conn.execute("SELECT job_key FROM jobs")}
    # Identity is only built from rows that were actually surfaced. Seeded Trackr backlog is
    # excluded on purpose: a backlog row must not suppress the same role when it finally opens.
    idents = {}
    for r in conn.execute("SELECT job_key,company,title FROM jobs WHERE status!='seeded'"):
        i = role_identity(r["company"], r["title"])
        if i:
            idents.setdefault(i, r["job_key"])
    out, dropped = [], []
    seen_this_batch = {}
    for rec in read_stdin_json():
        k = job_key(rec)
        if k in known:
            continue
        ident = role_identity(rec.get("company", ""), rec.get("title", ""))
        hit = idents.get(ident) or seen_this_batch.get(ident) if ident else None
        if hit:
            # Same opportunity, different URL or different source spelling of the company.
            dropped.append({"title": rec.get("title"), "company": rec.get("company"),
                            "duplicate_of": hit})
            continue
        rec["job_key"] = k
        if ident:
            seen_this_batch[ident] = k
        out.append(rec)
    # stderr so the skill's `| $STATE filter-jobs` pipe keeps returning clean JSON on stdout
    if dropped:
        print(f"filter-jobs: dropped {len(dropped)} cross-source duplicate(s):", file=sys.stderr)
        for d in dropped:
            print(f"  {d['company']} - {d['title']}  (already have {d['duplicate_of']})",
                  file=sys.stderr)
    print(json.dumps(out))


# Furthest through the process wins when two rows for one role are merged. 'rejected' beats
# 'parked' because it is a fact about the outcome; 'parked' is only a holding decision.
_STATUS_RANK = ["seeded", "surfaced", "not-interested", "parked", "applied",
                "rejected", "interviewing", "offer"]
_TAILOR_RANK = ["title-only", "search", "jd"]


def cmd_merge_dupes(conn, args) -> None:
    """Collapse identity groups into one row each, keeping the best of everything.

    Only ever run deliberately, never from a scan: `find-dupes` reports and a human decides.
    Survivor is the row with a real URL key (a sig: key is a fallback and the weaker identity);
    ties break on the earliest first_seen so the original sighting is what persists.
    """
    groups = {}
    for r in conn.execute("SELECT * FROM jobs WHERE status!='seeded'"):
        i = role_identity(r["company"], r["title"])
        if i:
            groups.setdefault(i, []).append(dict(r))

    board_path = Path.home()/"Documents"/"job-scout"/"applyboard"/"roles.json"
    board = json.loads(board_path.read_text()) if board_path.is_file() else None
    merged = 0
    for ident, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (r["job_key"].startswith("sig:"), r["first_seen"] or ""))
        keep, drop = rows[0], rows[1:]
        best = {
            "status": max((r["status"] for r in rows),
                          key=lambda s: _STATUS_RANK.index(s) if s in _STATUS_RANK else 0),
            "score": max((r["score"] or 0) for r in rows) or None,
            "tailoring": max((r["tailoring"] for r in rows if r["tailoring"]),
                             key=lambda t: _TAILOR_RANK.index(t) if t in _TAILOR_RANK else 0,
                             default=None),
            "applied_at": min((r["applied_at"] for r in rows if r["applied_at"]), default=None),
            "first_seen": min((r["first_seen"] for r in rows if r["first_seen"]), default=None),
        }
        print(f"  {ident}\n    KEEP {keep['job_key'][:58]}")
        for d in drop:
            print(f"    drop {d['job_key'][:58]}  [{d['status']}]")
        print(f"    -> status={best['status']} score={best['score']} tailoring={best['tailoring']}")
        if not args.dry_run:
            conn.execute("UPDATE jobs SET status=?,score=?,tailoring=?,applied_at=?,first_seen=? "
                         "WHERE job_key=?",
                         (best["status"], best["score"], best["tailoring"],
                          best["applied_at"], best["first_seen"], keep["job_key"]))
            for d in drop:
                conn.execute("DELETE FROM jobs WHERE job_key=?", (d["job_key"],))
            # a board card pointing at a deleted key would silently lose its status link
            if board:
                dropped = {d["job_key"] for d in drop}
                for bucket in ("open", "verify", "dead", "soon"):
                    for e in board.get(bucket, []):
                        ks = e.get("job_keys") or []
                        if dropped & set(ks):
                            e["job_keys"] = sorted({keep["job_key"]} |
                                                   (set(ks) - dropped))
        merged += len(drop)

    if args.dry_run:
        print(f"\nDRY RUN: would remove {merged} duplicate row(s)")
    else:
        conn.commit()
        if board:
            board_path.write_text(json.dumps(board, indent=1, ensure_ascii=False))
        print(f"\nremoved {merged} duplicate row(s); board job_keys repointed")


def cmd_find_dupes(conn, _args) -> None:
    """Audit rows already in the table. Reports, never merges: the same rules that correctly
    pair 'Palantir' with 'Palantir Technologies' would wrongly pair KPMG's Technology
    Engineering with its Technology Consulting, so a human decides."""
    groups = {}
    for r in conn.execute(
            "SELECT job_key,company,title,source,status,score FROM jobs WHERE status!='seeded'"):
        i = role_identity(r["company"], r["title"])
        if i:
            groups.setdefault(i, []).append(dict(r))
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"{len(dupes)} identity group(s) with more than one row\n")
    for ident, rows in sorted(dupes.items()):
        print(f"  {ident}")
        for r in rows:
            print(f"    [{r['status']:<9}] {r['source']:<16} {(r['title'] or '')[:44]:<44} {r['job_key'][:44]}")
        print()


def cmd_record_jobs(conn, _args) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for rec in read_stdin_json():
        k = rec.get("job_key") or job_key(rec)
        existing = conn.execute("SELECT score FROM jobs WHERE job_key=?", (k,)).fetchone()
        score = rec.get("score")
        if existing is not None:
            # keep the highest score we've seen; don't clobber status
            best = max(filter(lambda v: v is not None, [existing["score"], score]), default=None)
            conn.execute("UPDATE jobs SET score=? WHERE job_key=?", (best, k))
        else:
            # 'seeded' (Trackr backlog) is the only caller that overrides the default; it
            # marks a role as known without claiming the scan ever surfaced it.
            status = rec.get("status") or "surfaced"
            conn.execute(
                """INSERT INTO jobs(job_key,title,company,source,url,location,stage,score,reason,status,first_seen,category,tailoring)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    k, rec.get("title"), rec.get("company"), rec.get("source"), rec.get("url"),
                    rec.get("location"), rec.get("stage"), score, rec.get("reason"), status, now,
                    rec.get("category"), rec.get("tailoring"),
                ),
            )
            n += 1
        # tailoring can arrive on a later pass (a CV rebuilt against a real JD), so let it update
        if rec.get("tailoring"):
            conn.execute("UPDATE jobs SET tailoring=? WHERE job_key=?", (rec["tailoring"], k))
    conn.commit()
    print(json.dumps({"inserted": n}))


def cmd_set_status(conn, args) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    # applied_at is the date you SENT it and must never move; status_at is when the state last
    # changed, which is what "no word in 6 weeks" is measured from.
    applied_at = now if args.status == "applied" else None
    conn.execute(
        "UPDATE jobs SET status=?, status_at=?, applied_at=COALESCE(applied_at,?) WHERE job_key=?",
        (args.status, now, applied_at, args.job_key),
    )
    conn.commit()
    print(json.dumps({"job_key": args.job_key, "status": args.status, "status_at": now}))


def cmd_render_tracker(conn, _args) -> None:
    # Two states are deliberately kept out of the table: 'seeded' (Trackr backlog the scan never
    # surfaced - ~700 rows would drown the real tracker) and 'not-interested' (roles Ramy dropped
    # on the board). Both are counted in the callout and stay browsable at localhost:7777/tracker.
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status NOT IN ('seeded','not-interested') "
        "ORDER BY date(first_seen) DESC, score DESC"
    ).fetchall()
    counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM jobs WHERE status IN ('seeded','not-interested') "
        "GROUP BY status").fetchall())
    seeded, dropped = counts.get("seeded", 0), counts.get("not-interested", 0)
    total = len(rows)
    applied = sum(1 for r in rows if r["status"] == "applied")
    backlog = f" · {seeded} Trackr roles seeded (not yet open)" if seeded else ""
    backlog += f" · {dropped} not interested" if dropped else ""
    lines = [
        "# Job Scout — Application Tracker",
        "",
        f"> [!info] {total} roles surfaced · {applied} marked applied{backlog} · updated {date.today():%Y-%m-%d}",
        "> Mark a role applied with: `state.py set-status \"<job_key>\" applied`",
        "",
        "| First seen | Fit | Role | Company | Category | Stage | Status | Source |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        seen = (r["first_seen"] or "")[:10]
        title = (r["title"] or "").replace("|", "\\|")
        company = (r["company"] or "").replace("|", "\\|")
        link = f"[{title}]({r['url']})" if r["url"] else title
        lines.append(
            f"| {seen} | {r['score'] if r['score'] is not None else ''} | {link} | "
            f"{company} | {r['category'] or ''} | {r['stage'] or ''} | {r['status'] or ''} | "
            f"{r['source'] or ''} |"
        )
    if not rows:
        lines.append("| _(nothing yet)_ | | | | | | | |")
    print("\n".join(lines))



def cmd_add(conn, args) -> None:
    """Add one role by hand.

    For anything a source never surfaced: a referral, something a friend sent,
    a posting you found yourself. It lands in the tracker exactly like a fetched
    role, so the board and the CV builder treat it identically.
    """
    if not args.url and not args.title:
        raise SystemExit("add: need at least --url or --title")
    rec = {
        "title": args.title,
        "company": args.company,
        "url": args.url,
        "location": args.location,
        "source": args.source or "manual",
        "stage": args.stage,
        "score": args.score,
        "category": args.category,
        "status": "surfaced",
    }
    # job_key() already canonicalises the URL and drops tracking parameters, so a
    # link copied out of an email does not create a second row for a known role.
    rec["job_key"] = job_key(rec)
    now = datetime.now().isoformat(timespec="seconds")
    if conn.execute("SELECT 1 FROM jobs WHERE job_key=?", (rec["job_key"],)).fetchone():
        print(json.dumps({"ok": False, "error": "already tracked", "job_key": rec["job_key"]}))
        return
    conn.execute(
        """INSERT INTO jobs(job_key,title,company,source,url,location,stage,score,reason,status,first_seen,category)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec["job_key"], rec["title"], rec["company"], rec["source"], rec["url"],
         rec["location"], rec["stage"], rec["score"], "added by hand", rec["status"],
         now, rec["category"]),
    )
    conn.commit()
    print(json.dumps({"ok": True, "job_key": rec["job_key"]}))


COMMANDS = {
    "init": lambda c, a: None,  # init handled in main (always runs init_db)
    "since": cmd_since,
    "set-last-run": cmd_set_last_run,
    "filter-threads": cmd_filter_threads,
    "record-threads": cmd_record_threads,
    "filter-jobs": cmd_filter_jobs,
    "record-jobs": cmd_record_jobs,
    "add": cmd_add,
    "set-status": cmd_set_status,
    "render-tracker": cmd_render_tracker,
    "find-dupes": cmd_find_dupes,
    "merge-dupes": cmd_merge_dupes,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=list(COMMANDS.keys()))
    ap.add_argument("job_key", nargs="?", help="job_key (for set-status)")
    ap.add_argument("status", nargs="?", help="new status (for set-status)")
    ap.add_argument("--dry-run", action="store_true", help="merge-dupes: report only")
    ap.add_argument("--url", help="add: link to the posting")
    ap.add_argument("--title", help="add: role title")
    ap.add_argument("--company", help="add: employer")
    ap.add_argument("--location", help="add: location")
    ap.add_argument("--source", help="add: where you found it (default: manual)")
    ap.add_argument("--stage", help="add: internship | graduate | placement")
    ap.add_argument("--category", help="add: your own grouping label")
    ap.add_argument("--score", type=int, help="add: fit 0-100, if you want to rank it")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path (default: ~/Documents/job-scout/state.sqlite)")
    args = ap.parse_args()

    conn = connect(Path(args.db))
    init_db(conn)  # always safe; creates tables if missing
    if args.command != "init":
        COMMANDS[args.command](conn, args)
    else:
        print(f"initialised {args.db}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
