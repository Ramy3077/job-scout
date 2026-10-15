"""Demo mode: invented data, no outside access, and The Scout's flows work end to end."""
import json
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

import config
import demo
import select_cv

HAVE_TECTONIC = shutil.which("tectonic") is not None


def get(url, data=None):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req) as r:
        return r.status, r.read().decode()


@pytest.fixture(scope="module")
def server():
    import serve
    serve.bootstrap()
    srv = serve.ThreadingServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_demo_uses_a_throwaway_data_dir():
    assert config.DEMO
    assert "jobscout-test" in str(config.DATA)


def test_seed_is_all_invented_and_varied(tmp_path):
    demo.seed(tmp_path)
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    rows = conn.execute("SELECT url,status,company FROM jobs").fetchall()
    assert len(rows) == 15
    assert all(u.startswith("https://example.com/") for u, _, _ in rows)
    assert len({s for _, s, _ in rows}) >= 6
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE deadline IS NOT NULL").fetchone()[0] >= 3
    assert "Alex Example" in (tmp_path / "profile.md").read_text()


def test_demo_profile_cannot_produce_text_outside_itself():
    for jd in (demo.SAMPLE_JD, demo._JD_ML, "Kubernetes Rust Haskell Terraform"):
        c = select_cv.build_content(demo.PROFILE_MD, jd, "x")
        assert select_cv.verify_against_profile(c, select_cv.parse_profile(demo.PROFILE_MD)) == []


def test_reseed_resets_visitor_changes(tmp_path):
    demo.seed(tmp_path)
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.execute("UPDATE jobs SET status='rejected'")
    conn.commit()
    conn.close()
    demo.seed(tmp_path)
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='rejected'").fetchone()[0] < 15


def test_fetchers_are_disabled_in_demo():
    import fetch_mail
    import fetch_trackr
    for mod in (fetch_mail, fetch_trackr):
        with pytest.raises(SystemExit) as e:
            mod.main()
        assert e.value.code == 6


def post(server, path, data=b""):
    return json.loads(get(server + path, data=data)[1])


def state(server):
    return json.loads(get(server + "/api/state")[1])


def test_app_and_banner(server):
    status, body = get(server + "/")
    assert status == 200 and "The Scout" in body and "/app.js" in body
    assert body.count('id="demo-banner"') == 1
    for asset in ("/app.js", "/style.css"):
        assert get(server + asset)[0] == 200
    assert json.loads(get(server + "/healthz")[1])["demo"] is True


def test_state_has_fixtures_and_season(server):
    s = state(server)
    assert s["demo"] is True and s["target"] == 5
    assert 1 <= len(s["five"]) <= 5
    assert len({f["company"] for f in s["five"]}) == len(s["five"])      # one per employer
    assert all(f["fit"] >= 75 for f in s["five"])
    assert any(f["cv"] and f["report"]["kept"] for f in s["five"])       # a built CV and its report
    assert any(not f["cv"] and f["sample_jd"] for f in s["five"])        # and one to build
    assert s["season"]["sent"] == 5 and s["season"]["tracked"] == 15
    assert s["waiting"] and s["ticker"] and s["form"]


def test_roles_views(server):
    rows = lambda v: json.loads(get(server + "/api/roles?view=" + v)[1])["rows"]
    assert len(rows("all")) == 15
    assert all(r["status"] == "surfaced" for r in rows("open"))
    assert {r["status"] for r in rows("sent")} <= {"applied", "interviewing", "offer"}
    assert [r["status"] for r in rows("out")] == ["rejected"]
    hit = json.loads(get(server + "/api/roles?view=all&q=nimbus")[1])["rows"]
    assert len(hit) == 1 and hit[0]["company"] == "Nimbus Quarry"


