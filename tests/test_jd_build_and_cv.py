import json

import build_cv
import jd_build


def test_track_drops_cycle_words_and_keeps_the_role():
    assert jd_build.track_of("Machine Learning Research Internship") == "Machine Learning Research"
    assert jd_build.track_of("Trader (Intern)") == "Trader"
    assert jd_build.track_of("Summer 2027 Internship") == "Internship"


def test_track_is_cut_on_a_word_boundary():
    out = jd_build.track_of("Distributed Systems Reliability Engineering Platform Tooling Role", 30)
    assert len(out) <= 30 and not out.endswith("Platfo")


def test_filenames_are_clean():
    card = {"company": "D. E. Shaw", "role": "Quant: Research/Intern"}
    name = jd_build.expected_cv(card)
    assert name.startswith(jd_build.CV_PREFIX) and name.endswith(".pdf")
    assert ":" not in name and "/" not in name


def test_rebuild_keeps_the_existing_filename():
    assert jd_build.expected_cv({"cv": "Keep Me.pdf", "company": "X", "role": "Y"}) == "Keep Me.pdf"


def test_card_ids_cannot_escape_the_jds_directory():
    assert "/" not in jd_build.safe_id("../../etc/passwd")
    assert jd_build.safe_id("../..") == ""


def test_latex_escape_and_em_dash_normalisation():
    assert build_cv.latex_escape("R&D 50% #1_a") == r"R\&D 50\% \#1\_a"
    assert "—" not in build_cv.latex_escape("a — b")
    assert build_cv.latex_escape("2024 – 2025") == "2024 – 2025"   # en dash kept
    assert build_cv.latex_escape(None) == ""


def test_template_renders_the_content(tmp_path):
    env = build_cv.make_env(build_cv.Path(build_cv.__file__).parent)
    data = {"name": "A & B", "updated": "May 2026",
            "contact": {"location": "X", "email": "a@b.c", "links": []},
            "summary": "s", "education": [], "projects": [], "skills": [],
            "experience": [{"role": "Dev", "org": "Co", "location": "", "date": "2026",
                            "bullets": ["did 100% of it"]}]}
    tex = env.get_template("template.tex.j2").render(**data)
    assert r"A \& B" in tex and r"did 100\% of it" in tex
