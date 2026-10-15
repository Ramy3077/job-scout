#!/usr/bin/env python3
"""Seed data for demo mode (JOBSCOUT_DEMO=1).

Everything here is invented: one fictional person and fifteen roles at fictional
companies, so a hosted preview can show The Scout populated without implying real
openings or real employers. Any resemblance to a real company is coincidence.

    demo.py --seed DIR        wipe and re-seed DIR with the demo data
    demo.py --prebuild        compile the sample CVs into the cache (Docker build step)

`seed` is what serve.py calls at boot and on a timer, so the demo resets itself.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import select_cv  # noqa: E402
import state  # noqa: E402

REPO = config.REPO
CACHE = Path(os.environ.get("JOBSCOUT_DEMO_CACHE") or REPO / ".demo-cache") / "cvs"

PROFILE_MD = """# Profile

> Demo profile. Alex Example is a made-up person.

## Contact
- **Name:** Alex Example
- **Email:** alex@example.com
- **Location:** London, UK
- **GitHub:** github.com/alex-example
- **LinkedIn:** linkedin.com/in/alex-example

## Summary statements
- Computer Science student building backend services and data pipelines, with a taste for reliable infrastructure.
- Computer Science student working across machine learning and data analysis, interested in turning messy data into decisions.
- Computer Science student who builds full-stack web products, from the database to the browser.

## Education
### Example University
**Degree:** BSc Computer Science, Sept 2024 - June 2028
- Relevant modules: Algorithms, Databases, Operating Systems, Machine Learning, Distributed Systems, Statistics

## Work experience
### Software Engineering Intern - Fictional Logistics Ltd - London
- **Dates:** June 2026 - Sept 2026
- Bullets:
  - Built a Python service that reconciled delivery events from three internal systems into one table
  - Wrote SQL and Airflow jobs that loaded daily shipment data into a PostgreSQL warehouse
  - Added unit and integration tests to a Flask API and set up its GitHub Actions pipeline
  - Containerised two internal tools with Docker so teammates could run them locally
  - Reviewed and merged pull requests from four teammates during a two-week release
  - Presented a demo of the reconciliation service to the operations team

### Student Teaching Assistant - Example University - London
- **Dates:** Oct 2025 - May 2026
- Bullets:
  - Ran weekly lab sessions for 25 first-year students on Python and data structures
  - Marked coursework and gave written feedback on algorithm design
  - Wrote worked solutions for the Databases problem sheets

## Projects
### Tidepool - `github.com/alex-example/tidepool`
- **One-liner:** A small ETL pipeline that pulls public tide data, cleans it and serves a dashboard
- **Tech:** Python, SQLite, Pandas, Docker
- **Dates:** 2026
- Bullets:
  - Wrote a scheduled Python pipeline that ingests, validates and deduplicates tide readings
  - Stored cleaned data in SQLite and exposed it through a small JSON API
  - Packaged the pipeline and dashboard with Docker Compose

### Paperclip Classifier - `github.com/alex-example/paperclip`
- **One-liner:** A machine learning model that sorts support emails into topics
- **Tech:** Python, scikit-learn, Jupyter
- **Dates:** 2025
- Bullets:
  - Trained a logistic regression and a random forest classifier on 4,000 labelled emails
  - Compared models with cross-validation and reported precision and recall per topic
  - Wrote a short evaluation notebook explaining where the model fails

### Pocket Budget - `github.com/alex-example/pocket-budget`
- **One-liner:** A full-stack budgeting web app with accounts and charts
- **Tech:** TypeScript, React, Node.js, PostgreSQL
- **Dates:** 2025
- Bullets:
  - Built a React front end and a Node.js REST API backed by PostgreSQL
  - Implemented sign-in with hashed passwords and session cookies
  - Deployed the app to a free hosting tier with automated deploys from GitHub