def test_status_pick_and_add(server):
    s = state(server)
    key = s["five"][-1]["job_key"]
    assert post(server, "/api/pick?job_key=" + urllib.parse.quote(key) + "&pick=skipped")["ok"]
    assert key not in [f["job_key"] for f in state(server)["five"]]
    assert post(server, "/api/pick?job_key=" + urllib.parse.quote(key) + "&pick=pinned")["ok"]
    assert state(server)["five"][0]["job_key"] == key                    # picks come first
    assert post(server, "/api/mark?job_key=" + urllib.parse.quote(key) + "&status=parked")["ok"]
    rows = json.loads(get(server + "/api/roles?view=shelved")[1])["rows"]
    assert key in [r["job_key"] for r in rows]
    bad = urllib.request.Request(server + "/api/mark?job_key=x&status=hired", data=b"", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(bad)
    assert e.value.code == 400
    res = post(server, "/api/add?company=Example+Co&title=Data+Intern&url=https://example.com/x")
    assert res["ok"] and res["job_key"] in [f["job_key"] for f in state(server)["five"]]


def test_short_jd_is_refused(server):
    key = state(server)["five"][0]["job_key"]
    res = post(server, "/api/jd?job_key=" + urllib.parse.quote(key), data=b"too short")
    assert res["ok"] is False and "characters" in res["error"]


def test_static_files_cannot_escape_the_app_dir(server):
    for path in ("/../serve.py", "/%2e%2e/serve.py", "/state.sqlite"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(server + path)
        assert e.value.code in (400, 404)


@pytest.mark.skipif(not HAVE_TECTONIC, reason="tectonic not installed")
def test_pasting_a_jd_builds_a_pdf(server):
    import demo as _demo
    _demo.seed(config.DATA)
    fx = next(f for f in state(server)["five"] if not f["cv"] and f["sample_jd"])
    key = urllib.parse.quote(fx["job_key"])
    res = post(server, "/api/jd?job_key=" + key, data=fx["sample_jd"].encode())
    assert res["ok"]
    for _ in range(60):
        st = json.loads(get(server + "/api/jdstatus")[1])[fx["job_key"]]
        if st["state"] in ("done", "failed"):
            break
        time.sleep(1)
    assert st["state"] == "done", st
    pdf = urllib.request.urlopen(server + "/cv/" + urllib.parse.quote(st["cv"]) + "?dl=1")
    assert "attachment" in pdf.headers["Content-Disposition"]
    assert pdf.read(4) == b"%PDF"
    built = next(f for f in state(server)["five"] if f["job_key"] == fx["job_key"])
    assert built["cv"] == st["cv"] and built["tailoring"] == "jd"
    assert built["report"]["kept"] and built["report"]["dropped"]
    assert post(server, "/api/jddismiss?job_key=" + key)["ok"]
    assert fx["job_key"] not in json.loads(get(server + "/api/jdstatus")[1])


def test_every_open_role_without_a_cv_has_its_own_sample_jd(tmp_path):
    demo.seed(tmp_path)
    board = json.loads((tmp_path / "roles.json").read_text())
    with_cv = {k for c in board["open"] for k in c["job_keys"]}
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    todo = [(k, co, t) for k, co, t in conn.execute(
        "SELECT job_key,company,title FROM jobs WHERE status='surfaced'") if k not in with_cv]
    assert todo
    jds = [demo.sample_for(co, t) for _, co, t in todo]
    assert all(jds) and len(set(jds)) == len(jds)
    for (_, co, _), jd in zip(todo, jds):
        assert co.split()[0].lower() in jd.lower() and len(jd) > 120


def test_timestamps_carry_an_offset():
    from datetime import datetime
    assert datetime.fromisoformat(config.now_iso()).tzinfo is not None


def test_explain_matches_what_select_keeps():
    profile = select_cv.parse_profile(demo.PROFILE_MD)
    rep = select_cv.explain(profile, demo._JD_ML)
    facts = select_cv.fact_set(profile)
    assert rep["kept"] and rep["dropped"]
    for x in rep["kept"] + rep["dropped"]:
        assert x["text"] in facts and 0 <= x["score"] <= 100


def test_old_board_links_redirect_to_the_app(server):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    for path in ("/apply/", "/tracker/"):
        with pytest.raises(urllib.error.HTTPError) as e:
            opener.open(server + path)
        assert e.value.code == 301 and e.value.headers["Location"] == "/"


def test_explain_flags_zero_match_lines_as_filler():
    profile = select_cv.parse_profile(demo.PROFILE_MD)
    rep = select_cv.explain(profile, demo._JD_ML)
    for x in rep["kept"]:
        assert x["filler"] == (x["score"] == 0)
    # No JD terms at all: everything kept is filler, and nothing claims a match.
    blank = select_cv.explain(profile, "zzzz qqqq")
    assert blank["kept"] and all(x["filler"] for x in blank["kept"])
