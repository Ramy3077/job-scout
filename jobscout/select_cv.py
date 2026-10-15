#!/usr/bin/env python3
"""Deterministic CV selection: pick and order facts from a profile, write nothing new.

This is the offline counterpart to the model-driven build in `jd_build.py`, and the one the
hosted demo uses. It is deliberately small and has no model in it, so the guarantee is easy
to state and to check:

    Every string that reaches the CV (summary, bullet, job title, employer, skill) is
    copied verbatim from the profile. The only freedom is WHICH items are kept and in
    WHAT ORDER, ranked by how much each one overlaps with the job description.

`verify_against_profile` re-checks that after the fact, and `build_content` refuses to
return anything that fails it.

    select_cv.py PROFILE.md JD.txt [--filename NAME]    # prints CV-content JSON
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

# Words that say nothing about the job. Kept short on purpose: a long list starts to remove
# real signal ("data", "cloud", "systems").
STOPWORDS = set("""
a about above after all also an and any are as at be been being but by can could do does
each for from had has have he her his how i if in into is it its may more most must no not of
on one or our out over own per role roles she so some such team teams than that the their
them then there these they this those to under up us using very was we were what when where
which while who will with within work working would you your experience join looking
ability strong skills skill candidate candidates company position opportunity responsibilities
requirements required preferred including include etc good great new
""".split())

# Light normalisation so "services" and "service", "pipelines" and "pipeline" match.
_SUFFIXES = ("ing", "es", "s")
_ALIASES = {"js": "javascript", "k8s": "kubernetes", "ml": "machine-learning",
            "postgres": "postgresql", "py": "python"}


def _stem(tok: str) -> str:
    tok = _ALIASES.get(tok, tok)
    for suf in _SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            return tok[: -len(suf)]
    return tok


def tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z][a-z0-9+#.\-]*", (text or "").lower())
    out = []
    for t in raw:
        t = t.strip(".-")
        if len(t) < 2 or t in STOPWORDS:
            continue
        out.append(_stem(t))
    return out


def jd_weights(jd_text: str) -> dict[str, float]:
    """Term weights from the JD. A term mentioned more often matters more, with
    diminishing returns so one repeated word cannot dominate."""
    counts = Counter(tokens(jd_text))
    return {t: 1.0 + math.log(c) for t, c in counts.items()}


def score(text: str, weights: dict[str, float]) -> float:
    """Sum of the weights of the distinct JD terms that appear in `text`."""
    seen = set(tokens(text))
    return round(sum(w for t, w in weights.items() if t in seen), 6)


def rank(items: list, key, weights) -> list:
    """Stable sort, best match first. Ties keep the order the profile gave them, so an
    uninformative JD leaves the profile's own order untouched."""
    scored = [(score(key(it), weights), -i, it) for i, it in enumerate(items)]
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [it for _, _, it in scored]


# ── profile parsing ─────────────────────────────────────────────────────────────
def _field(block: str, name: str) -> str:
    m = re.search(rf"^\s*(?:-\s*)?\*\*{re.escape(name)}:\*\*\s*(.+)$", block, re.M | re.I)
    return m.group(1).strip() if m else ""


def _bullets(block: str) -> list[str]:
    """Indented bullets under a `- Bullets:` line."""
    m = re.search(r"^\s*-\s*Bullets:\s*$", block, re.M)
    out: list[str] = []
    if not m:
        return out
    for line in block[m.end():].splitlines():
        b = re.match(r"^\s{2,}[-*]\s+(.+)$", line)
        if b:
            out.append(b.group(1).strip())
        elif line.strip() and not line.startswith(" "):
            break
    return out


def _sections(text: str, level: str) -> list[tuple[str, str]]:
    parts = re.split(rf"^{level}\s+(.+)$", text, flags=re.M)
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def parse_profile(text: str) -> dict:
    """Read the profile markdown (see config/profile.example.md) into structured facts."""
    top = dict(_sections(text, "##"))
    contact = top.get("Contact", "")
    links = [v for v in (_field(contact, "GitHub"), _field(contact, "LinkedIn"),
                         _field(contact, "Website")) if v]
    summaries = [re.sub(r"\s*\n\s*", " ", m.strip()) for m in re.findall(
        r"^-\s+(.+(?:\n {2,}\S.+)*)", top.get("Summary statements", ""), re.M)]

    education = []
    for org, body in _sections(top.get("Education", ""), "###"):
        mods = re.search(r"Relevant modules:\s*(.+)", body)
        degree = _field(body, "Degree")
        education.append({"org": org, "qualification": degree.split(",")[0].strip(),
                          "date": ", ".join(degree.split(",")[1:]).strip(),
                          "location": _field(body, "Location"),
                          "modules": [m.strip() for m in mods.group(1).split(",")] if mods else []})

    experience = []
    for head, body in _sections(top.get("Work experience", ""), "###"):
        bits = [b.strip() for b in head.split(" - ")]
        experience.append({"role": bits[0], "org": bits[1] if len(bits) > 1 else "",
                           "location": bits[2] if len(bits) > 2 else "",
                           "date": _field(body, "Dates"), "bullets": _bullets(body)})

    projects = []
    for head, body in _sections(top.get("Projects", ""), "###"):
        name = re.sub(r"\s+-\s+`.*$", "", head).strip()
        projects.append({"name": name, "description": _field(body, "One-liner"),
                         "tech": _field(body, "Tech"), "date": _field(body, "Dates"),
                         "bullets": _bullets(body)})

    skills = []
    for m in re.finditer(r"^-\s+\*\*(.+?):\*\*\s*(.+)$", top.get("Skills", ""), re.M):
        skills.append({"category": m.group(1).strip(),
                       "items": [s.strip() for s in m.group(2).split(",") if s.strip()]})

    return {"name": _field(contact, "Name"), "email": _field(contact, "Email"),
            "location": _field(contact, "Location"), "links": links,
            "summaries": summaries, "education": education, "experience": experience,
            "projects": projects, "skills": skills}


