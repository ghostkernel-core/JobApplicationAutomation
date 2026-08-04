"""One deterministic QA pass over a finished application folder.

The proofreader and the final verifier used to each run the healthcheck, each
grep the extracted PDF text for the same AI-fingerprint list, each count pages,
and each rasterise every PDF. On one measured run that was ten healthcheck
invocations, three rasterisation passes, and two identical fingerprint greps
over documents that had not changed between them — several minutes of wall
clock spent re-deriving the same verdict.

This script does all of it once and prints a single JSON report. The models read
the report and look only at the page images it points them at. What is left for
them is what actually needs a reader: language, tone, and whether the rendered
page looks right.

Deterministic findings are facts, not opinions -- if this script reports a
fingerprint hit or a page overflow, that is a defect to fix, not a judgement
call to re-litigate.

Usage:
    python scripts/qa_application.py "<folder>"
    python scripts/qa_application.py "<folder>" --require-prep      # final pass
    python scripts/qa_application.py "<folder>" --no-images         # skip raster
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from latex_healthcheck import (  # noqa: E402
    PAGE_LIMITS,
    PREP_PDF,
    check_folder,
    pdf_text_and_pages,
)
from workspace_identity import load as load_identity  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_IDENT = load_identity()

# The fingerprint list that rules/07-humanlike-anti-ai.md section F describes in
# prose and that the proofreader and final-verifier slices used to each carry as
# their own grep. One definition, so the two agents cannot drift apart on what
# counts as a hit.
FINGERPRINTS: dict[str, list[str]] = {
    "authorship": [
        r"generated (?:with|by)",
        r"AI[- ]generated",
        r"AI[- ]assisted",
        r"written by AI",
        r"drafted with",
        r"created using",
    ],
    "assistant_voice": [
        r"\bAs an AI\b",
        r"Certainly!",
        r"I hope this helps",
        r"Let me know if",
        r"Here(?:'s| is) (?:the|a) (?:draft|revised)",
    ],
    "meta_commentary": [
        r"This (?:letter|CV|document) (?:highlights|demonstrates|showcases)",
        r"Tailored for:",
        r"^\s*Note:",
        r"Draft v\d",
        r"\[(?:PLACEHOLDER|INSERT|YOUR|COMPANY)\b",
    ],
    "placeholder": [
        r"\{\{.*?\}\}",
        r"\bTODO\b",
        r"\bTBD\b",
        r"\?\?",
    ],
    "scaffolding": [
        r"Key achievements include:",
        r"\bResponsibilities:",
        r"\bIn conclusion,",
        r"\b(?:Furthermore|Moreover|Additionally),",
    ],
    # Interview Prep is exempt from this category only -- it is a private study
    # aid and may name models freely. It is not exempt from the others.
    "vendor": [
        r"\b(?:Claude|ChatGPT|GPT-?\d*|Sonnet|Opus|Haiku|Anthropic|OpenAI|Codex"
        r"|Gemini|Bard|Gemma|DeepMind|Llama|Mistral|Mixtral|Cohere|Grok|xAI"
        r"|Perplexity|Copilot|Cursor|Ollama|LM Studio|LangChain|MCP)\b",
    ],
}

PREP_EXEMPT_CATEGORIES = {"vendor"}

# Only these belong in a finished folder.
DELIVERABLE_SUFFIXES = {".tex", ".pdf", ".html"}


def _rasterise(pdf: Path) -> tuple[list[str], str | None]:
    """Rasterise one PDF via pdf_to_images.py. Returns (image paths, error)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "pdf_to_images.py"), str(pdf)],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"{pdf.name}: rasterise failed: {exc}"
    if proc.returncode != 0:
        return [], f"{pdf.name}: rasterise failed: {proc.stderr.strip()[:200]}"
    outdir = ROOT / "_tmp" / "pdf_pages" / pdf.stem
    if not outdir.is_dir():
        return [], f"{pdf.name}: rasterise produced no output directory"
    return [str(p) for p in sorted(outdir.glob("page-*.png"))], None


