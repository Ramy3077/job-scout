#!/usr/bin/env python3
"""Push newly surfaced strong matches from state.sqlite onto the Apply Board.

The Apply Board (applyboard/roles.json) is the "what do I do next" list; state.sqlite is the
record of every role ever seen. Until now the daily run only wrote the second, so a strong
match with a CV built landed in the vault and the tracker and the Apply Board never heard
about it. This closes that gap as the last step of a run.

WHY job_key ALONE CANNOT DEDUP THIS
-----------------------------------
The two stores are keyed at different granularities and from different sources:

  * The board is organised by APPLICATION, the database by LISTING. BlackRock's
    "Application 1 of 2 - Technology: Software Engineering + Analytics & Modeling" is one
    board card covering two functions that are two separate rows in the database.
  * The same opportunity arrives under different URLs from different sources, and each URL
    is a different job_key: BlackRock is `gradcracker.com/hub/807/...` from the Gmail digest
    but `blackrock.tal.net/...` from Trackr; Revolut is a `sig:linkedin-...` fallback key
    from the mail digest but `revolut.com/careers?text=...` from Trackr; DRW arrived as the
    short link `grnh.se/dfw7paj51us` while the board carries the resolved Greenhouse URL.

Measured on 2026-08-04: 6 of the 13 cards then on the board resolved to no database row at
all. A naive `job_key in board` check would have re-added every one of them.

SO: each board entry carries a LIST of `job_keys`. Matching runs job_key first, then falls
back to normalised company plus role-token overlap. A fuzzy hit does not create a card - it
ATTACHES the new key to the existing entry, so the board learns the alias and the next run
matches exactly. Ambiguity therefore decays instead of compounding.

SAFETY: this only ever ADDS cards and APPENDS keys. It never edits a note, never flips
`applied`, never reorders, never deletes. Entries you have marked applied are read-only.
The board carries editorial judgement the pipeline cannot generate (the BlackRock "max three
functions across two applications" rule, why Technology Operations was ruled out) and none
of that is machine-writable.

Usage:
    sync_applyboard.py [--dry-run] [--threshold 75] [--db PATH] [--board PATH]

Exit codes: 0 ok (including nothing to do) - 4 board file missing or unreadable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import date
from pathlib import Path

import config

PROJ = Path(__file__).resolve().parent
DEFAULT_DB = config.DB
DEFAULT_BOARD = config.BOARD
CV_DIR = config.CV_DIR
CV_PREFIX = config.CV_PREFIX

# Statuses that must never reach the board.
SKIP_STATUS = {"seeded", "not-interested"}

# Tokens that carry no identity: every role here is a summer-2027 internship, so these words
# are noise when deciding whether two titles are the same opportunity.
STOPWORDS = {
    "internship", "internships", "intern", "programme", "program", "summer", "the", "of",
    "and", "a", "an", "for", "in", "at", "to", "2026", "2027", "2028", "application",
    "off", "cycle", "offcycle", "student", "students", "uk", "london", "emea", "graduate",
}

# Company spellings that differ between sources but mean one employer.
COMPANY_ALIASES = {
    "de shaw": "deshaw", "d e shaw": "deshaw", "deshaw co": "deshaw", "de shaw co": "deshaw",
    "chicago trading": "chicagotrading", "chicago trading company": "chicagotrading",
    "castleton commodities": "castleton", "castleton commodities international": "castleton",
    "citadel securities": "citadelsecurities",
    "millennium": "millennium", "millennium management": "millennium",
    "gsa": "gsacapital", "gsa capital": "gsacapital",
    "aquatic capital": "aquatic", "aquatic capital management": "aquatic",
    "quadrature": "quadrature", "quadrature capital": "quadrature",
    "squarepoint": "squarepoint", "squarepoint capital": "squarepoint",
    "g research": "gresearch", "gresearch": "gresearch",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def company_key(name: str) -> str:
    n = re.sub(r"\s+", " ", norm(name))
    n = re.sub(r"\b(ltd|llp|llc|inc|plc|group|limited|co|corp|inc)\b", "", n).strip()
    return COMPANY_ALIASES.get(n, n.replace(" ", ""))


# Words that, tacked onto a company name, do not make it a different employer. NOTE the
# deliberate omission of 'securities': Citadel and Citadel Securities are separate firms with
# separate boards, and collapsing them would hide one behind the other.
GENERIC_SUFFIX = {"technologies", "technology", "holdings", "international", "partners",
                  "company", "corporation", "corp", "global", "worldwide"}


def same_company(a: str, b: str) -> bool:
    """'Palantir' from Trackr and 'Palantir Technologies' from a LinkedIn alert are one
    employer; without this they produce two cards for the same job."""
    if a == b:
        return True
    lo, hi = sorted((a, b), key=len)
    return bool(lo) and hi.startswith(lo) and hi[len(lo):] in GENERIC_SUFFIX


# Abbreviations one source uses and another spells out.
SYNONYMS = {
    "swe": "software engineer", "sde": "software engineer", "qt": "quant trader",
    "ml": "machine learning", "ds": "data science", "fde": "forward deployed engineer",
    "ib": "investment banking", "sre": "site reliability engineer",
}
# Light stemming so 'Quantitative Researcher' and 'Quant Research' land on the same tokens,
# and so British/American spellings ('modelling'/'modeling') do not split a match.
_STEM = {
    "researcher": "research", "engineering": "engineer", "engineers": "engineer",
    "developer": "develop", "development": "develop", "dev": "develop",
    "quantitative": "quant", "modelling": "model", "modeling": "model",
    "analytics": "analytic", "analyst": "analytic", "analysis": "analytic",
    "sciences": "science", "operations": "operation", "systems": "system",
    "traders": "trader", "trading": "trade", "technologies": "technology",
}


def role_tokens(title: str) -> set:
    words = norm(title).split()
    out = []
    for w in words:
        out.extend(SYNONYMS.get(w, w).split())
    return {_STEM.get(t, t) for t in out if t and t not in STOPWORDS}


def overlap(a: set, b: set) -> float:
    """Containment, not Jaccard: a board card's role text is often longer than the listing's
    (it carries the application framing), so symmetric similarity under-scores real matches."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# Tokens that split a role rather than describe it. Two postings that share 'software
