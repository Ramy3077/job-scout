"""The CV-selection logic: it may choose and order profile facts, and nothing else."""
from pathlib import Path

import pytest

import select_cv as sc

ROOT = Path(__file__).resolve().parent.parent
PROFILE = (ROOT / "config" / "profile.example.md").read_text()

RICH = """# Profile
## Contact
- **Name:** Test Person
- **Email:** t@example.com
- **Location:** Leeds, UK
- **GitHub:** github.com/testperson

## Summary statements
- Backend engineer who likes databases and reliable services.
- Data analyst who likes statistics and machine learning models.

## Education
### Test University
**Degree:** BSc Mathematics, 2024 - 2027
- Relevant modules: Statistics, Algebra, Databases

## Work experience
### Developer Intern - Acme Ltd - Leeds
- **Dates:** 2026
- Bullets:
  - Wrote a Python service backed by PostgreSQL
  - Trained a machine learning model for churn prediction
  - Organised the team lunch rota

## Projects
### Alpha - `github.com/x/alpha`
- **One-liner:** A web crawler
- **Tech:** Python
- Bullets:
  - Crawled a million pages
### Beta - `github.com/x/beta`
- **One-liner:** A machine learning classifier
- **Tech:** scikit-learn
- Bullets:
  - Evaluated a classifier with cross-validation
### Gamma - `github.com/x/gamma`
- **One-liner:** A tiny game
- **Tech:** C
- Bullets:
  - Drew sprites

## Skills
- **Languages:** Python, SQL, C
- **Data:** PostgreSQL, scikit-learn, Pandas
"""


def test_parses_the_shipped_example_profile():
    p = sc.parse_profile(PROFILE)
    assert p["name"] == "Jane Doe"
    assert p["experience"][0]["role"] == "Software Engineering Intern"
    assert p["experience"][0]["org"] == "Example Ltd"
    assert p["education"][0]["qualification"] == "BSc Computer Science"
    assert p["skills"][0]["category"] == "Languages"


def test_ranks_bullets_by_job_description_overlap():
    p = sc.parse_profile(RICH)
    ml = sc.select(p, "We train machine learning models and evaluate predictions.")
    assert ml["experience"][0]["bullets"][0].startswith("Trained a machine learning model")
    be = sc.select(p, "Backend services with PostgreSQL and Python.")
    assert be["experience"][0]["bullets"][0].startswith("Wrote a Python service")


def test_summary_and_projects_follow_the_jd():
    p = sc.parse_profile(RICH)
    ml = sc.select(p, "machine learning classifier statistics")
    assert "statistics" in ml["summary"]
    assert ml["projects"][0]["name"] == "Beta"
    assert len(ml["projects"]) == sc.MAX_PROJECTS      # weak fit (Gamma) is dropped


def test_uninformative_jd_keeps_the_profiles_own_order():
    p = sc.parse_profile(RICH)
    out = sc.select(p, "the and of to a")
    assert out["experience"][0]["bullets"] == p["experience"][0]["bullets"]
    assert out["summary"] == p["summaries"][0]
    assert [s["category"] for s in out["skills"]] == ["Languages", "Data"]


def test_skills_the_profile_lacks_never_appear():
    jd = "Kubernetes, Terraform, Rust and Go required. Python helpful."
    c = sc.build_content(RICH, jd, "x")
    items = {i for s in c["skills"] for i in s["items"]}
    assert not items & {"Kubernetes", "Terraform", "Rust", "Go"}
    assert "Python" in items


def test_job_titles_are_never_relabelled():
    jd = "Senior Staff Principal Machine Learning Architect"
    c = sc.build_content(RICH, jd, "x")
    assert [j["role"] for j in c["experience"]] == ["Developer Intern"]


@pytest.mark.parametrize("jd", [
    "machine learning", "backend postgres", "", "Kubernetes Terraform", "x" * 5000,
    "Python Python Python SQL SQL data data data pipeline",
])
def test_every_output_string_is_a_profile_fact(jd):
    p = sc.parse_profile(RICH)
    content = sc.build_content(RICH, jd, "x")
    assert sc.verify_against_profile(content, p) == []


def test_verifier_catches_fabrication():
    p = sc.parse_profile(RICH)
    content = sc.build_content(RICH, "python", "x")
    content["experience"][0]["bullets"].append("Led a team of 40 engineers")
    content["skills"][0]["items"].append("Kubernetes")
    content["experience"][0]["role"] = "Senior Developer"
    bad = sc.verify_against_profile(content, p)
    assert set(bad) == {"Led a team of 40 engineers", "Kubernetes", "Senior Developer"}


def test_build_content_refuses_to_return_unverified_text(monkeypatch):
    real = sc.to_content

    def tampered(profile, picked, filename):
        c = real(profile, picked, filename)
        c["summary"] = "Invented summary"
        return c

    monkeypatch.setattr(sc, "to_content", tampered)
    with pytest.raises(AssertionError):
        sc.build_content(RICH, "python", "x")


def test_selection_is_deterministic():
    jd = "python postgres machine learning"
    assert sc.build_content(RICH, jd, "x") == sc.build_content(RICH, jd, "x")


def test_bullet_caps():
    many = RICH.replace("  - Organised the team lunch rota", "\n".join(
        f"  - Extra bullet number {i}" for i in range(10)))
    c = sc.build_content(many, "python", "x")
    assert len(c["experience"][0]["bullets"]) == sc.MAX_EXPERIENCE_BULLETS
