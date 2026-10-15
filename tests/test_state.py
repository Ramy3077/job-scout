"""Dedup keys and identity: the logic that stops one role becoming two."""
import state


def test_job_key_ignores_tracking_params_and_fragment_order():
    a = state.job_key({"url": "https://Jobs.Example.com/role/1?utm_source=x&gh_jid=7&ref=abc"})
    b = state.job_key({"url": "https://jobs.example.com/role/1?gh_jid=7&utm_campaign=y"})
    assert a == b == "jobs.example.com/role/1?gh_jid=7"


def test_job_key_keeps_the_query_that_identifies_the_role():
    a = state.job_key({"url": "https://ats.example.com/apply?token=aaa"})
    b = state.job_key({"url": "https://ats.example.com/apply?token=bbb"})
    assert a != b


def test_job_key_falls_back_to_a_signature_for_mail_permalinks():
    k = state.job_key({"url": "https://mail.google.com/mail/u/0/#inbox/abc",
                       "source": "LinkedIn", "company": "Acme", "title": "SWE Intern"})
    assert k == "sig:linkedin-acme-swe-intern"


def test_company_ident_collapses_corporate_suffixes():
    assert state.company_ident("Palantir") == state.company_ident("Palantir Technologies")
    assert state.company_ident("Credit Agricole CIB") == state.company_ident("Crédit Agricole CIB")


def test_role_identity_matches_across_sources():
    a = state.role_identity("Palantir", "Software Engineer Intern 2027")
    b = state.role_identity("Palantir Technologies", "Software Engineer Intern")
    assert a == b != ""


def test_internship_and_graduate_programmes_stay_distinct():
    a = state.role_identity("Acme", "Internship Programme: Software Engineer")
    b = state.role_identity("Acme", "Graduate Programme: Software Engineer")
    assert a != b


def test_title_repeating_the_employer_does_not_change_identity():
    a = state.role_identity("Tencent", "Tencent Agent Development Internship")
    b = state.role_identity("Tencent", "Agent Development Internship")
    assert a == b


def test_init_db_is_idempotent_and_has_status_columns(tmp_path):
    conn = state.connect(tmp_path / "s.sqlite")
    state.init_db(conn)
    state.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"status", "status_at", "tailoring", "category"} <= cols
