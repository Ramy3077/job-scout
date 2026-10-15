#!/usr/bin/env python3
"""job-scout server: The Scout, on one local port.

    python3 serve.py            # then open http://localhost:7777

Binds to 127.0.0.1:7777 by default. JOBSCOUT_HOST / JOBSCOUT_PORT change that (a container
sets JOBSCOUT_HOST=0.0.0.0). JOBSCOUT_DEMO=1 runs the hosted-demo sandbox: invented data in a
temp directory, re-seeded on a timer, and no mail or external sources are ever touched.

The Scout reads everything from state.sqlite (roles, statuses, deadlines, picks). roles.json is
only the CV store: which PDF belongs to which role, and what the tailoring kept and dropped.

Stdlib only. Every path is resolved relative to this file, or overridden with environment
variables, so the repo runs from wherever you cloned it.
"""
from __future__ import annotations

import glob
import json
import mimetypes
import os
import re
import socketserver
import sqlite3
import subprocess
import sys
import http.server
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "jobscout"))
import config  # noqa: E402
import state as _state  # noqa: E402

HOST = os.environ.get("JOBSCOUT_HOST") or "127.0.0.1"
PORT = int(os.environ.get("JOBSCOUT_PORT", "7777"))
DEMO = config.DEMO
DATA = str(config.DATA)
LOGDIR = str(config.LOG_DIR)
CV_DIR = str(config.CV_DIR)
DB = str(config.DB)
BOARD = str(config.BOARD)
JDS = str(config.JD_DIR)
BUILDS = str(config.BUILD_DIR)
WORKER = os.path.join(HERE, "jobscout", "jd_build.py")
PYTHON = os.path.expanduser(os.environ.get("JOBSCOUT_PYTHON") or sys.executable)
STATIC = os.path.join(HERE, "app")

# How many applications make a good week, and the fit score a role needs before it is put in
# the week's fixtures automatically. Both are yours to change.
WEEKLY_TARGET = int(os.environ.get("JOBSCOUT_WEEKLY_TARGET", "5"))
MIN_FIT = int(os.environ.get("JOBSCOUT_MIN_FIT", "75"))
SEASON_WEEKS = 16          # the form guide never reaches further back than this

STATUSES = {"surfaced", "applied", "interviewing", "offer", "rejected", "parked",
            "not-interested"}
LIVE = ("applied", "interviewing")
SENT = ("applied", "interviewing", "offer", "rejected")

for d in (DATA, LOGDIR, CV_DIR, JDS, BUILDS):
    os.makedirs(d, exist_ok=True)


def bootstrap():
    """A fresh clone has no database and no CV store. Create both so the first
    `python3 serve.py` opens a working, empty app rather than a stack trace."""
    if DEMO:
        import demo
        demo.seed(DATA)
        return
    conn = _state.connect(Path(DB))
    try:
        _state.init_db(conn)
        conn.commit()
    finally:
        conn.close()
    if not os.path.isfile(BOARD):
        save_board({"updated": date.today().isoformat(), "open": [], "soon": [],
                    "verify": [], "dead": [], "rules": ""})


# ── storage ─────────────────────────────────────────────────────────────────
def db():
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    _state.init_db(c)          # idempotent: guarantees the deadline and pick columns
    return c


def load_board() -> dict:
    try:
        with open(BOARD) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    for b in ("open", "soon", "verify", "dead"):
        d.setdefault(b, [])
    return d


def save_board(d: dict) -> None:
    tmp = BOARD + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    os.replace(tmp, BOARD)          # atomic: a crash mid-write cannot corrupt it


def cards_by_key(board: dict) -> dict:
    """job_key -> the card holding that role's CV. A card can cover several listings."""
    out = {}
    for b in ("open", "verify"):
        for e in board.get(b, []):
            for k in e.get("job_keys") or []:
                out.setdefault(k, e)
    return out


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")[:48]


def ensure_card(key: str) -> dict | None:
    """The card a build for this role writes to, created on first use."""
    with db() as c:
        r = c.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
    if not r:
        return None
    board = load_board()
    hit = cards_by_key(board).get(key)
    if hit:
        return hit
    taken = {e.get("id") for b in ("open", "verify", "soon", "dead") for e in board[b]}
    base = slug(f"{r['company']} {r['title']}") or "role"
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}-{n}", n + 1
    card = {"id": cid, "company": r["company"], "role": r["title"],
            "location": r["location"] or "", "fit": r["score"], "url": r["url"] or "",
            "note": (r["reason"] or "").strip(), "job_keys": [key], "state": "needs-jd",
            "deadline": r["deadline"], "deadline_label": "Rolling"}
    board["verify"].append(card)
    save_board(board)
    return card


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def sample_jd(company, title, card=None):
    if card and card.get("sample_jd"):
        return card["sample_jd"]
    if DEMO:
        import demo
        return demo.sample_for(company, title)
    return None


