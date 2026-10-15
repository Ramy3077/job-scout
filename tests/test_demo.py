"""Demo mode: invented data, no outside access, and the board flows work end to end."""
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
    board = json.loads((tmp_path / "roles.json").read_text())
    assert board["verify"] and board["soon"] and board["dead"]
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


def test_boards_and_banner(server):
    for path in ("/tracker/", "/apply/"):
        status, body = get(server + path)
        assert status == 200 and "Demo data, resets periodically" in body
        assert body.count('id="demo-banner"') == 1
    assert json.loads(get(server + "/healthz")[1])["demo"] is True
    assert len(json.loads(get(server + "/tracker/api/rows")[1])["rows"]) == 15
    roles = json.loads(get(server + "/apply/api/roles")[1])
    assert roles["verify"] and roles["soon"]


def test_reveal_is_disabled(server):
    res = json.loads(get(server + "/apply/api/reveal?cv=x.pdf", data=b"")[1])
    assert res["ok"] is False


def test_status_change_is_shared_between_boards(server):
    rows = json.loads(get(server + "/tracker/api/rows")[1])["rows"]
    key = next(r["job_key"] for r in rows if r["status"] == "surfaced")
    get(server + "/tracker/api/mark?job_key=" + key + "&status=parked", data=b"")
    rows = json.loads(get(server + "/tracker/api/rows")[1])["rows"]
    assert next(r for r in rows if r["job_key"] == key)["status"] == "parked"


@pytest.mark.skipif(not HAVE_TECTONIC, reason="tectonic not installed")
def test_pasting_a_jd_builds_a_pdf(server):
    roles = json.loads(get(server + "/apply/api/roles")[1])
    card = roles["verify"][0]
    body = card["sample_jd"].encode()
    res = json.loads(get(server + "/apply/api/jd?id=" + card["id"], data=body)[1])
    assert res["ok"]
    for _ in range(60):
        st = json.loads(get(server + "/apply/api/jdstatus")[1])[card["id"]]
        if st["state"] in ("done", "failed"):
            break
        time.sleep(1)
    assert st["state"] == "done", st
    pdf = urllib.request.urlopen(server + "/apply/cv/" + urllib.parse.quote(st["cv"]))
    assert pdf.read(4) == b"%PDF"
    board = json.loads(get(server + "/apply/api/roles")[1])
    built = next(c for c in board["open"] if c["id"] == card["id"])
    assert built["tailoring_report"]["kept"] and built["tailoring_report"]["dropped"]


def test_every_empty_card_has_its_own_sample_jd(tmp_path):
    demo.seed(tmp_path)
    board = json.loads((tmp_path / "roles.json").read_text())
    assert board["demo"] is True
    jds = [c["sample_jd"] for c in board["verify"]]
    assert len(jds) == len(board["verify"]) == len(set(jds))
    for c in board["verify"]:
        assert c["company"].split()[0].lower() in c["sample_jd"].lower()
        assert len(c["sample_jd"]) > 120


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


def test_demo_root_redirects_to_the_apply_board(server):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(server + "/")
    assert e.value.code == 302 and e.value.headers["Location"] == "/apply/"


def test_explain_flags_zero_match_lines_as_filler():
    profile = select_cv.parse_profile(demo.PROFILE_MD)
    rep = select_cv.explain(profile, demo._JD_ML)
    for x in rep["kept"]:
        assert x["filler"] == (x["score"] == 0)
    # No JD terms at all: everything kept is filler, and nothing claims a match.
    blank = select_cv.explain(profile, "zzzz qqqq")
    assert blank["kept"] and all(x["filler"] for x in blank["kept"])