### Tinyshell - `github.com/alex-example/tinyshell`
- **One-liner:** A Unix-style shell written in C for an operating systems module
- **Tech:** C, Make
- **Dates:** 2025
- Bullets:
  - Implemented pipes, redirection and job control using fork, exec and signals
  - Wrote a test script that compares its output against bash on 40 commands

## Skills
- **Languages:** Python, SQL, TypeScript, C, JavaScript
- **Data and ML:** Pandas, scikit-learn, Airflow, PostgreSQL, SQLite
- **Web:** React, Node.js, Flask, REST APIs
- **Infrastructure:** Docker, GitHub Actions, Linux, Git
"""

PREFS_MD = """# Preferences

> Demo preferences for the made-up profile.

## What I am looking for
- Summer 2027 software engineering internships
- Graduate schemes starting 2028

## Locations
London, anywhere in the UK, fully remote, hybrid.

## Scoring guidance (0-100)
- 85-100 bullseye
- 75-84 strong
- 55-74 borderline
"""

# Sample job descriptions: every open role without a CV gets one, so "Paste sample JD" works on
# whichever fixture a visitor opens.
SAMPLE_JD = """Platform Engineering Intern, Summer 2027

Harrowgate Payments is a fictional company, so nothing here is a real opening.

About the role
You will join the platform team that keeps our payment services running. You will write
Python services, work with PostgreSQL and SQL, containerise applications with Docker and
help improve our CI pipelines in GitHub Actions. You will write automated tests, review
pull requests and take part in an on-call rotation as an observer.

What we are looking for
- Studying Computer Science or a related degree, graduating in 2028 or 2029
- Comfortable with Python and SQL, and happy working on Linux
- Interest in reliable infrastructure, testing and deployment pipelines
- Good communication: you will present your work to the wider engineering team
"""


_SAMPLE_SABLESTONE = """Security Engineering Intern, Summer 2027

Sablestone Security is a fictional company, so nothing here is a real opening.

About the role
You will join the application security team. You will write Python and C tooling that
checks our services for unsafe patterns, review pull requests for security problems,
and help harden Linux hosts and Docker images. You will write automated tests for the
checks you build and explain findings clearly to the engineers who own the code.

What we are looking for
- Studying Computer Science, graduating in 2028 or 2029
- Comfortable with Python or C, Linux and Git
- Curiosity about operating systems, processes and how software fails
"""

_SAMPLE_MARROWDALE = """DevOps Intern, Summer 2027

Marrowdale Cloud is a fictional company, so nothing here is a real opening.

About the role
You will work with the infrastructure team on the pipelines that ship our cloud
platform. You will containerise services with Docker, improve GitHub Actions workflows,
automate Linux deployments with scripts and keep our PostgreSQL backups healthy.
You will write tests for the tooling you change and review teammates' pull requests.

What we are looking for
- Studying Computer Science or a related degree
- Comfortable on Linux, with some Python and SQL
- Interest in CI pipelines, deployment and reliable infrastructure
"""

_SAMPLE_TALLOW = """Data Science Intern, Summer 2027

Tallow & Finch Software is a fictional company, so nothing here is a real opening.

About the role
You will join the analytics team to turn messy customer data into decisions. You will
clean and analyse data in Python with Pandas, train and evaluate machine learning models
with scikit-learn, compare them with cross-validation and report precision and recall
clearly. You will use SQL to pull data from PostgreSQL and present findings to the team.

What we are looking for
- Studying Computer Science, Maths or Statistics, graduating in 2028 or 2029
- Comfortable with Python, SQL and statistics
- Interest in machine learning and data analysis
"""

_SAMPLE_KESTREL_CPP = """Software Engineer Intern (C++), Summer 2027

Kestrel Ridge Robotics is a fictional company, so nothing here is a real opening.

About the role
You will work on the software that drives our robot test rigs. Most of it is C++ on Linux,
with Python tooling around it for test automation. You will write tests, debug processes and
signals on embedded Linux boxes, and review pull requests with the controls team.