# ── what the app shows ──────────────────────────────────────────────────────
def form_guide(rows, today):
    """One entry per week since the first application: sent count and W/D/L against the
    weekly target. W is the target met, D is at least 60% of it."""
    sent = {}
    for r in rows:
        if r["applied_at"]:
            wk = week_start(datetime.fromisoformat(r["applied_at"][:19]).date())
            sent[wk] = sent.get(wk, 0) + 1
    this_week = week_start(today)
    start = min(sent) if sent else this_week
    start = max(start, this_week - timedelta(weeks=SEASON_WEEKS - 1))
    draw = max(1, -(-WEEKLY_TARGET * 3 // 5))
    weeks, wk = [], start
    while wk <= this_week:
        n = sent.get(wk, 0)
        res = "W" if n >= WEEKLY_TARGET else "D" if n >= draw else "L"
        weeks.append({"week": wk.isoformat(), "sent": n,
                      "result": "live" if wk == this_week and n < WEEKLY_TARGET else res})
        wk += timedelta(days=7)
    streak = 0
    for w in reversed(weeks):
        if w["result"] == "live":
            continue
        if w["result"] != "W":
            break
        streak += 1
    return weeks, streak


def app_state() -> dict:
    today = date.today()
    monday = week_start(today).isoformat()
    board = load_board()
    cards = cards_by_key(board)
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM jobs WHERE status!='seeded'")]
        meta = dict(c.execute("SELECT key,value FROM meta").fetchall())

    sent_week = sum(1 for r in rows if (r["applied_at"] or "") >= monday)
    open_ = [r for r in rows if r["status"] == "surfaced"
             and not (r["deadline"] and r["deadline"] < today.isoformat())]
    pinned = [r for r in open_ if r["pick"] == "pinned"]
    pool = [r for r in open_ if not r["pick"] and (r["score"] or 0) >= MIN_FIT]

    def rank(r):
        # best fit first, nudged by a close deadline: a week away is worth ten points
        bonus = 0
        if r["deadline"]:
            left = (date.fromisoformat(r["deadline"]) - today).days
            bonus = 10 if left <= 7 else 5 if left <= 21 else 0
        return -((r["score"] or 0) + bonus)
    pool.sort(key=rank)
    auto, seen = [], {_state.company_ident(r["company"]) for r in pinned}
    slots = max(0, WEEKLY_TARGET - sent_week - len(pinned))
    for r in pool:
        if len(auto) >= slots:
            break
        ident = _state.company_ident(r["company"])
        if ident in seen:            # one per employer, so five tracks at one firm
            continue                 # cannot crowd everything else out
        seen.add(ident)
        auto.append(r)

    def card(r, why):
        e = cards.get(r["job_key"]) or {}
        return {"job_key": r["job_key"], "company": r["company"], "title": r["title"],
                "fit": r["score"], "deadline": r["deadline"], "url": r["url"],
                "location": r["location"], "note": (r["reason"] or "")[:240],
                "cv": e.get("cv"), "tailoring": "jd" if e.get("jd_pasted") else r["tailoring"],
                "report": e.get("tailoring_report"),
                "sample_jd": sample_jd(r["company"], r["title"], e), "why": why}

    waiting = sorted(
        ({"job_key": r["job_key"], "company": r["company"], "title": r["title"],
          "status": r["status"], "applied_at": (r["applied_at"] or "")[:10],
          "days": (today - datetime.fromisoformat(r["applied_at"][:19]).date()).days
          if r["applied_at"] else None}
         for r in rows if r["status"] in LIVE),
        key=lambda w: -(w["days"] or 0))

    # ticker: the latest things that happened, newest first
    feed = []
    for r in rows:
        if r["applied_at"]:
            feed.append({"at": r["applied_at"], "kind": "sent", "company": r["company"],
                         "title": r["title"]})
        if r["status"] in ("interviewing", "offer", "rejected") and r["status_at"]:
            feed.append({"at": r["status_at"], "kind": r["status"], "company": r["company"],
                         "title": r["title"]})
    feed.sort(key=lambda f: f["at"], reverse=True)
    ticker, keys = [], set()
    for f in feed:
        k = (f["kind"], f["company"], f["at"][:10])
        if k not in keys:
            keys.add(k)
            ticker.append(f)
        if len(ticker) >= 14:
            break

    weeks, streak = form_guide(rows, today)
    count = lambda s: sum(1 for r in rows if r["status"] == s)
    return {
        "demo": DEMO,
        "today": today.isoformat(),
        "matchweek": len(weeks),
        "target": WEEKLY_TARGET, "sent_week": sent_week,
        "five": [card(r, "pinned") for r in pinned] + [card(r, "auto") for r in auto],
        "reserve": len(pool) - len(auto),
        "waiting": waiting,
        "ticker": ticker,
        "form": weeks[-8:], "weeks": weeks, "streak": streak,
        "season": {"sent": sum(1 for r in rows if r["applied_at"]),
                   "interviews": count("interviewing") + count("offer"),
                   "offers": count("offer"), "rejected": count("rejected"),
                   "live": count("applied") + count("interviewing"),
                   "tracked": len(rows)},
        "last_scan": meta.get("last_run"),
    }


def roles(view: str = "open", q: str = "") -> list:
    """The whole market, for the Scouting tab."""
    today = date.today().isoformat()
    cards = cards_by_key(load_board())
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT job_key,company,title,score,deadline,status,pick,url,category,location,"
            "reason,applied_at,status_at,first_seen FROM jobs WHERE status!='seeded'")]
    q = q.strip().lower()
    out = []
    for r in rows:
        closed = bool(r["deadline"] and r["deadline"] < today)
        r["closed"] = closed
        if view == "open" and not (r["status"] == "surfaced" and not closed):
            continue
        if view == "sent" and r["status"] not in ("applied", "interviewing", "offer"):
            continue
        if view == "out" and r["status"] != "rejected":
            continue
        if view == "shelved" and r["status"] not in ("parked", "not-interested") and not (
                r["status"] == "surfaced" and closed):
            continue
        if q and q not in (r["company"] or "").lower() and q not in (r["title"] or "").lower():
            continue
        r["reason"] = (r["reason"] or "")[:200]
        r["cv"] = (cards.get(r["job_key"]) or {}).get("cv")
        out.append(r)
    key = {"open": lambda r: -(r["score"] or 0),
           "sent": lambda r: r["applied_at"] or "",
           "out": lambda r: r["status_at"] or ""}.get(view, lambda r: -(r["score"] or 0))
    out.sort(key=key, reverse=view in ("sent", "out"))
    return out