def scan_fingerprints(name: str, text: str) -> list[dict[str, str]]:
    """Every fingerprint hit in one document's extracted text."""
    is_prep = name == PREP_PDF
    hits: list[dict[str, str]] = []
    for category, patterns in FINGERPRINTS.items():
        if is_prep and category in PREP_EXEMPT_CATEGORIES:
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                line = text[line_start:line_end if line_end != -1 else len(text)]
                hits.append({
                    "document": name,
                    "category": category,
                    "match": match.group(0).strip(),
                    "context": line.strip()[:160],
                })
    return hits


def inventory(folder: Path) -> dict[str, list[str]]:
    """Split the folder into deliverables and anything that should not be there."""
    deliverables, unexpected = [], []
    for entry in sorted(folder.iterdir()):
        if entry.is_dir():
            unexpected.append(entry.name + "/")
        elif entry.suffix.lower() in DELIVERABLE_SUFFIXES:
            deliverables.append(entry.name)
        else:
            unexpected.append(entry.name)
    html_count = sum(1 for n in deliverables if n.lower().endswith(".html"))
    if html_count != 1:
        unexpected.append(f"<expected exactly one posting .html, found {html_count}>")
    return {"deliverables": deliverables, "unexpected": unexpected}


def run(folder: Path, require_prep: bool, want_images: bool) -> dict:
    if not folder.is_dir():
        return {"folder": str(folder), "verdict": "FAIL",
                "errors": [f"not a folder: {folder}"]}

    health_errors = check_folder(folder, require_prep=require_prep)

    documents: list[dict] = []
    fingerprints: list[dict[str, str]] = []
    raster_errors: list[str] = []

    for pdf in sorted(folder.glob("*.pdf")):
        pages, text, pdf_errors = pdf_text_and_pages(pdf)
        limit = PAGE_LIMITS.get(pdf.name)
        entry: dict = {
            "name": pdf.name,
            "pages": pages,
            "limit": limit,
            "within_limit": bool(limit is None or not pages or pages <= limit),
            "text_extractable": bool(text.strip()),
        }
        if want_images:
            images, error = _rasterise(pdf)
            entry["images"] = images
            if error:
                raster_errors.append(error)
        documents.append(entry)
        health_errors.extend(pdf_errors)
        if text:
            fingerprints.extend(scan_fingerprints(pdf.name, text))

    files = inventory(folder)
    errors = health_errors + raster_errors
    verdict = "PASS" if not errors and not fingerprints and not files["unexpected"] else "FAIL"

    return {
        "folder": str(folder),
        "verdict": verdict,
        "healthcheck_passed": not health_errors,
        "errors": errors,
        "documents": documents,
        "fingerprints": fingerprints,
        "files": files,
        "images_root": str(ROOT / "_tmp" / "pdf_pages") if want_images else None,
    }


def _print_human(report: dict) -> None:
    print(f"QA {report['verdict']}: {report['folder']}")
    for doc in report.get("documents", []):
        flag = "" if doc["within_limit"] else "  <-- OVER LIMIT"
        limit = doc["limit"] if doc["limit"] is not None else "-"
        print(f"  {doc['name']}: {doc['pages']} pages (limit {limit}){flag}")
        for image in doc.get("images", []):
            print(f"      {image}")
    for hit in report.get("fingerprints", []):
        print(f"  FINGERPRINT [{hit['category']}] {hit['document']}: "
              f"{hit['match']!r} -- {hit['context']}")
    for name in report.get("files", {}).get("unexpected", []):
        print(f"  UNEXPECTED FILE: {name}")
    for error in report.get("errors", []):
        print(f"  ERROR: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-pass deterministic QA over an application folder.")
    parser.add_argument("folder")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON only")
    parser.add_argument("--no-images", action="store_true",
                        help="skip PDF rasterisation")
    parser.add_argument("--require-prep", action="store_true",
                        help="require Interview Prep to be present (final pass)")
    args = parser.parse_args()

    report = run(Path(args.folder).resolve(),
                 require_prep=args.require_prep,
                 want_images=not args.no_images)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