# engineer' but differ on one of these are separate tracks with separate CVs, not one job.
DISCRIMINATORS = {
    "java", "python", "c", "cpp", "go", "golang", "rust", "scala", "kotlin", "javascript",
    "typescript", "ruby", "php", "swift", "matlab", "appsec", "infosec", "frontend",
    "backend", "fullstack", "mobile", "ios", "android", "embedded", "hardware",
}


def same_role(a: set, b: set) -> bool:
    """Do two token sets describe the same opportunity?

    Identical sets always match, which is what carries short titles like {technology}.
    Otherwise require BOTH decent containment AND at least two shared tokens: containment
    alone let 'Quant Research' match 'Machine Learning Research' on the single word
    'research', while the two-token floor still admits BlackRock's 'Technology - Data
    Analytics & Modelling' against the card that bundles it with Software Engineering.

    A discriminator on each side vetoes the match outright: Revolut's 'Software Engineer
    (Java)' and 'Software Engineer (Python)' share two tokens and 0.67 containment, but they
    are distinct applications with distinct CVs, and merging them loses one of them.
    """
    if a and a == b:
        return True
    if (a - b) & DISCRIMINATORS and (b - a) & DISCRIMINATORS:
        return False
    return overlap(a, b) >= 0.5 and len(a & b) >= 2


def find_cv(company: str, title: str, used: set) -> str | None:
    """Locate a built CV for this role. The skill names them '<prefix><Company> <Track>.pdf'.

    `used` holds CVs already attached to a board card, and they are excluded. That single rule
    is what stops a wrong-track CV being shipped: DRW has one CV, 'DRW Software Developer',
    already carried by the software card, so DRW's *quant trading* role finds no free CV and
    correctly stays in 'needs a JD' rather than going out with an engineering CV attached.
    """
    if not CV_DIR.is_dir():
        return None
    ck = company_key(company)
    if len(ck) < 4:
        return None
    cands = [p for p in CV_DIR.glob(f"{CV_PREFIX}*.pdf")
             if p.name not in used
             and company_key(p.stem[len(CV_PREFIX):]).startswith(ck[:6])]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0].name
    # several free CVs for one employer: pick the one whose track words best match the role
    want = role_tokens(title)
    best = max(cands, key=lambda p: overlap(role_tokens(p.stem[len(CV_PREFIX):]), want))
    return best.name


def slug(company: str, title: str) -> str:
    base = re.sub(r"-+", "-", f"{norm(company)}-{norm(title)}".replace(" ", "-")).strip("-")
    return base[:48].rstrip("-")