def set_status(key, status):
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}")
    now = datetime.now().isoformat(timespec="seconds")
    applied_at = now if status == "applied" else None
    with db() as c:
        return c.execute(
            "UPDATE jobs SET status=?, status_at=?, applied_at=COALESCE(applied_at,?) "
            "WHERE job_key=?", (status, now, applied_at, key)).rowcount


def set_pick(key, pick):
    if pick not in (None, "pinned", "skipped"):
        raise ValueError("pick must be pinned, skipped or empty")
    with db() as c:
        n = c.execute("UPDATE jobs SET pick=? WHERE job_key=?", (pick, key)).rowcount
        if pick == "pinned":      # picking something shelved brings it back
            c.execute("UPDATE jobs SET status='surfaced' WHERE job_key=? "
                      "AND status IN ('parked','not-interested')", (key,))
    return n


def add_role(company, title, url="", deadline=""):
    company, title, url = (company or "").strip(), (title or "").strip(), (url or "").strip()
    if not company or not title:
        raise ValueError("company and title are required")
    if url and not re.match(r"https?://", url):
        raise ValueError("the link must start with http")
    k = _state.job_key({"source": "Manual", "company": company, "title": title, "url": url})
    deadline = deadline if re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline or "") else None
    now = datetime.now().isoformat(timespec="seconds")
    with db() as c:
        if c.execute("SELECT 1 FROM jobs WHERE job_key=?", (k,)).fetchone():
            c.execute("UPDATE jobs SET pick='pinned' WHERE job_key=?", (k,))
            return k, "already tracked, now in this week's fixtures"
        c.execute(
            "INSERT INTO jobs(job_key,title,company,source,url,location,stage,score,reason,"
            "status,first_seen,deadline,pick) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (k, title[:200], company[:120], "Manual", url[:500], "", None, None,
             "Added by hand.", "surfaced", now, deadline, "pinned"))
    return k, "added to this week's fixtures"


