#!/usr/bin/env python3
"""job-scout server: the tracker and the apply board on one local port.

    python3 serve.py            # then open http://localhost:7777

Stdlib only. Every path is resolved relative to this file, or overridden with
environment variables, so the repo runs from wherever you cloned it.
"""
import glob
import json
import mimetypes
import os
import re
import sqlite3
import socketserver
import subprocess
import sys
import http.server
import urllib.parse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "jobscout"))
import config  # noqa: E402


def _env(name, default):
    return os.path.expanduser(os.environ.get(name) or default)


PORT = int(os.environ.get("JOBSCOUT_PORT", "7777"))
DATA = _env("JOBSCOUT_DATA", os.path.join(HERE, "data"))
LOGDIR = os.path.join(DATA, "logs")
# Where finished CVs land. Point this at a notes vault if you keep one.
CV_DIR = _env("JOBSCOUT_CVS", os.path.join(DATA, "cvs"))
PYTHON = _env("JOBSCOUT_PYTHON", sys.executable)

for d in (DATA, LOGDIR, CV_DIR, os.path.join(DATA, "jds"),
          os.path.join(DATA, "jdbuilds")):
    os.makedirs(d, exist_ok=True)

# Keyed the way the handlers below expect. Add a board by adding an entry.
BY_ID = {
    "tracker": {
        "root": os.path.join(HERE, "boards", "trackerboard"),
        "index": "index.html",
        "db": os.path.join(DATA, "state.sqlite"),
    },
    "apply": {
        "root": os.path.join(HERE, "boards", "applyboard"),
        "index": "index.html",
        "data": os.path.join(DATA, "roles.json"),
        "cvs": CV_DIR,
        "jds": os.path.join(DATA, "jds"),
        "builds": os.path.join(DATA, "jdbuilds"),
        "worker": os.path.join(HERE, "jobscout", "jd_build.py"),
        "py": PYTHON,
    },
}

def bootstrap():
    """A fresh clone has no database and no board data. Create both so the first
    `python3 serve.py` opens working, empty boards rather than a stack trace."""
    from pathlib import Path
    import state as _state
    conn = _state.connect(Path(BY_ID["tracker"]["db"]))
    try:
        _state.init_db(conn)
        conn.commit()
    finally:
        conn.close()
    roles = BY_ID["apply"]["data"]
    if not os.path.isfile(roles):
        with open(roles, "w") as f:
            json.dump({"updated": datetime.now().date().isoformat(),
                       "open": [], "soon": [], "verify": [], "dead": [],
                       "rules": ""}, f, indent=1)


