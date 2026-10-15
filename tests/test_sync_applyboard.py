"""Matching a database listing to a board card: same_role and company aliases."""
import sync_applyboard as sa


def toks(t):
    return sa.role_tokens(t)


def test_abbreviations_and_stems_line_up():
    assert toks("SWE Intern") == toks("Software Engineering Internship")


def test_same_role_needs_two_shared_tokens():
    assert not sa.same_role(toks("Quant Research"), toks("Machine Learning Research"))
    assert sa.same_role(toks("Quantitative Researcher"), toks("Quant Research Intern"))


def test_language_discriminators_veto_a_match():
    assert not sa.same_role(toks("Software Engineer (Java)"), toks("Software Engineer (Python)"))
    assert sa.same_role(toks("Software Engineer (Python)"), toks("Software Engineer Python Intern"))


def test_company_aliases_and_generic_suffixes():
    assert sa.company_key("D. E. Shaw & Co") == sa.company_key("DE Shaw")
    assert sa.same_company("palantir", "palantirtechnologies")
    assert not sa.same_company("citadel", "citadelsecurities")   # separate firms


def test_overlap_is_containment_not_jaccard():
    assert sa.overlap({"a", "b"}, {"a", "b", "c", "d"}) == 1.0
    assert sa.overlap(set(), {"a"}) == 0.0