What we are looking for
- Studying Computer Science or Engineering, graduating in 2028 or 2029
- Some C or C++, plus Python, Linux and Git
- Interest in operating systems and how software talks to hardware
"""

_SAMPLE_NIMBUS = """Site Reliability Intern, Summer 2027

Nimbus Quarry is a fictional company, so nothing here is a real opening.

About the role
You will join the reliability team that keeps our hosted services up. You will write Python
scripts to automate Linux operations, improve monitoring, work with Docker and help run our
GitHub Actions deployment pipelines. You will shadow the on-call rotation.

What we are looking for
- Studying Computer Science or a related degree
- Comfortable on Linux, with some Python and Git
- Interest in reliable infrastructure and incident response
"""

# (company, title) -> the sample JD that role's "Paste sample JD" button fills in. Each one names
# its own company and title, so the mismatch warning stays quiet for the intended pairing.
SAMPLE_JDS = {
    ("Harrowgate Payments", "Platform Engineering Intern"): SAMPLE_JD,
    ("Sablestone Security", "Security Engineering Intern"): _SAMPLE_SABLESTONE,
    ("Marrowdale Cloud", "DevOps Intern"): _SAMPLE_MARROWDALE,
    ("Tallow & Finch Software", "Data Science Intern"): _SAMPLE_TALLOW,
    ("Kestrel Ridge Robotics", "Software Engineer Intern (C++)"): _SAMPLE_KESTREL_CPP,
    ("Nimbus Quarry", "Site Reliability Intern"): _SAMPLE_NIMBUS,
}


def sample_for(company: str, title: str) -> str | None:
    return SAMPLE_JDS.get((company, title))

# `jd` is set only where a CV already exists, built at seed time. `deadline` is days from today.
_JD_PYTHON = ("Backend and data services in Python with SQL and PostgreSQL. You will build "
              "pipelines, write tests, use Docker and GitHub Actions, and review pull requests. "
              "Linux and a love of reliable systems help.")
_JD_ML = ("Machine learning intern: train and evaluate models in Python with scikit-learn and "
          "Pandas, compare approaches with cross-validation and report where the models fail. "
          "Statistics and machine learning coursework preferred.")
_JD_FULLSTACK = ("Full-stack engineering: build React and TypeScript front ends with Node.js REST "
                 "APIs and PostgreSQL. Deploy with GitHub Actions and write tests for your code.")
_JD_DATA = ("Data engineering placement: build ETL pipelines in Python and SQL, orchestrate with "
            "Airflow, load a PostgreSQL warehouse and validate data quality. Docker a plus.")
_JD_SYSTEMS = ("Software engineering in C and Python on Linux: processes, signals and operating "
               "systems fundamentals, with tests and clear communication.")

ROLES = [
    dict(co="Brightwater Systems", title="Software Engineer Intern", loc="London", stage="internship",
         cat="Software Engineering", score=92, status="surfaced", seen=1,
         why="Python and SQL backend work, London, summer 2027 intake. Closest fit on the list.",
         card="open", jd=_JD_PYTHON, deadline=21),
    dict(co="Northgate Labs", title="Backend Engineering Intern", loc="London", stage="internship",
         cat="Software Engineering", score=88, status="applied", seen=9,
         why="Backend services, Docker and testing. Applied after tailoring to the posting.",
         card="open", jd=_JD_PYTHON, deadline=14, applied=6),
    dict(co="Quillmark Data", title="Data Engineering Placement", loc="Manchester", stage="placement",
         cat="Data", score=85, status="interviewing", seen=18,
         why="Airflow and PostgreSQL warehouse work. First-round call booked.",
         card="open", jd=_JD_DATA, deadline=None, applied=12),
    dict(co="Fernhill Analytics", title="Machine Learning Intern", loc="Remote (UK)", stage="internship",
         cat="Machine Learning", score=81, status="surfaced", seen=3,
         why="Classification and evaluation work that matches the Paperclip project.",
         card="open", jd=_JD_ML, deadline=30),
    dict(co="Kestrel Ridge Robotics", title="Software Engineer Intern (Python)", loc="Bristol",
         stage="internship", cat="Software Engineering", score=90, status="applied", seen=11,
         why="Python tooling for a robotics test rig.",
         card="open", jd=_JD_SYSTEMS, deadline=None, applied=8),
    dict(co="Wrenfield Health", title="Graduate Software Engineer", loc="London", stage="graduate",
         cat="Software Engineering", score=83, status="offer", seen=40,
         why="Full-stack role on a patient-scheduling product. Offer received, decision due.",
         card="open", jd=_JD_FULLSTACK, deadline=None, applied=35),
    dict(co="Kestrel Ridge Robotics", title="Software Engineer Intern (C++)", loc="Bristol",
         stage="internship", cat="Software Engineering", score=84, status="surfaced", seen=11,
         why="Same employer as the Python role. The application form covers several postings."),
    dict(co="Harrowgate Payments", title="Platform Engineering Intern", loc="London", stage="internship",
         cat="Cloud and DevOps", score=79, status="surfaced", seen=2,
         why="Platform and CI work. Posting text is behind a script-rendered page, so paste it in.",
         deadline=9),
    dict(co="Sablestone Security", title="Security Engineering Intern", loc="Edinburgh", stage="internship",
         cat="Security", score=76, status="surfaced", seen=4,
         why="Secure-coding focus. Needs the job description pasted in."),
    dict(co="Marrowdale Cloud", title="DevOps Intern", loc="London", stage="internship",
         cat="Cloud and DevOps", score=77, status="surfaced", seen=5,
         why="Docker, Linux and pipelines. Needs the job description pasted in.", deadline=40),
    dict(co="Tallow & Finch Software", title="Data Science Intern", loc="Leeds", stage="internship",
         cat="Data", score=80, status="surfaced", seen=6,
         why="Statistics and Python analysis. Needs the job description pasted in.", deadline=5),
    dict(co="Lumenfold", title="Full-Stack Engineer Intern", loc="London", stage="internship",
         cat="Software Engineering", score=86, status="parked", seen=14,
         why="Parked while the Wrenfield offer is open."),
    dict(co="Copperline Logistics", title="Cloud Infrastructure Intern", loc="Birmingham",
         stage="internship", cat="Cloud and DevOps", score=74, status="rejected", seen=30,
         why="Rejected after the online assessment.", applied=26),
    dict(co="Pebblecraft Games", title="Gameplay Engineering Intern", loc="Brighton", stage="internship",
         cat="Software Engineering", score=58, status="not-interested", seen=20,
         why="Games engine work in C++. Not the direction."),
    dict(co="Nimbus Quarry", title="Site Reliability Intern", loc="Remote (UK)", stage="internship",
         cat="Cloud and DevOps", score=72, status="surfaced", seen=1,
         why="Borderline: on-call heavy and light on software."),
]

def _slug(s: str) -> str:
    import re
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")[:48]


def _url(r: dict) -> str:
    return f"https://example.com/jobs/{_slug(r['co'])}/{_slug(r['title'])}"


def _cv_name(r: dict) -> str:
    import jd_build
    company = jd_build.short_company(r["co"]).translate(jd_build._UNSAFE).strip()
    return f"{config.CV_PREFIX}{company} {jd_build.track_of(r['title'])}.pdf"


def _ensure_cv(r: dict, profile_text: str, dest: Path) -> bool:
    """Put this role's CV in `dest`: copy from the cache, else build it (and cache it)."""
    name = _cv_name(r)
    cached = CACHE / name
    if not cached.is_file():
        import build_cv
        content = select_cv.build_content(profile_text, r["jd"], name[:-4])
        try:
            build_cv.build_pdf(content, CACHE)
        except RuntimeError as e:
            print(f"demo: could not build {name}: {e}", file=sys.stderr)
            return False
    shutil.copy2(cached, dest / name)
    return True