# ── paste a JD, get a CV ────────────────────────────────────────────────────
# jd_build.py owns the build so it stays runnable from a terminal and a long build cannot
# block the server. The server only queues it and reports on it.
JD_MIN = 120          # shorter than this is a mis-paste, not a job description
JD_MAX_PENDING = 4    # builds are serialised, so a deep queue just means a long wait


def jd_status() -> dict:
    """Every build we know about, keyed by job_key, for the app to poll."""
    out = {}
    for fp in glob.glob(os.path.join(BUILDS, "*.json")):
        try:
            with open(fp) as f:
                s = json.load(f)
        except Exception:  # noqa: BLE001
            continue       # a status file caught mid-write: it will be whole next poll
        if s.get("job_key"):
            out[s["job_key"]] = s
    return out


def jd_start(key: str, body: bytes) -> dict:
    text = body.decode("utf-8", errors="replace").strip()
    if len(text) < JD_MIN:
        return {"ok": False, "error": "that is only %d characters. Paste the whole job "
                                      "description." % len(text)}
    card = ensure_card(key)
    if not card:
        return {"ok": False, "error": "unknown role"}
    rid = card["id"]
    live = [s for s in jd_status().values() if s.get("state") in ("queued", "running")]
    if any(s.get("id") == rid for s in live):
        return {"ok": False, "error": "a build for this role is already running"}
    if len(live) >= JD_MAX_PENDING:
        return {"ok": False,
                "error": "%d builds already queued. Let those finish first." % len(live)}

    jd_file = os.path.join(JDS, rid + ".txt")
    with open(jd_file + ".tmp", "w") as f:
        f.write(text)
    os.replace(jd_file + ".tmp", jd_file)

    # Seed the status here so the role flips to 'queued' on this response rather than on
    # whichever poll first catches the worker starting up.
    st = {"id": rid, "job_key": key, "state": "queued", "company": card.get("company"),
          "role": card.get("role"), "jd_chars": len(text), "at": config.now_iso()}
    stmp = os.path.join(BUILDS, rid + ".json.tmp")
    with open(stmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(stmp, os.path.join(BUILDS, rid + ".json"))

    logf = open(os.path.join(LOGDIR, "jd-build.log"), "a")
    subprocess.Popen([PYTHON, WORKER, "--id", rid, "--jd", jd_file],
                     cwd=os.path.dirname(WORKER), stdout=logf, stderr=logf,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "state": "queued", "chars": len(text)}


def jd_dismiss(key: str) -> dict:
    """Clear a finished or failed build so the role goes back to a clean slate."""
    st = jd_status().get(key)
    if not st:
        return {"ok": False, "error": "nothing to clear"}
    if st.get("state") in ("queued", "running"):
        return {"ok": False, "error": "that build is still running"}
    rid = re.sub(r"[^A-Za-z0-9._-]+", "-", st.get("id") or "").strip("-.")
    fp = os.path.join(BUILDS, rid + ".json")
    if rid and os.path.isfile(fp):
        os.remove(fp)
    return {"ok": True}


def cv_path(name: str):
    """Resolve a CV filename to a real path inside the CVs dir, or None."""
    base = os.path.realpath(CV_DIR)
    fp = os.path.realpath(os.path.join(base, os.path.basename(name)))
    ok = fp.startswith(base + os.sep) and os.path.isfile(fp) and fp.lower().endswith(".pdf")
    return fp if ok else None


# One line, everywhere. Every rule is scoped to #demo-banner and marked !important so the
# page's own styles cannot turn it back into a block.
DEMO_BANNER = """<style>
html{padding-top:34px!important}
#demo-banner{position:fixed!important;top:0;left:0;right:0;z-index:99999;height:34px;
display:flex!important;align-items:center;justify-content:center;gap:10px;white-space:nowrap;
overflow:hidden;background:#1d1a16!important;color:#f3ebdc!important;
font:600 12.5px/1 ui-sans-serif,system-ui,sans-serif!important;padding:0 14px!important;margin:0!important}
#demo-banner span,#demo-banner a{display:inline!important;padding:0!important;margin:0!important;
border:0!important;color:#f3ebdc!important;font:inherit!important}
#demo-banner a{text-decoration:underline;color:#efb32d!important}
@media(max-width:560px){#demo-banner .opt{display:none!important}}</style>
<div id="demo-banner"><span>Demo data, resets periodically.</span>
<a href="https://github.com/Ramy3077/job-scout">Source on GitHub</a>
<span class="opt">Open a fixture and press Paste sample JD.</span></div>"""


class Handler(http.server.BaseHTTPRequestHandler):
    # A pasted job description is far too big for a query string, so it arrives as the
    # request body. Capped because nothing legitimate here is a megabyte of text.
    MAX_BODY = 400_000
    NOT_SERVABLE = (".sqlite", ".sqlite3", ".db", ".db-journal", ".py", ".sql",
                    ".env", ".json.tmp")

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _redirect(self, where, code=302):
        self.send_response(code)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        return self.rfile.read(min(n, self.MAX_BODY)) if n > 0 else b""

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        one = lambda k, d="": (qs.get(k) or [d])[0]
        path = u.path
        try:
            if path == "/healthz":
                return self._json({"ok": True, "demo": DEMO})
            if path == "/demo/sample-jd.txt" and DEMO:
                import demo
                return self._send(200, demo.SAMPLE_JD.encode(), "text/plain; charset=utf-8")
            if path == "/api/state":
                return self._json(app_state())
            if path == "/api/roles":
                return self._json({"rows": roles(one("view", "open"), one("q"))})
            if path == "/api/jdstatus":
                return self._json(jd_status())
            if path.startswith("/cv/"):
                fp = cv_path(urllib.parse.unquote(path[4:]))
                if not fp:
                    return self._json({"error": "no such CV"}, 404)
                how = "attachment" if one("dl") == "1" else "inline"
                with open(fp, "rb") as f:
                    data = f.read()
                return self._send(200, data, "application/pdf",
                                  [("Content-Disposition",
                                    '%s; filename="%s"' % (how, os.path.basename(fp)))])
            # The two old boards lived here. Keep their links working.
            if path.rstrip("/") in ("/tracker", "/apply"):
                return self._redirect("/", 301)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)
        self.serve_static(path.lstrip("/") or "index.html")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        one = lambda k, d="": (qs.get(k) or [d])[0]
        action = u.path[len("/api/"):] if u.path.startswith("/api/") else ""
        try:
            if action == "mark":
                n = set_status(one("job_key"), one("status", "applied"))
                return self._json({"ok": bool(n), **({} if n else {"error": "unknown role"})})
            if action == "pick":
                n = set_pick(one("job_key"), one("pick") or None)
                return self._json({"ok": bool(n), **({} if n else {"error": "unknown role"})})
            if action == "add":
                key, note = add_role(one("company"), one("title"), one("url"), one("deadline"))
                return self._json({"ok": True, "job_key": key, "note": note})
            if action == "jd":
                return self._json(jd_start(one("job_key"), self._body()))
            if action == "jddismiss":
                return self._json(jd_dismiss(one("job_key")))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": str(e)}, 500)
        self._json({"ok": False, "error": "unknown action"}, 404)

    def serve_static(self, rel):
        base = os.path.realpath(STATIC)
        fp = os.path.realpath(os.path.join(base, rel))
        if fp.lower().endswith(self.NOT_SERVABLE) or "/__pycache__/" in fp \
                or not fp.startswith(base + os.sep) or not os.path.isfile(fp):
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        with open(fp, "rb") as f:
            data = f.read()
        if ctype == "text/html":
            ctype += "; charset=utf-8"
            if DEMO:
                data = data.replace(b"<body>", b"<body>" + DEMO_BANNER.encode(), 1)
        self._send(200, data, ctype)


def _demo_reset_loop(hours):
    """Re-seed the demo on a timer so whatever visitors did to it does not pile up."""
    import time
    import demo
    while True:
        time.sleep(hours * 3600)
        try:
            demo.seed(DATA)
        except Exception as e:  # noqa: BLE001
            print("demo reseed failed:", e, file=sys.stderr)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    bootstrap()
    shown = "localhost" if HOST in ("127.0.0.1", "localhost") else HOST
    print(f"The Scout on http://{shown}:{PORT}" + ("  [DEMO MODE]" if DEMO else ""))
    print(f"  data   {DATA}")
    print(f"  CVs    {CV_DIR}")
    if DEMO:
        import threading
        threading.Thread(target=_demo_reset_loop, daemon=True,
                         args=(float(os.environ.get("JOBSCOUT_DEMO_RESET_HOURS", "6")),)).start()
    ThreadingServer((HOST, PORT), Handler).serve_forever()
