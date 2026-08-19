#!/usr/bin/env python3
"""Render a tailored CV from a JSON content file + the Jinja LaTeX template, then
compile to PDF with tectonic.

Usage:
    build_cv.py CONTENT.json [-o OUTDIR] [-t TEMPLATE] [--keep-tex]

The JSON schema is documented in sample_cv.json. All string fields are LaTeX-escaped
automatically, so the LLM that produces the JSON can write plain text (incl. & % $ # _).
"""
from __future__ import annotations
import argparse
import json
import shutil
from datetime import datetime
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

# Typographic normalisation, applied BEFORE escaping. Em dashes are stripped because
# they read as an "AI-generated" tell on a CV; en dashes (used in date ranges) are kept.
_NORMALIZE = [
    ("—", "-"),  # em dash      —  -> hyphen
    ("―", "-"),  # horizontal bar ―  -> hyphen
]

# Order matters: backslash first so we don't double-escape the replacements.
_LATEX_REPLACEMENTS = [
    ("\\", r"\textbackslash{}"),
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
]


def latex_escape(value) -> str:
    if value is None:
        return ""
    text = str(value)
    for needle, repl in _NORMALIZE:
        text = text.replace(needle, repl)
    for needle, repl in _LATEX_REPLACEMENTS:
        text = text.replace(needle, repl)
    return text


def make_env(template_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        block_start_string="<%",
        block_end_string="%>",
        variable_start_string="<<",
        variable_end_string=">>",
        comment_start_string="<#",
        comment_end_string="#>",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        keep_trailing_newline=True,
    )
    env.filters["e"] = latex_escape
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("data", help="CV content JSON file")
    ap.add_argument("-o", "--outdir", default=".", help="output directory for the PDF")
    ap.add_argument(
        "-t",
        "--template",
        default=str(Path(__file__).parent / "template.tex.j2"),
        help="Jinja LaTeX template",
    )
    ap.add_argument("--keep-tex", action="store_true", help="keep the generated .tex")
    args = ap.parse_args()

    if not shutil.which("tectonic"):
        print("error: 'tectonic' not found on PATH", file=sys.stderr)
        return 2

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    template_path = Path(args.template)
    env = make_env(template_path.parent)
    rendered = env.get_template(template_path.name).render(**data)

    outdir = Path(args.outdir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    stem = data.get("filename") or Path(args.data).stem

    # Snapshot any CV we are about to replace. The filename rule deliberately overwrites rather
    # than minting dated variants, but on 2026-08-10 a role re-surfaced under a link-shortener URL
    # and a title-only rebuild silently destroyed a CV that had been written against the real
    # fetched JD. Overwriting is still the right default; losing the previous file is not.
    existing = outdir / f"{stem}.pdf"
    if existing.is_file():
        archive = outdir / "Archive"
        archive.mkdir(exist_ok=True)
        stamp = datetime.fromtimestamp(existing.stat().st_mtime).strftime("%Y-%m-%d_%H%M")
        shutil.copy2(existing, archive / f"{stem} ({stamp}).pdf")

    tex_path = outdir / f"{stem}.tex"
    tex_path.write_text(rendered, encoding="utf-8")

    proc = subprocess.run(
        ["tectonic", str(tex_path), "--outdir", str(outdir), "--chatter", "minimal"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + "\n" + proc.stderr + "\n")
        print(f"error: tectonic failed to compile {tex_path}", file=sys.stderr)
        return 1

    if not args.keep_tex:
        tex_path.unlink(missing_ok=True)

    print(str(outdir / f"{stem}.pdf"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
