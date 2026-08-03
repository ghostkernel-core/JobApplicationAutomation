"""Render a LaTeX-first job application from a structured JSON payload.

Usage:
    python scripts/render_latex_application.py payload.json "2026/Company/YYYY-MM-DD - Role"
    python scripts/render_latex_application.py payload.json "Recruiter CVs/General CV" --cv-only

The payload must contain `cv_payload_en` and `letter_payload_en` for a complete application.
With `--cv-only`, only `cv_payload_en` is required and `cv_payload_de` is optional.
All other payload keys remain optional.

This script renders locked templates from master/LaTeX/templates, compiles PDFs in a
temporary build directory, and copies only finished .tex/.pdf files into the target folder.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_identity import load as load_identity  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "master" / "LaTeX" / "templates"
SHARED_DIR = ROOT / "master" / "LaTeX" / "shared"

# payload key -> (template, document label). The filename is the label prefixed
# with whoever this workspace belongs to, so a second install renames every
# deliverable by editing identity.toml rather than by editing this map.
DOC_LABELS = {
    "cv_payload_en": ("cv_en.tex", "CV"),
    "cv_payload_de": ("cv_de.tex", "Lebenslauf"),
    "letter_payload_en": ("letter_en.tex", "Cover Letter"),
    "letter_payload_de": ("letter_de.tex", "Anschreiben"),
    "interview_prep_payload_en": ("interview_prep_en.tex", "Interview Prep"),
}
DOCS = {
    key: (template, load_identity().doc_name(label))
    for key, (template, label) in DOC_LABELS.items()
}

LATEX_ESCAPE = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def latex_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return "".join(LATEX_ESCAPE.get(ch, ch) for ch in text)


def lines(values: Any, command: str = r"\\") -> str:
    if not values:
        return ""
    if isinstance(values, str):
        values = [values]
    return f" {command}\n".join(latex_escape(item) for item in values if str(item).strip())


def itemize(values: Any) -> str:
    if not values:
        return ""
    return "\n".join(r"\item " + latex_escape(item) for item in values if str(item).strip())


def render_entries(entries: Any, german: bool = False) -> str:
    if not entries:
        return ""
    chunks: list[str] = []
    for entry in entries:
        date = latex_escape(entry.get("date", ""))
        title = latex_escape(entry.get("title", ""))
        company = latex_escape(entry.get("company", ""))
        location = latex_escape(entry.get("location", ""))
        subtitle = ", ".join(part for part in (company, location) if part)
        bullets = itemize(entry.get("bullets", []))
        chunks.append(
            "\n".join(
                [
                    rf"\entry{{{date}}}{{\textbf{{\color{{posblue}}{title}}}}}{{{latex_escape(subtitle)}}}",
                    r"\begin{discitems}",
                    bullets,
                    r"\end{discitems}",
                ]
            )
        )
    return "\n\n".join(chunks)


def render_projects(projects: Any) -> str:
    if not projects:
        return ""
    chunks: list[str] = []
    for project in projects:
        date = latex_escape(project.get("date", ""))
        title = latex_escape(project.get("title", ""))
        bullets = itemize(project.get("bullets", []))
        chunks.append(
            "\n".join(
                [
                    rf"\entry{{{date}}}{{\textbf{{\color{{subblue}}{title}}}}}{{\relax}}",
                    r"\begin{discitems}",
                    bullets,
                    r"\end{discitems}",
                ]
            )
        )
    return "\n\n".join(chunks)


def render_education(education: Any) -> str:
    if not education:
        return ""
    if all(isinstance(entry, dict) for entry in education):
        chunks: list[str] = []
        for entry in education:
            date = latex_escape(entry.get("date", ""))
            degree = latex_escape(entry.get("degree", entry.get("title", "")))
            details = itemize(entry.get("details", entry.get("bullets", [])))
            chunks.append(
                "\n".join(
                    [
                        rf"\entry{{{date}}}{{\textbf{{\color{{subblue}}{degree}}}}}{{\relax}}",
                        r"\begin{dashitems}",
                        details,
                        r"\end{dashitems}",
                    ]
                )
            )
        return "\n\n".join(chunks)
    return "\n".join([r"\begin{dashitems}", itemize(education), r"\end{dashitems}"])


def render_skills(skills: Any) -> str:
    if not skills:
        return ""

    def render_rows(rows: Any) -> str:
        if isinstance(rows, dict):
            iterable = rows.items()
        else:
            iterable = [(row.get("label", ""), row.get("value", "")) for row in rows]
        return "\n".join(rf"\xitem{{{latex_escape(label)}:}}{{{latex_escape(value)}}}" for label, value in iterable)

    if isinstance(skills, list) and all(isinstance(row, dict) and "category" in row for row in skills):
        chunks: list[str] = []
        for category in skills:
            label = latex_escape(category.get("category", ""))
            rows = render_rows(category.get("items", []))
            if not label or not rows:
                continue
            chunks.append("\n".join([rf"\xcat{{{label}:}}", r"\begin{xpert}", rows, r"\end{xpert}"]))
        return "\n\n".join(chunks)

    if isinstance(skills, dict):
        iterable = skills.items()
    else:
        iterable = [(row.get("label", ""), row.get("value", "")) for row in skills]
    rows = "\n".join(rf"\xitem{{{latex_escape(label)}:}}{{{latex_escape(value)}}}" for label, value in iterable)
    return "\n".join([r"\xcat{Technology Stack:}", r"\begin{xpert}", rows, r"\end{xpert}"])


def render_languages(languages: dict) -> str:
    if not languages:
        return ""
    items = list(languages.items())
    count = len(items)
    if count == 3:
        # Three languages is the common case, so it keeps the hand-tuned
        # widths the templates shipped with instead of the generic 1/3 split,
        # which would render fractionally narrower/wider and change existing
        # PDFs for no reason.
        widths = [0.36, 0.34, 0.30]
    else:
        # Generic case: split evenly, two-decimal precision, with any
        # rounding remainder absorbed by the last row so the widths still
        # sum to 1.00.
        share = round(1.0 / count, 2)
        widths = [share] * (count - 1)
        widths.append(round(1.0 - share * (count - 1), 2))
    rows = []
    for index, (label, value) in enumerate(items):
        row = (
            rf"\makebox[{widths[index]:.2f}\textwidth][l]"
            rf"{{\discbul\hspace{{2.2mm}}\textbf{{{latex_escape(label)}:}} {latex_escape(value)}}}"
        )
        if index != count - 1:
            row += "%"
        rows.append(row)
    return "\n".join(rows)


def render_cv_context(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "headline": latex_escape(payload.get("headline", "")),
        "profile": latex_escape(payload.get("profile", "")),
        "experience_entries": render_entries(payload.get("experience", [])),
        "project_entries": render_projects(payload.get("projects", [])),
        "education_items": render_education(payload.get("education", [])),
        "skill_items": render_skills(payload.get("skills", {})),
        "language_items": render_languages(payload.get("languages", {}) or {}),
        # "Place, Date". The place defaults to where this workspace's owner
        # lives rather than to a literal city, which would follow a clone into
        # someone else's CV.
        "date_line": latex_escape(payload.get("date_line") or load_identity().city),
    }


def render_letter_context(payload: dict[str, Any]) -> dict[str, str]:
    bullets = itemize(payload.get("bullets", []))
    bullet_block = "" if not bullets else "\n".join([r"\begin{discitems}", bullets, r"\end{discitems}"])
    return {
        "date": latex_escape(payload.get("date", "")),
        "recipient": lines(payload.get("recipient", [])),
        "subject": latex_escape(payload.get("subject", "")),
        "salutation": latex_escape(payload.get("salutation", "")),
        "paragraphs": "\n\n".join(latex_escape(p) for p in payload.get("paragraphs", []) if str(p).strip()),
        "bullets": bullets,
        "bullet_block": bullet_block,
        "closing": lines(str(payload.get("closing", "")).split("\n")),
        "enclosure": latex_escape(payload.get("enclosure", "")),
    }


def render_prep_blocks(items: Any) -> str:
    if not items:
        return ""
    chunks: list[str] = []
    for entry in items:
        heading = latex_escape(entry.get("name", entry.get("title", entry.get("label", ""))))
        body = latex_escape(entry.get("body", ""))
        use_for = entry.get("use_for", "")
        parts = [rf"\textbf{{\color{{subblue}}{heading}}}\par"]
        if body:
            parts.append(body)
        if use_for:
            parts.append(rf"\textit{{Use for: {latex_escape(use_for)}}}")
        bullets = itemize(entry.get("items", []))
        if bullets:
            parts.append("\n".join([r"\begin{dashitems}", bullets, r"\end{dashitems}"]))
        chunks.append("\n\\par\n".join(parts))
    separator = "\n" + r"\vspace{2pt}\noindent{\color{ruleblue!45!white}\hrule height 0.5pt}\vspace{6pt}" + "\n"
    return separator.join(chunks)


def render_interview_prep_context(payload: dict[str, Any]) -> dict[str, str]:
    pitch = payload.get("pitch", "")
    pitch_paragraphs = pitch if isinstance(pitch, list) else [pitch]
    pitch_rationale = itemize(payload.get("pitch_rationale", []))
    pitch_rationale_block = (
        "" if not pitch_rationale else "\n".join([r"\begin{dashitems}", pitch_rationale, r"\end{dashitems}"])
    )
    context_items = itemize(payload.get("context_notes", []))
    context_block = "" if not context_items else "\n".join([r"\begin{dashitems}", context_items, r"\end{dashitems}"])
    logistics_items = itemize(payload.get("logistics_notes", []))
    logistics_block = (
        "" if not logistics_items else "\n".join([r"\begin{dashitems}", logistics_items, r"\end{dashitems}"])
    )
    return {
        "role_title": latex_escape(payload.get("role_title", "")),
        "folder_note": latex_escape(payload.get("folder_note", "")),
        "pitch": "\n\n".join(latex_escape(p) for p in pitch_paragraphs if str(p).strip()),
        "pitch_rationale_items": pitch_rationale_block,
        "context_items": context_block,
        "theme_blocks": render_prep_blocks(payload.get("themes", [])),
        "example_blocks": render_prep_blocks(payload.get("examples", [])),
        "gap_items": render_prep_blocks(payload.get("gaps", [])),
        "question_items": itemize(payload.get("questions", [])),
        "logistics_items": logistics_block,
        "date_line": latex_escape(payload.get("date_line", "")),
    }


def replace_placeholders(template: str, context: dict[str, str]) -> str:
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def find_latex_engine() -> str | None:
    configured = os.environ.get("LATEX_ENGINE")
    if configured:
        path = Path(configured)
        if path.exists() or shutil.which(configured):
            return configured
    for cmd in ("xelatex", "latexmk"):
        found = shutil.which(cmd)
        if found:
            return found
    return None


def compile_pdf(tex_path: Path, build_dir: Path, engine: str) -> Path:
    if Path(engine).name.lower().startswith("latexmk"):
        cmd = [engine, "-xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name]
    else:
        cmd = [engine, "-interaction=nonstopmode", "-halt-on-error", tex_path.name]

    for _ in range(2):
        result = subprocess.run(cmd, cwd=build_dir, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            log = build_dir / (tex_path.stem + ".compile-output.txt")
            log.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
            raise RuntimeError(f"LaTeX compile failed for {tex_path.name}; see {log}")
    pdf = build_dir / tex_path.with_suffix(".pdf").name
    if not pdf.exists():
        raise RuntimeError(f"LaTeX produced no PDF for {tex_path.name}")
    return pdf


def render_one(key: str, payload: dict[str, Any], target: Path, engine: str | None, compile_enabled: bool) -> None:
    template_name, output_name = DOCS[key]
    template_path = TEMPLATE_DIR / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"Missing template: {template_path}")
    template = template_path.read_text(encoding="utf-8")
    if key.startswith("letter"):
        context = render_letter_context(payload)
    elif key.startswith("interview_prep"):
        context = render_interview_prep_context(payload)
    else:
        context = render_cv_context(payload)
    # The contact block is the same person in every document, but the country
    # name and nationality are written in the document's own language.
    context.update(load_identity().context("de" if key.endswith("_de") else "en"))
    tex_text = replace_placeholders(template, context)
    if re.search(r"\{\{[A-Za-z0-9_]+\}\}", tex_text):
        raise ValueError(f"Unresolved placeholder in rendered {output_name}")

    target_tex = target / output_name
    target_tex.write_text(tex_text, encoding="utf-8")

    if not compile_enabled:
        return
    if engine is None:
        raise RuntimeError("No LaTeX engine found on PATH. Install latexmk/xelatex before PDF rendering.")

    with tempfile.TemporaryDirectory(prefix="latex-build-") as tmp:
        build_dir = Path(tmp)
        work_tex = build_dir / output_name
        work_tex.write_text(tex_text, encoding="utf-8")
        if SHARED_DIR.exists():
            shutil.copytree(SHARED_DIR, build_dir / "shared", dirs_exist_ok=True)
        for dirname in ("fonts", "images"):
            src = ROOT / "master" / "LaTeX" / dirname
            if src.exists():
                shutil.copytree(src, build_dir / dirname, dirs_exist_ok=True)
        pdf = compile_pdf(work_tex, build_dir, engine)
        shutil.copy2(pdf, target / output_name.replace(".tex", ".pdf"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload_json")
    parser.add_argument("target_folder")
    parser.add_argument("--no-pdf", action="store_true", help="Render .tex only; skip PDF compilation")
    parser.add_argument(
        "--cv-only",
        action="store_true",
        help="Render CV/Lebenslauf payloads without requiring or rendering application documents.",
    )
    args = parser.parse_args()

    payload_path = Path(args.payload_json)
    target = Path(args.target_folder)
    data = json.loads(payload_path.read_text(encoding="utf-8"))
    required = ("cv_payload_en",) if args.cv_only else ("cv_payload_en", "letter_payload_en")
    missing = [key for key in required if key not in data]
    if missing:
        print(f"ERROR: payload missing required keys: {', '.join(missing)}", file=sys.stderr)
        return 2

    target.mkdir(parents=True, exist_ok=True)
    engine = None if args.no_pdf else find_latex_engine()
    keys = ("cv_payload_en", "cv_payload_de") if args.cv_only else DOCS
    for key in keys:
        if data.get(key) is None:
            continue
        render_one(key, data[key], target, engine, not args.no_pdf)
    print(f"Rendered {'CVs' if args.cv_only else 'LaTeX application'}: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