def load_board(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
    except Exception as e:
        print(f"sync_applyboard: cannot read {path}: {e}", file=sys.stderr)
        sys.exit(4)
    for bucket in ("open", "soon", "verify", "dead"):
        d.setdefault(bucket, [])
    return d


def board_entries(d: dict):
    """Every entry that represents an opportunity, across the buckets that can collide."""
    for bucket in ("open", "verify", "dead", "soon"):
        for e in d[bucket]:
            yield bucket, e


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--board", default=str(DEFAULT_BOARD))
    ap.add_argument("--threshold", type=int, default=75,
                    help="minimum fit to reach the board (default 75, matches prefs.md)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    board_path = Path(args.board)
    d = load_board(board_path)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT job_key,title,company,source,url,location,score,reason,status,category "
        "FROM jobs WHERE status NOT IN ('seeded','not-interested') AND score >= ? "
        "ORDER BY score DESC", (args.threshold,))]
    conn.close()

    known: dict[str, dict] = {}          # job_key -> entry
    for _, e in board_entries(d):
        e.setdefault("job_keys", [])
        for k in e["job_keys"]:
            known[k] = e

    added_open, added_verify, learned, promoted, queued = [], [], [], [], []
    # every CV already carried by a card, so no two roles can claim the same file
    used_cvs = {e["cv"] for _, e in board_entries(d) if e.get("cv")}

    def promote(entry, r) -> bool:
        """A card waiting on a job description has since had a CV built, so it is actionable
        now. Move it out of 'needs a JD from you' into 'Open now' rather than leaving a stale
        ask sitting there. The note is kept and appended to, never rewritten."""
        if entry not in d["verify"]:
            return False
        cv = find_cv(r["company"], r["title"], used_cvs)
        if not cv:
            return False
        used_cvs.add(cv)
        d["verify"].remove(entry)
        entry.pop("state", None)
        entry["cv"] = cv
        entry["fit"] = max(entry.get("fit") or 0, r["score"] or 0)
        entry["pick"] = entry["fit"] >= 84
        entry.setdefault("applied", False)
        entry.setdefault("deadline", None)
        entry.setdefault("deadline_label", "Rolling")
        if r["url"]:
            entry["url"] = r["url"]           # a real posting beats the old search-page link
        entry["note"] = (entry.get("note", "").rstrip() +
                         " CV has since been built from the fetched JD, so this is ready to send.").strip()
        d["open"].append(entry)
        promoted.append((r["company"], r["title"], entry["fit"], cv))
        return True

    for r in rows:
        key = r["job_key"]
        if key in known:
            promote(known[key], r)      # already represented, but may now be actionable
            continue

        ck = company_key(r["company"])
        want = role_tokens(r["title"])
        peers = [e for _, e in board_entries(d)
                 if same_company(company_key(e.get("company", "")), ck)]

        if not want:
            # The title is nothing but boilerplate ("Internship Programme 2027"), so it names
            # the umbrella programme rather than a function. If this employer is on the board
            # at all, the programme is already represented - BlackRock and Revolut are both
            # split into per-function cards, and adding an umbrella card would duplicate all
            # of them at once.
            match = peers[0] if peers else None
        else:
            scored = [(role_tokens(e.get("role", "")), e) for e in peers]
            hits = [(tok, e) for tok, e in scored if same_role(tok, want)]
            # prefer an exact token match over a merely-similar one
            hits.sort(key=lambda x: (x[0] != want, -overlap(x[0], want), -len(x[0] & want)))
            match = hits[0][1] if hits else None
        if match is not None:
            # Same opportunity under a different URL or source. Teach the board the alias so
            # the next run matches exactly, but do not mint a second card for it.
            match["job_keys"].append(key)
            known[key] = match
            learned.append((r["company"], r["title"], match.get("id") or match.get("role")))
            promote(match, r)
            continue

        cv = find_cv(r["company"], r["title"], used_cvs)
        entry = {
            "id": slug(r["company"], r["title"]),
            "company": r["company"],
            "role": r["title"],
            "location": r["location"] or "UK",
            "fit": r["score"],
            "deadline": None,
            "deadline_label": "Rolling",
            "url": r["url"] or "",
            "note": (r["reason"] or "").strip(),
            "job_keys": [key],
            "applied": False,
        }
        if not cv:
            # No CV means nothing is actionable yet, so this does NOT belong on a working list.
            # Auto-adding these is how the board reached 22 cards with 1 real to-do. It stays on
            # the Tracker, where the "-> apply board" button puts it across when Ramy wants it.
            queued.append((r["company"], r["title"], r["score"]))
            continue
        if cv:
            used_cvs.add(cv)
            entry["cv"] = cv
            entry["pick"] = r["score"] >= 84
            d["open"].append(entry)
            added_open.append((r["company"], r["title"], r["score"], cv))
        else:
            # No CV means the JD could not be read, so the blocker is a paste from Ramy. He
            # does that on the card itself now (Apply Board → paste the JD → jd_build.py),
            # so this state is a one-paste fix rather than a manual session.
            entry["state"] = "needs-jd"
            d["verify"].append(entry)
            added_verify.append((r["company"], r["title"], r["score"]))
        known[key] = entry

    changed = bool(added_open or added_verify or learned or promoted)
    if changed:
        d["updated"] = date.today().isoformat()

    if args.dry_run:
        print("DRY RUN, nothing written")
    elif changed:
        tmp = str(board_path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
        os.replace(tmp, board_path)      # atomic: a crash mid-write cannot corrupt the board

    print(f"apply board: +{len(added_open)} open, +{len(added_verify)} needs-jd, "
          f"{len(promoted)} promoted, {len(learned)} alias(es) learned, "
          f"{len(queued)} left on tracker, "
          f"{len(rows)} candidates at fit >= {args.threshold}")
    for co, t, s, cv in added_open:
        print(f"  + open   {s} {co} - {t}  [{cv}]")
    for co, t, s, cv in promoted:
        print(f"  ^ promoted {s} {co} - {t}  [{cv}]  (needs-jd -> open)")
    for co, t, s in added_verify:
        print(f"  + needs-jd {s} {co} - {t}")
    for co, t, eid in learned:
        print(f"  = alias  {co} - {t}  ->  {eid}")
    if queued:
        print(f"  {len(queued)} role(s) left on the Tracker (no CV, so nothing to action yet) - "
              f"use the '-> apply board' button there to bring one across:")
        for co, t, s_ in queued[:8]:
            print(f"      {s_} {co} - {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