def seed(data_dir: str | Path) -> dict:
    """Wipe and re-seed `data_dir` with the demo data. Safe to call while serving: the new
    data is staged beside the old and swapped in file by file."""
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    stage = data / ".stage"
    shutil.rmtree(stage, ignore_errors=True)
    for sub in ("cvs", "jds", "jdbuilds", "logs"):
        (stage / sub).mkdir(parents=True)
    (stage / "profile.md").write_text(PROFILE_MD)
    (stage / "prefs.md").write_text(PREFS_MD)

    today = date.today()
    now = datetime.now()

    # tracker database
    db = stage / "state.sqlite"
    conn = state.connect(db)
    state.init_db(conn)
    sources = ["Alert email", "Career page", "Alert email", "Manual", "Career page"]
    for i, r in enumerate(ROLES):
        r["src"] = sources[i % len(sources)]
        rec = {"title": r["title"], "company": r["co"], "source": r["src"], "url": _url(r)}
        key = state.job_key(rec)
        r["_key"] = key
        seen = (now - timedelta(days=r["seen"])).isoformat(timespec="seconds")
        applied = (now - timedelta(days=r["applied"])).isoformat(timespec="seconds") \
            if r.get("applied") else None
        status_at = applied or seen
        deadline = (today + timedelta(days=r["deadline"])).isoformat() \
            if r.get("deadline") else None
        conn.execute(
            "INSERT INTO jobs(job_key,title,company,source,url,location,stage,score,reason,"
            "status,first_seen,applied_at,category,status_at,tailoring,deadline) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, r["title"], r["co"], r["src"], rec["url"], r["loc"], r["stage"], r["score"],
             r["why"], r["status"], seen, applied, r["cat"], status_at,
             "jd" if r.get("jd") else None, deadline))
    conn.commit()
    conn.close()

    # CV store: one card per role that already has a CV. serve.py adds a card for any other
    # role the first time someone pastes a job description into it.
    board = {"updated": today.isoformat(), "demo": True, "open": [], "soon": [], "verify": [],
             "dead": [], "rules": ""}
    for r in ROLES:
        if not (r.get("card") == "open" and r.get("jd")):
            continue
        if not _ensure_cv(r, PROFILE_MD, stage / "cvs"):
            continue                  # no tectonic: the role simply has no CV yet
        board["open"].append({
            "id": _slug(f"{r['co']} {r['title']}"), "company": r["co"], "role": r["title"],
            "location": r["loc"], "fit": r["score"], "url": _url(r), "note": r["why"],
            "job_keys": [r["_key"]], "cv": _cv_name(r),
            "tailoring_report": select_cv.explain(select_cv.parse_profile(PROFILE_MD), r["jd"]),
            "jd_pasted": (today - timedelta(days=r["seen"])).isoformat()})
    (stage / "roles.json").write_text(json.dumps(board, indent=1, ensure_ascii=False))

    # swap in
    for sub in ("cvs", "jds", "jdbuilds", "logs"):
        shutil.rmtree(data / sub, ignore_errors=True)
        os.replace(stage / sub, data / sub)
    for name in ("profile.md", "prefs.md", "roles.json", "state.sqlite"):
        for junk in (data / f"{name}-journal", data / f"{name}-wal"):
            junk.unlink(missing_ok=True)
        os.replace(stage / name, data / name)
    shutil.rmtree(stage, ignore_errors=True)
    return {"roles": len(ROLES), "with_cv": len(board["open"])}


def prebuild() -> int:
    ok = 0
    for r in ROLES:
        if r.get("jd") and r.get("card") == "open":
            tmp = CACHE.parent / "tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            ok += _ensure_cv(r, PROFILE_MD, tmp)
    shutil.rmtree(CACHE.parent / "tmp", ignore_errors=True)
    print(f"demo: {ok} sample CVs cached in {CACHE}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", metavar="DIR")
    ap.add_argument("--prebuild", action="store_true")
    a = ap.parse_args()
    if a.prebuild:
        sys.exit(prebuild())
    if a.seed:
        print(seed(a.seed))
    else:
        ap.print_help()