INDEX = """<!doctype html><meta charset="utf-8"><title>job-scout</title>
<style>body{font:16px/1.6 ui-sans-serif,system-ui;margin:4rem auto;max-width:34rem;padding:0 1.5rem}
a{display:block;padding:.9rem 0;border-bottom:1px solid #ddd;color:inherit;text-decoration:none}
a:hover{color:#c2410c}small{color:#666}</style>
<h1>job-scout</h1>
<a href="/tracker/"><b>Tracker</b><br><small>Every role surfaced, sortable and groupable.</small></a>
<a href="/apply/"><b>Apply Board</b><br><small>What is open now, and build a tailored CV from a pasted job description.</small></a>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    MAX_BODY = 400_000
    NOT_SERVABLE = (".sqlite", ".sqlite3", ".db", ".db-journal", ".py", ".sql",
                    ".env", ".json.tmp")

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # A pasted job description is far too big for a query string, so it arrives as the request
    # body. Capped because nothing legitimate here is a megabyte of text.
    MAX_BODY = 400_000

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        return self.rfile.read(min(n, self.MAX_BODY)) if n > 0 else b""

    def _html(self, body):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path, qs = p.path.rstrip("/") or "/", urllib.parse.parse_qs(p.query)
        if path == "/":
            body = INDEX.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/tracker/api/rows":
            self._json(self.tracker_rows())
        elif path == "/tracker" or path.startswith("/tracker/"):
            self.serve_static(BY_ID["tracker"], path[len("/tracker/"):] if "/" in path[1:] else "")
        elif path == "/apply/api/roles":
            self._json(self.apply_rows())
        elif path == "/apply/api/jdstatus":
            self._json(self.jd_status())
        elif path.startswith("/apply/cv/"):
            self.serve_cv(urllib.parse.unquote(path[len("/apply/cv/"):]))
        elif path == "/apply" or path.startswith("/apply/"):
            self.serve_static(BY_ID["apply"], path[len("/apply/"):] if "/" in path[1:] else "")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        if p.path.startswith("/tracker/api/"):
            self._json(self.tracker_api(p.path.rsplit("/", 1)[-1], qs))
        elif p.path.startswith("/apply/api/"):
            self._json(self.apply_api(p.path.rsplit("/", 1)[-1], qs, self._body()))
        else:
            self._json({"error": "not found"}, 404)

    def _db(self):
        conn = sqlite3.connect(BY_ID["tracker"]["db"], timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def tracker_rows(self):
        with self._db() as c:
            return [dict(r) for r in c.execute(
                "SELECT job_key, title, company, source, url, location, stage,"
                "       score, reason, status, first_seen, applied_at, category FROM jobs")]

    # The status vocabulary, shared by BOTH boards. state.sqlite is the single source of truth:
    # the Apply Board used to keep its own `applied` boolean in roles.json, so ticking a box on
    # one board left the other stale. Nothing is ever deleted, so every state is reversible and
    # a dismissed role can still never be re-surfaced (dedup keys off the table, not the status).
    TRACKER_STATUSES = {"surfaced", "applied", "interviewing", "offer",
                        "rejected", "parked", "not-interested"}
    # Which status wins when one card covers several listings (BlackRock's "Application 1 of 2"
    # spans two job_keys). Furthest through the process wins; 'surfaced' is the floor.
    STATUS_RANK = ["surfaced", "not-interested", "parked", "applied",
                   "rejected", "interviewing", "offer"]

    def set_status(self, keys, status):
        """Write one status across every listing a card covers. Returns rows touched."""
        now = datetime.now().isoformat(timespec="seconds")
        applied_at = now if status == "applied" else None
        n = 0
        with self._db() as c:
            for k in keys:
                n += c.execute(
                    "UPDATE jobs SET status=?, status_at=?, applied_at=COALESCE(applied_at,?) "
                    "WHERE job_key=?", (status, now, applied_at, k)).rowcount
        return n

    # Statuses that represent a real outcome or a decision already taken. Parking must never
    # overwrite one of these.
    PARK_PROTECTED = {"applied", "interviewing", "offer", "rejected", "not-interested",
                      "parked", "seeded"}

    def _company_ident(self, name):
        """Reuse job-scout's own company normalisation so 'Citadel' and 'Citadel Securities'
        stay distinct while 'Palantir' and 'Palantir Technologies' do not."""
        import sys
        proj = os.path.join(HERE, "jobscout")
        if proj not in sys.path:
            sys.path.insert(0, proj)
        from state import company_ident
        return company_ident(name)

    def tracker_park_company(self, key, dry=False):
        """Park every OTHER still-open role at the same employer.

        Deliberately manual. Several firms let one application cover several postings (Citadel's
        form has a 'tick other postings you might be interested in' box), so once you have applied
        there the siblings are not separate things to do. Nothing here is automated: it only ever
        runs when the button is pressed, and it never touches a role that is already applied,
        rejected, interviewing, an offer, or one you have already dismissed."""
        with self._db() as c:
            row = c.execute("SELECT company,title FROM jobs WHERE job_key=?", (key,)).fetchone()
            if not row:
                return {"ok": False, "error": "unknown job_key"}
            target = self._company_ident(row["company"])
            siblings = [dict(r) for r in c.execute(
                "SELECT job_key,company,title,status FROM jobs WHERE job_key!=?", (key,))
                if r["status"] not in self.PARK_PROTECTED
                and self._company_ident(r["company"]) == target]
            if dry:
                return {"ok": True, "company": row["company"], "would_park": len(siblings),
                        "roles": [s["title"] for s in siblings]}
            for s in siblings:
                self.set_status([s["job_key"]], "parked")
        return {"ok": True, "company": row["company"], "parked": len(siblings),
                "roles": [s["title"] for s in siblings]}

    def tracker_queue(self, key):
        """Put one tracked role onto the Apply Board, on demand.

        The scan used to push every role scoring >= 75 across automatically, which is how the
        board reached 22 cards of which 21 were already settled. Discovery belongs on the
        Tracker; the Apply Board is a working list, so what lands there is now Ramy's call."""
        with self._db() as c:
            r = c.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
        if not r:
            return {"ok": False, "error": "unknown job_key"}
        r = dict(r)
        path = BY_ID["apply"]["data"]
        with open(path) as f:
            data = json.load(f)
        # "soon" is the opens-later calendar, not a working list, and nothing ever moved a row
        # out of it once its date arrived: Goldman Sachs sat there at fit 87 the day after it
        # opened, and the queue button refused to add it because the key was technically
        # "already on the board". A key that appears ONLY in soon is therefore a promotion, not
        # a duplicate -- drop the calendar row and carry on building the real card below.
        promoted_from_soon = None
        for bucket in ("open", "verify", "dead"):
            for e in data.get(bucket, []):
                if key in (e.get("job_keys") or []):
                    return {"ok": False, "error": f"already on the board in '{bucket}'"}
        for e in list(data.get("soon", [])):
            if key in (e.get("job_keys") or []):
                data["soon"].remove(e)
                promoted_from_soon = e.get("company") or key

        cv = None
        cvdir = BY_ID["apply"]["cvs"]
        if os.path.isdir(cvdir):
            taken = {e.get("cv") for bucket in ("open", "verify", "dead", "soon")
                     for e in data.get(bucket, []) if e.get("cv")}
            want = re.sub(r"[^a-z0-9]", "", (r["company"] or "").lower())[:6]
            for fn in sorted(os.listdir(cvdir)):
                if not fn.endswith(".pdf") or fn in taken:
                    continue
                stem = fn[len(config.CV_PREFIX):] if fn.startswith(config.CV_PREFIX) else fn
                if want and re.sub(r"[^a-z0-9]", "", stem.lower()).startswith(want):
                    cv = fn
                    break

        slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-",
                      f"{r['company']} {r['title']}".lower())).strip("-")[:48]
        entry = {"id": slug, "company": r["company"], "role": r["title"],
                 "location": r["location"] or "UK", "fit": r["score"],
                 "deadline": None, "deadline_label": "Rolling", "url": r["url"] or "",
                 "note": (r["reason"] or "").strip(), "job_keys": [key],
                 "pick": (r["score"] or 0) >= 84}
        if cv:
            entry["cv"] = cv
            bucket = "open"
        else:
            entry["state"] = "needs-jd"
            bucket = "verify"
        data[bucket].append(entry)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)          # atomic: a crash mid-write cannot corrupt the board
        return {"ok": True, "bucket": bucket, "cv": cv,
                "promoted_from_soon": promoted_from_soon}

    def tracker_api(self, action, qs):
        if action == "queue":
            return self.tracker_queue((qs.get("job_key") or [""])[0])
        if action == "park-company":
            return self.tracker_park_company((qs.get("job_key") or [""])[0],
                                             dry=(qs.get("dry") or [""])[0] == "1")
        if action != "mark":
            return {"ok": False, "error": "unknown action"}
        key = (qs.get("job_key") or [""])[0]
        status = (qs.get("status") or ["applied"])[0]
        if status not in self.TRACKER_STATUSES:
            return {"ok": False, "error": f"status must be one of {sorted(self.TRACKER_STATUSES)}"}
        if not self.set_status([key], status):
            return {"ok": False, "error": "unknown job_key"}
        return {"ok": True, "status": status}

    # ── Action Board ────────────────────────────────────────────────────────
    def cv_path(self, name):
        """Resolve a CV filename to a real path inside the CVs dir, or None."""
        base = os.path.realpath(BY_ID["apply"]["cvs"])
        fp = os.path.realpath(os.path.join(base, os.path.basename(name)))
        ok = fp.startswith(base + os.sep) and os.path.isfile(fp) and fp.lower().endswith(".pdf")
        return fp if ok else None

    def serve_cv(self, name):
        fp = self.cv_path(name)
        if not fp:
            self._json({"error": "no such CV"}, 404)
            return
        # inline so Chrome previews it in a tab; the drag-out path uses DownloadURL
        self.send_file(fp, ctype="application/pdf",
                       extra=[("Content-Disposition",
                               'inline; filename="%s"' % os.path.basename(fp))])

    # ── Paste a JD, get a CV ────────────────────────────────────────────────
    # The Apply Board can now do the thing that used to require opening a Claude session:
    # paste the job description onto the card and a tailored CV gets built against it. The
    # Hub only queues and reports; jd_build.py owns the build so it stays runnable from a
    # terminal and a long build cannot block the server.
    JD_MIN = 120          # shorter than this is a mis-paste, not a job description
    JD_MAX_PENDING = 4    # builds are serialised, so a deep queue just means a long wait

    def jd_status(self):
        """Every build we know about, keyed by card id, for the board to poll."""
        out = {}
        d = BY_ID["apply"]["builds"]
        for fp in glob.glob(os.path.join(d, "*.json")):
            try:
                with open(fp) as f:
                    s = json.load(f)
            except Exception:  # noqa: BLE001
                continue       # a status file caught mid-write: it'll be there next poll
            if s.get("id"):
                out[s["id"]] = s
        return out

    def jd_start(self, rid, body):
        t = BY_ID["apply"]
        rid = re.sub(r"[^A-Za-z0-9._-]+", "-", rid or "").strip("-.")[:80]
        if not rid:
            return {"ok": False, "error": "missing card id"}
        with open(t["data"]) as f:
            data = json.load(f)
        card = next((r for b in ("verify", "open", "soon", "dead")
                     for r in data.get(b, []) if r.get("id") == rid), None)
        if not card:
            return {"ok": False, "error": "unknown role"}

        text = body.decode("utf-8", errors="replace").strip()
        if len(text) < self.JD_MIN:
            return {"ok": False,
                    "error": "that is only %d characters - paste the whole job description"
                             % len(text)}

        live = [s for s in self.jd_status().values() if s.get("state") in ("queued", "running")]
        if any(s["id"] == rid for s in live):
            return {"ok": False, "error": "a build for this role is already running"}
        if len(live) >= self.JD_MAX_PENDING:
            return {"ok": False,
                    "error": "%d builds already queued - let those finish first" % len(live)}

        os.makedirs(t["jds"], exist_ok=True)
        jd_file = os.path.join(t["jds"], rid + ".txt")
        tmp = jd_file + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, jd_file)

        # Seed the status here so the card flips to 'queued' on this response rather than on
        # whichever poll first catches the worker starting up.
        os.makedirs(t["builds"], exist_ok=True)
        st = {"id": rid, "state": "queued", "company": card.get("company"),
              "role": card.get("role"), "jd_chars": len(text),
              "at": datetime.now().isoformat(timespec="seconds")}
        stmp = os.path.join(t["builds"], rid + ".json.tmp")
        with open(stmp, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(stmp, os.path.join(t["builds"], rid + ".json"))

        logf = open(os.path.join(LOGDIR, "apply-jd.log"), "a")
        subprocess.Popen([t["py"], t["worker"], "--id", rid, "--jd", jd_file],
                         cwd=os.path.dirname(t["worker"]), stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "state": "queued", "chars": len(text)}

    def jd_dismiss(self, rid):
        """Clear a finished or failed build so the card goes back to a clean slate."""
        rid = re.sub(r"[^A-Za-z0-9._-]+", "-", rid or "").strip("-.")[:80]
        fp = os.path.join(BY_ID["apply"]["builds"], rid + ".json")
        if not rid or not os.path.isfile(fp):
            return {"ok": False, "error": "nothing to clear"}
        with open(fp) as f:
            if json.load(f).get("state") in ("queued", "running"):
                return {"ok": False, "error": "that build is still running"}
        os.remove(fp)
        return {"ok": True}

    def apply_api(self, action, qs, body=b""):
        data_path = BY_ID["apply"]["data"]
        if action == "jd":
            return self.jd_start((qs.get("id") or [""])[0], body)
        if action == "jddismiss":
            return self.jd_dismiss((qs.get("id") or [""])[0])
        if action == "reveal":
            fp = self.cv_path((qs.get("cv") or [""])[0])
            if not fp:
                return {"ok": False, "error": "no such CV"}
            subprocess.run(["open", "-R", fp])
            return {"ok": True}
        if action == "remove":
            # Takes the card off the board only. The role stays in state.sqlite and on the
            # Tracker, so nothing is lost and it can be queued back across at any time.
            rid = (qs.get("id") or [""])[0]
            with open(data_path) as f:
                data = json.load(f)
            found = None
            for bucket in ("open", "verify", "dead", "soon"):
                for e in list(data.get(bucket, [])):
                    if e.get("id") == rid:
                        data[bucket].remove(e)
                        found = (bucket, e.get("role"))
            if not found:
                return {"ok": False, "error": "unknown card"}
            tmp = data_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, data_path)
            return {"ok": True, "removed": found[1], "from": found[0]}
        if action == "mark":
            rid = (qs.get("id") or [""])[0]
            status = (qs.get("status") or [""])[0]
            if not status:   # legacy callers sent applied=true/false
                status = "applied" if (qs.get("applied") or ["false"])[0] == "true" else "surfaced"
            if status not in self.TRACKER_STATUSES:
                return {"ok": False,
                        "error": f"status must be one of {sorted(self.TRACKER_STATUSES)}"}
            with open(data_path) as f:
                data = json.load(f)
            hit = next((r for bucket in ("open", "verify", "dead", "soon")
                        for r in data.get(bucket, []) if r.get("id") == rid), None)
            if not hit:
                return {"ok": False, "error": "unknown role"}

            keys = hit.get("job_keys") or []
            if keys and self.set_status(keys, status):
                return {"ok": True, "status": status, "where": "state.sqlite"}
            # A card with no listing behind it (hand-written entries like Jane Street) has
            # nowhere in the database to record this, so it keeps its status on the card.
            hit["status"] = status
            tmp = data_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, data_path)
            return {"ok": True, "status": status, "where": "roles.json"}
        return {"ok": False, "error": "unknown action"}

    def apply_rows(self):
        """roles.json is the editorial layer; state.sqlite owns status. Join them here so the
        board and the tracker can never disagree, and attach the per-company rollup that
        answers 'how many have I already sent to this employer'."""
        with open(BY_ID["apply"]["data"]) as f:
            data = json.load(f)
        with self._db() as c:
            db = {r["job_key"]: dict(r) for r in c.execute(
                "SELECT job_key,status,status_at,applied_at,tailoring FROM jobs")}

        rank = {s: i for i, s in enumerate(self.STATUS_RANK)}
        buckets = [b for b in ("open", "verify", "dead", "soon") if b in data]
        for b in buckets:
            for e in data[b]:
                rows = [db[k] for k in (e.get("job_keys") or []) if k in db]
                if rows:
                    best = max(rows, key=lambda r: rank.get(r["status"], 0))
                    e["status"] = best["status"]
                    e["status_at"] = best["status_at"]
                    e["applied_at"] = best["applied_at"]
                    # weakest provenance across the listings this card covers, so the warning
                    # is never hidden by a better-sourced sibling
                    tail = [r["tailoring"] for r in rows if r["tailoring"]]
                    order = {"title-only": 0, "search": 1, "jd": 2}
                    if tail:
                        e["tailoring"] = min(tail, key=lambda t: order.get(t, 1))
                else:
                    e.setdefault("status", "surfaced")
                # A card you pasted a JD for was tailored against the employer's real text.
                # For hand-written cards (Citadel, Databricks) there is no database row to
                # carry that, so the card itself is the only place the provenance can live.
                if e.get("jd_pasted"):
                    e["tailoring"] = "jd"
                e["applied"] = e["status"] in ("applied", "interviewing", "offer")

        # Rollup counts APPLICATIONS (cards), not listings: BlackRock is nine rows in the
        # database but two actual applications, and the two is the number that matters.
        stats = {}
        for b in buckets:
            for e in data[b]:
                s = stats.setdefault(e.get("company", ""), {"sent": 0, "rejected": 0,
                                                            "live": 0, "total": 0})
                s["total"] += 1
                if e["status"] in ("applied", "interviewing", "offer"):
                    s["sent"] += 1
                    s["live"] += 1
                elif e["status"] == "rejected":
                    s["sent"] += 1
                    s["rejected"] += 1
        # How many still-open roles sit behind each employer in the database, so a card can offer
        # "park the other N at this company" with a real number rather than a guess.
        parkable = {}
        with self._db() as c:
            for r in c.execute("SELECT company,status FROM jobs"):
                if r["status"] in self.PARK_PROTECTED:
                    continue
                parkable[self._company_ident(r["company"])] = \
                    parkable.get(self._company_ident(r["company"]), 0) + 1
        for b in buckets:
            for e in data[b]:
                e["company_stats"] = stats.get(e.get("company", ""))
                e["parkable"] = parkable.get(self._company_ident(e.get("company", "")), 0)
        return data

    def send_file(self, fp, ctype=None, extra=()):
        ctype = ctype or mimetypes.guess_type(fp)[0] or "application/octet-stream"
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # ── Prep Board ──────────────────────────────────────────────────────────
    # Algorithms live in prepboard/ so they stay testable outside the server.
    def serve_static(self, t, rel):
        if not rel:
            rel = t["index"]
        # prevent path traversal outside the tool root
        base = os.path.realpath(t["root"])
        fp = os.path.realpath(os.path.join(base, rel))
        if fp.lower().endswith(self.NOT_SERVABLE) or "/__pycache__/" in fp:
            self._json({"error": "not found"}, 404)
            return
        if not fp.startswith(base) or not os.path.isfile(fp):
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)



class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    bootstrap()
    print(f"job-scout on http://localhost:{PORT}")
    print(f"  data   {DATA}")
    print(f"  CVs    {CV_DIR}")
    ThreadingServer(("127.0.0.1", PORT), Handler).serve_forever()
