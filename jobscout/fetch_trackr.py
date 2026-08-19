#!/usr/bin/env python3
"""Fetch UK internship cycles from The-Trackr's open JSON API into a JSON file for the skill.

job-scout used to discover roles only from Gmail digests (Gradcracker / Bright Network).
The-Trackr catalogs the exact cycles Ramy is targeting right now - UK Tech summer-2027 and
UK Finance summer-2027 - as structured records, so they can skip email parsing entirely and
enter the skill at its filter/score step as ready-made job records.

This is a READ-ONLY discovery source. Trackr's own per-account status tracking is deliberately
not used: state.sqlite plus the local-hub boards stay the single source of truth for
apply/skip/stage. No auth, no account, no writes back to Trackr.

Like fetch_mail.py this runs as plain Python from run.sh BEFORE claude starts, so the scan
itself never makes a network call to Trackr and no new WebFetch domains are allow-listed.

Output (--out): {generated, source, boards:[...], fetched, new, roles:[...], errors:[...]}
Each role: {source:"Trackr", title, company, url, careers_site, location, category,
            categories, stage_hint, deadline, opening, last_year_opening, open,
            company_desc, board, tab, trackr_id, cv, cover_letter}

MOST CYCLES ARE NOT OPEN YET. The board is a calendar as much as a job list: as of
2026-08-02 only 52 of 716 records carry an apply `url` (the rest have opened in past years
and are listed with `lastYearOpening` as a hint). Unopened roles are kept - with url "" -
because that is exactly what makes the "it just opened" signal work:

  seeded while closed -> job_key is the sig:trackr|company|title fallback
  opens, gains a url  -> job_key becomes the url key, so it reads as NEW and surfaces
                         on the very next run, with a real apply link.

The company careers site is NEVER used as the role url: job_key drops the query string, so
every unopened role at one company would collapse onto a single careers-page key and dedup
would silently swallow them. It travels in its own `careers_site` field instead.

By default --out writes only roles state.sqlite has not seen before (it shells out to
state.py filter-jobs, the same way fetch_mail.py shells out to state.py since). That keeps
the daily file small - the full boards are ~700 roles and would bloat the scan's context for
no gain. Pass --all to write everything regardless.

Exit codes: 0 ok (even with zero new roles) - 5 network/API failure (retryable).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

PROJ = Path.home() / "Documents" / "job-scout"
STATE_PY = PROJ / "state.py"

API = "https://api.the-trackr.com/programmes"
TIMEOUT = 45
USER_AGENT = "job-scout/1.0 (personal job tracker; +local use)"

# The cycles Ramy is actually targeting. Tech is primary; finance is narrowed by category.
BOARDS = [
    {"region": "UK", "industry": "Tech", "season": "2027", "type": "summer-internships"},
    {"region": "UK", "industry": "Finance", "season": "2027", "type": "summer-internships"},
]

# Finance is 430 roles of which most are off-target (Middle Market, Accounting, Pensions...).
# Keep only quant/data/IB at brand-name firms; the scorer narrows further per prefs.md.
FINANCE_CATEGORIES = {"Trading and Quant", "Buy-Side", "Bulge Bracket", "Elite Boutique"}

# "Promoted" is a paid placement flag on the site, not a real category - never let it become
# a role's primary category, and never let it qualify a finance role on its own.
PSEUDO_CATEGORIES = {"Promoted"}

# Dropped from apply links. Everything else is kept: some ATS carry the job id in the query
# (?token=, ?pid=, ?gh_jid=), and stripping the whole query string would both break those links
# and collapse every role at that company onto one dedup key. Shared with state.job_key so the
# link and the key always agree on what counts as noise.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import TRACKING_PARAMS  # noqa: E402

COMPANY_DESC_CHARS = 700  # enough to tailor a CV against; keeps the daily file small


def die(code: int, message: str) -> None:
    print(f"fetch_trackr: {message}", file=sys.stderr)
    sys.exit(code)


def board_label(board: dict) -> str:
    return f"{board['region']} {board['industry']} {board['season']}"


def fetch_board(board: dict) -> list[dict]:
    url = f"{API}?{urllib.parse.urlencode(board)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(f"expected a JSON array, got {type(data).__name__}")
    return data


# Link shorteners: the SAME role arrives as `grnh.se/103h8wnc2us` from one Trackr refresh and
# as `job-boards.greenhouse.io/mwinternshipprogram/jobs/8598324002` from another, and job_key is
# computed on whichever string arrived, so the two never dedup. That is exactly how Marshall Wace
# got a second card on 2026-08-10 and had its JD-matched CV overwritten by a title-only rebuild.
# Resolving here means only the canonical URL is ever stored.
SHORTENERS = ("grnh.se", "bit.ly", "lnkd.in", "t.co", "ow.ly", "tinyurl.com")


def resolve_short_link(url: str) -> str:
    """Follow a shortener to its real destination. Best effort: on any failure keep the original."""
    try:
        host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
        if host not in SHORTENERS:
            return url
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.url or url
    except Exception:
        return url


def strip_tracking(url: str | None) -> str:
    """Drop analytics params from an apply link, keep the ones the ATS actually needs."""
    if not url or not url.startswith("http"):
        return ""
    parts = urllib.parse.urlsplit(url)
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(kept), parts.fragment))


def prettify(company_id: str) -> str:
    return " ".join(w.capitalize() for w in (company_id or "").split("-")) or "Unknown"


def day(ts: str | None) -> str:
    """'2026-07-21T00:00:00.000Z' -> '2026-07-21'."""
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 else ""


def primary_category(categories: list) -> str:
    real = [c for c in categories if c not in PSEUDO_CATEGORIES]
    return (real or categories or [""])[0]


def normalize(rec: dict, board: dict) -> dict:
    company = rec.get("company") or {}
    categories = [c for c in (rec.get("categories") or []) if c]
    apply_url = strip_tracking(resolve_short_link(rec.get("url") or ""))
    desc = (company.get("description") or "").strip()
    # Only open roles can produce a CV, and descriptions are ~1 KB each - carry them only
    # where they earn their place.
    if desc and (len(desc) > COMPANY_DESC_CHARS):
        desc = desc[:COMPANY_DESC_CHARS].rstrip() + "..."
    locations = [l for l in (rec.get("locations") or []) if l]
    return {
        "source": "Trackr",
        "title": (rec.get("name") or "").strip(),
        "company": (company.get("name") or "").strip() or prettify(rec.get("companyId", "")),
        "url": apply_url,
        "careers_site": strip_tracking(company.get("careersSite")),
        # The API leaves `locations` empty on every record today; region is the honest
        # fallback and is never invented (the board itself is region-scoped).
        "location": ", ".join(locations) or board["region"],
        "category": primary_category(categories),
        "categories": categories,
        "stage_hint": "internship",
        "deadline": day(rec.get("closingDate")),
        "opening": day(rec.get("openingDate")),
        "last_year_opening": day(rec.get("lastYearOpening")),
        "open": bool(apply_url),
        "company_desc": desc if apply_url else "",
        "board": board_label(board),
        "tab": board["type"],
        "trackr_id": rec.get("id"),
        "cv": rec.get("cv"),
        "cover_letter": rec.get("coverLetter"),
    }


def wanted(role: dict, board: dict) -> bool:
    if not role["title"]:
        return False
    if board["industry"] != "Finance":
        return True
    return bool(FINANCE_CATEGORIES & set(role["categories"]))


def collect(errors: list) -> list[dict]:
    """Every wanted role across all boards, deduped on trackr_id."""
    roles: dict[str, dict] = {}
    for board in BOARDS:
        try:
            raw = fetch_board(board)
        except Exception as e:  # one bad board must not sink the other
            errors.append(f"{board_label(board)}: {e!r}")
            continue
        for rec in raw:
            role = normalize(rec, board)
            if wanted(role, board):
                roles.setdefault(role["trackr_id"] or role["url"] or role["title"], role)
    return list(roles.values())


def filter_new(roles: list[dict], errors: list) -> list[dict]:
    """Drop roles state.sqlite has already seen (same job_key rule as the skill's Step 7)."""
    if not roles:
        return []
    r = subprocess.run([sys.executable, str(STATE_PY), "filter-jobs"],
                       input=json.dumps(roles), capture_output=True, text=True)
    if r.returncode != 0:
        errors.append(f"state.py filter-jobs failed ({r.stderr.strip()}); "
                      "writing the unfiltered board")
        return roles
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        errors.append(f"state.py filter-jobs returned unreadable JSON ({e}); "
                      "writing the unfiltered board")
        return roles


def seed(roles: list[dict]) -> int:
    """Record the whole current backlog as 'seeded' so the first real run isn't an avalanche."""
    payload = [dict(r, status="seeded", score=None, stage=r["stage_hint"],
                    reason="seeded from Trackr backlog") for r in roles]
    r = subprocess.run([sys.executable, str(STATE_PY), "record-jobs"],
                       input=json.dumps(payload), capture_output=True, text=True)
    if r.returncode != 0:
        die(5, f"state.py record-jobs failed: {r.stderr.strip()}")
    try:
        return json.loads(r.stdout).get("inserted", 0)
    except json.JSONDecodeError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="write JSON here (default: print a summary only)")
    ap.add_argument("--check", action="store_true",
                    help="print per-board counts and exit, writing nothing")
    ap.add_argument("--seed", action="store_true",
                    help="one-time: record the whole current backlog as status 'seeded'")
    ap.add_argument("--all", action="store_true",
                    help="write every role, not just ones state.sqlite hasn't seen")
    args = ap.parse_args()

    errors: list[str] = []

    if args.check:
        total = 0
        for board in BOARDS:
            try:
                raw = fetch_board(board)
            except Exception as e:
                print(f"FAIL: {board_label(board)}: {e!r}", file=sys.stderr)
                errors.append(str(e))
                continue
            kept = [r for r in (normalize(x, board) for x in raw) if wanted(r, board)]
            live = sum(1 for r in kept if r["open"])
            total += len(kept)
            print(f"ok: {board_label(board)} {board['type']} - "
                  f"{len(raw)} listed, {len(kept)} kept, {live} open now")
        if errors:
            die(5, f"{len(errors)} board(s) failed")
        print(f"ok: {total} roles across {len(BOARDS)} boards")
        return 0

    roles = collect(errors)
    if not roles and errors:
        die(5, "; ".join(errors))

    if args.seed:
        inserted = seed(roles)
        print(f"seeded {inserted} of {len(roles)} Trackr roles "
              f"(status 'seeded'; already-known roles left untouched)")
        for e in errors:
            print(f"  error: {e}", file=sys.stderr)
        return 0

    fetched = len(roles)
    if not args.all:
        roles = filter_new(roles, errors)

    doc = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "trackr",
        "boards": [f"{board_label(b)} {b['type']}" for b in BOARDS],
        "fetched": fetched,
        "new": len(roles),
        "roles": roles,
        "errors": errors,
    }
    summary = (f"fetched {fetched} roles, {len(roles)} new, "
               f"{sum(1 for r in roles if r['open'])} of those open now "
               f"({len(errors)} errors)")
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
        print(f"{summary} -> {out}")
    else:
        print(summary)
    for e in errors:
        print(f"  error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