# ── selection ───────────────────────────────────────────────────────────────────
MAX_EXPERIENCE_BULLETS = 4
MAX_PROJECTS = 2
MAX_PROJECT_BULLETS = 2


def select(profile: dict, jd_text: str) -> dict:
    """Return the profile cut down and reordered for this JD. Same facts, different order."""
    w = jd_weights(jd_text)

    summary = ""
    if profile["summaries"]:
        summary = rank(profile["summaries"], lambda s: s, w)[0]

    experience = []
    for job in profile["experience"]:
        bullets = rank(job["bullets"], lambda b: b, w)[:MAX_EXPERIENCE_BULLETS]
        experience.append({**job, "bullets": bullets})
    # Roles keep their real titles; only their order can move. Most relevant first.
    experience = rank(experience, lambda j: " ".join(j["bullets"]) + " " + j["role"], w)

    projects = []
    for p in rank(profile["projects"],
                  lambda p: " ".join([p["name"], p["description"], p["tech"]] + p["bullets"]),
                  w)[:MAX_PROJECTS]:
        projects.append({**p, "bullets": rank(p["bullets"], lambda b: b, w)[:MAX_PROJECT_BULLETS]})

    skills = []
    for cat in rank(profile["skills"], lambda c: " ".join(c["items"]), w):
        skills.append({"category": cat["category"], "items": rank(cat["items"], lambda i: i, w)})

    education = [{**ed, "modules": rank(ed["modules"], lambda m: m, w)}
                 for ed in profile["education"]]

    return {"summary": summary, "experience": experience, "projects": projects,
            "skills": skills, "education": education}


# ── the CV-content JSON build_cv.py consumes ────────────────────────────────────
MODULES_PREFIX = "Relevant modules: "


def to_content(profile: dict, picked: dict, filename: str) -> dict:
    edu = [{"org": ed["org"], "qualification": ed["qualification"],
            "location": ed["location"], "date": ed["date"],
            "description": MODULES_PREFIX + ", ".join(ed["modules"]) if ed["modules"] else "",
            "bullets": []} for ed in picked["education"]]
    return {
        "filename": filename,
        "name": profile["name"],
        "updated": date.today().strftime("%B %Y"),
        "contact": {"location": profile["location"], "email": profile["email"],
                    "links": profile["links"]},
        "summary": picked["summary"],
        "education": edu,
        "experience": [{"role": j["role"], "org": j["org"], "location": j["location"],
                        "date": j["date"], "bullets": j["bullets"]}
                       for j in picked["experience"]],
        "projects": [{"name": p["name"], "location": "", "date": p["date"],
                      "description": p["description"], "bullets": p["bullets"]}
                     for p in picked["projects"]],
        "skills": [{"category": c["category"], "items": c["items"]} for c in picked["skills"]],
    }


def fact_set(profile: dict) -> set[str]:
    """Every string the profile asserts. A CV may only contain members of this set."""
    facts = {profile["name"], profile["email"], profile["location"], *profile["links"],
             *profile["summaries"]}
    for ed in profile["education"]:
        facts |= {ed["org"], ed["qualification"], ed["date"], ed["location"], *ed["modules"]}
    for j in profile["experience"]:
        facts |= {j["role"], j["org"], j["location"], j["date"], *j["bullets"]}
    for p in profile["projects"]:
        facts |= {p["name"], p["description"], p["date"], *p["bullets"]}
    for c in profile["skills"]:
        facts |= {c["category"], *c["items"]}
    facts.discard("")
    return facts


def verify_against_profile(content: dict, profile: dict) -> list[str]:
    """Return every string in the CV content that is NOT a fact from the profile.
    An empty list means the CV contains nothing the profile did not already say."""
    facts = fact_set(profile)
    bad: list[str] = []

    def check(s):
        if s and s not in facts:
            bad.append(s)

    check(content.get("name"))
    c = content.get("contact", {})
    check(c.get("location"))
    check(c.get("email"))
    for link in c.get("links", []):
        check(link)
    check(content.get("summary"))
    for ed in content.get("education", []):
        for k in ("org", "qualification", "location", "date"):
            check(ed.get(k))
        d = ed.get("description", "")
        if d:
            if not d.startswith(MODULES_PREFIX):
                bad.append(d)
            else:
                for m in d[len(MODULES_PREFIX):].split(", "):
                    check(m)
    for j in content.get("experience", []):
        for k in ("role", "org", "location", "date"):
            check(j.get(k))
        for b in j.get("bullets", []):
            check(b)
    for p in content.get("projects", []):
        for k in ("name", "date", "description"):
            check(p.get(k))
        for b in p.get("bullets", []):
            check(b)
    for s in content.get("skills", []):
        check(s.get("category"))
        for i in s.get("items", []):
            check(i)
    return bad


def build_content(profile_text: str, jd_text: str, filename: str) -> dict:
    profile = parse_profile(profile_text)
    content = to_content(profile, select(profile, jd_text), filename)
    bad = verify_against_profile(content, profile)
    if bad:  # a bug in this module, not a user error: fail loudly rather than ship it
        raise AssertionError("CV contains text that is not in the profile: %r" % bad[:3])
    return content


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profile")
    ap.add_argument("jd")
    ap.add_argument("--filename", default="CV")
    a = ap.parse_args()
    out = build_content(Path(a.profile).read_text(), Path(a.jd).read_text(), a.filename)
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
