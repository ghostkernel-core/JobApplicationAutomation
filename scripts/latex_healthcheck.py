"""Deterministic health checks for LaTeX-first application folders."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from pypdf import PdfReader  # type: ignore
except Exception:  # noqa: BLE001
    PdfReader = None


sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_identity import load as load_identity  # noqa: E402

_IDENT = load_identity()

# Labels and page caps are the same for everyone; only the name in front of them
# comes from identity.toml.
TEX_FILES = [_IDENT.doc_name(label) for label in
             ("CV", "Lebenslauf", "Cover Letter", "Anschreiben")]
PDF_FILES = [name[:-4] + ".pdf" for name in TEX_FILES]
PAGE_LIMITS = {
    _IDENT.doc_name(label, ".pdf"): limit for label, limit in (
        ("CV", 2),
        ("Lebenslauf", 2),
        ("Cover Letter", 1),
        ("Anschreiben", 1),
        ("Interview Prep", 6),
    )
}
PREP_TEX = _IDENT.doc_name("Interview Prep")
PREP_PDF = _IDENT.doc_name("Interview Prep", ".pdf")
ROOT = Path(__file__).resolve().parents[1]
MASTER_COMMON = ROOT / "master" / "LaTeX" / "shared" / "common.tex"

# Generic patterns only: these apply to anybody using this system, regardless
# of who owns the workspace, so they are safe to keep hard-coded in a public
# repo. Person-specific stale strings (superseded dates, claims that person
# cannot make, tools they may not claim, former addresses/affiliations) do NOT
# belong here — they are loaded at runtime from the git-ignored
# rules/stale_patterns.txt via load_stale_patterns() below.
BAD_PATTERNS = [
    r"\{\{.*?\}\}",
    r"\bTODO\b",
    r"\bTBD\b",
    r"\?\?",
    r"visa|permit|sponsorship|relocation",
    r"PERSOENLICHE|Nationalitaet|\bfuer\b|\bueber\b|Gruessen|Strasse",
    r"\b(?:Claude|ChatGPT|Anthropic|OpenAI|Ollama|Gemini|Mistral|Copilot|LangChain|MCP)\b",
]

STALE_PATTERNS_FILE = ROOT / "rules" / "stale_patterns.txt"


def load_stale_patterns(path: Path = STALE_PATTERNS_FILE) -> list[str]:
    """Load per-person stale-pattern regexes from a git-ignored file.

    The file is optional: if it does not exist, this returns an empty list
    and the check runs with only the generic BAD_PATTERNS. Each non-blank,
    non-comment line is compiled defensively so a typo produces a clear error
    naming the file and line instead of a raw traceback.
    """
    if not path.exists():
        return []
    patterns: list[str] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            re.compile(line)
        except re.error as exc:
            raise SystemExit(
                f"invalid regex in {path} at line {lineno}: {line!r} ({exc})"
            ) from exc
        patterns.append(line)
    return patterns


BAD_PATTERNS.extend(load_stale_patterns())


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def pdf_text_and_pages(path: Path) -> tuple[int, str, list[str]]:
    if PdfReader is None:
        return 0, "", ["pypdf is not available; cannot inspect PDFs"]
    errors: list[str] = []
    try:
        reader = PdfReader(str(path))
        # extract_text() returns None for a page with no text layer, not "".
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return len(reader.pages), text, errors
    except Exception as exc:  # noqa: BLE001
        return 0, "", [f"{path.name}: cannot read PDF: {exc}"]


def check_bad_patterns(name: str, text: str) -> list[str]:
    errors: list[str] = []
    for pattern in BAD_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            errors.append(f"{name}: forbidden/stale pattern found: {pattern}")
    return errors


def check_folder(folder: Path, require_prep: bool = True) -> list[str]:
    """Validate a deliverable folder.

    `require_prep` is False while the application documents are being checked
    ahead of the Interview Prep render. Prep is a private study aid that lands
    after the CV and letter, so demanding it here would fail a folder whose
    application is finished and correct. When prep is absent and not required
    its checks are skipped; when it is present it is checked either way.
    """
    errors: list[str] = []
    if not folder.is_dir():
        return [f"not a folder: {folder}"]

    has_german = any((folder / name).exists() for name in (
        _IDENT.doc_name("Lebenslauf"),
        _IDENT.doc_name("Anschreiben"),
        _IDENT.doc_name("Lebenslauf", ".pdf"),
        _IDENT.doc_name("Anschreiben", ".pdf"),
    ))
    expected_tex_files = TEX_FILES if has_german else [
        _IDENT.doc_name("CV"),
        _IDENT.doc_name("Cover Letter"),
    ]
    expected_pdf_files = PDF_FILES if has_german else [
        _IDENT.doc_name("CV", ".pdf"),
        _IDENT.doc_name("Cover Letter", ".pdf"),
    ]

    for name in expected_tex_files:
        path = folder / name
        if not path.exists():
            errors.append(f"missing TEX: {name}")
            continue
        text = read_text(path)
        errors.extend(check_bad_patterns(name, text))
        if r"\documentclass" not in text or r"\end{document}" not in text:
            errors.append(f"{name}: does not look like a complete LaTeX document")
        if name in {_IDENT.doc_name("Cover Letter"), _IDENT.doc_name("Anschreiben")}:
            if r"\usepackage{ragged2e}" not in text or r"\RaggedRight" not in text:
                errors.append(f"{name}: missing explicit ragged-right letter alignment safeguard")
            if r"\setlength{\parskip}{10pt}" not in text:
                errors.append(f"{name}: missing letter paragraph-spacing safeguard")

    prep_tex_path = folder / PREP_TEX
    if not prep_tex_path.exists():
        if require_prep:
            errors.append(f"missing TEX: {PREP_TEX}")
    else:
        prep_text = read_text(prep_tex_path)
        if r"\documentclass" not in prep_text or r"\end{document}" not in prep_text:
            errors.append(f"{PREP_TEX}: does not look like a complete LaTeX document")
        if re.search(r"\{\{.*?\}\}", prep_text):
            errors.append(f"{PREP_TEX}: unresolved template placeholder")

    if MASTER_COMMON.exists():
        common = read_text(MASTER_COMMON)
        if r"\usepackage{ragged2e}" not in common or r"\RaggedRight" not in common or r"\spaceskip" not in common:
            errors.append("master shared common.tex: missing ragged-right skill-row spacing safeguard")
    else:
        errors.append(f"missing master shared LaTeX file: {MASTER_COMMON}")

    for name in expected_pdf_files:
        path = folder / name
        if not path.exists():
            errors.append(f"missing PDF: {name}")
            continue
        pages, text, pdf_errors = pdf_text_and_pages(path)
        errors.extend(pdf_errors)
        limit = PAGE_LIMITS[name]
        if pages and pages > limit:
            errors.append(f"{name}: {pages} pages exceeds limit {limit}")
        if text:
            errors.extend(check_bad_patterns(name, text))
            if not any(part in text for part in _IDENT.full_name.split() if len(part) > 2):
                errors.append(f"{name}: PDF text extraction missing candidate name")

    prep_pdf_path = folder / PREP_PDF
    if not prep_pdf_path.exists():
        if require_prep:
            errors.append(f"missing PDF: {PREP_PDF}")
    else:
        pages, text, pdf_errors = pdf_text_and_pages(prep_pdf_path)
        errors.extend(pdf_errors)
        limit = PAGE_LIMITS.get(PREP_PDF)
        if pages and limit and pages > limit:
            errors.append(f"{PREP_PDF}: {pages} pages exceeds limit {limit}")
        if text and not any(part in text for part in _IDENT.full_name.split() if len(part) > 2):
            errors.append(f"{PREP_PDF}: PDF text extraction missing candidate name")

    html_files = list(folder.glob("*.html"))
    if len(html_files) != 1:
        errors.append(f"expected exactly one posting .html, found {len(html_files)}")

    for build_artifact in ("*.aux", "*.log", "*.out", "*.fls", "*.fdb_latexmk", "*.synctex.gz"):
        matches = list(folder.glob(build_artifact))
        if matches:
            errors.append(f"compiler artifacts found in deliverable folder: {', '.join(p.name for p in matches)}")

    return errors


def check_cv_folder(folder: Path, expect_phd: bool) -> list[str]:
    errors: list[str] = []
    if not folder.is_dir():
        return [f"not a folder: {folder}"]

    expected_tex_files = [
        _IDENT.doc_name("CV"),
        _IDENT.doc_name("Lebenslauf"),
    ]
    expected_pdf_files = [name[:-4] + ".pdf" for name in expected_tex_files]
    rendered_text: dict[str, str] = {}

    for name in expected_tex_files:
        path = folder / name
        if not path.exists():
            errors.append(f"missing TEX: {name}")
            continue
        text = read_text(path)
        rendered_text[name] = text
        errors.extend(check_bad_patterns(name, text))
        if r"\documentclass" not in text or r"\end{document}" not in text:
            errors.append(f"{name}: does not look like a complete LaTeX document")

    for name in expected_pdf_files:
        path = folder / name
        if not path.exists():
            errors.append(f"missing PDF: {name}")
            continue
        pages, text, pdf_errors = pdf_text_and_pages(path)
        errors.extend(pdf_errors)
        if pages and pages > PAGE_LIMITS[name]:
            errors.append(f"{name}: {pages} pages exceeds limit {PAGE_LIMITS[name]}")
        if text:
            errors.extend(check_bad_patterns(name, text))
            if not any(part in text for part in _IDENT.full_name.split() if len(part) > 2):
                errors.append(f"{name}: PDF text extraction missing candidate name")
            rendered_text[name] = text

    combined = "\n".join(rendered_text.values())
    # Deliberately matches the doctoral-programme wording rather than a bare "Dr.",
    # which would fire on a recipient's title in the cover-letter address block.
    phd_pattern = r"Doctor of Engineering|Doctor of Philosophy|Promotion zum Dr\.-Ing\.|Promotionskolleg"
    if expect_phd:
        if not re.search(phd_pattern, combined, flags=re.IGNORECASE):
            errors.append("PhD variant: missing ongoing doctorate entry")
        if not re.search(r"ongoing|in progress|laufend", combined, flags=re.IGNORECASE):
            errors.append("PhD variant: doctorate is not clearly marked ongoing")
        # The doctorate is ongoing, so it may appear as an education entry but never
        # as a title on the name line -- either directly after "Name:" or after a
        # given name on the same line.
        given = "|".join(re.escape(p) for p in _IDENT.full_name.split() if len(p) > 1)
        title_in_name = r"Name:\s*(?:Dr\.|Dr\.-Ing\.)"
        if given:
            title_in_name += rf"|Name:\s*(?:{given})\s+(?:Dr\.|Dr\.-Ing\.)"
        if re.search(title_in_name, combined, flags=re.IGNORECASE):
            errors.append("PhD variant: doctorate title appears in the candidate name")
    elif re.search(phd_pattern, combined, flags=re.IGNORECASE):
        errors.append("Non-PhD variant: doctorate wording found")

    for build_artifact in ("*.aux", "*.log", "*.out", "*.fls", "*.fdb_latexmk", "*.synctex.gz"):
        matches = list(folder.glob(build_artifact))
        if matches:
            errors.append(f"compiler artifacts found in deliverable folder: {', '.join(p.name for p in matches)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--cv-only", action="store_true", help="Validate a bilingual recruiter CV folder.")
    parser.add_argument("--expect-phd", action="store_true", help="Require an ongoing doctorate entry in CV-only mode.")
    parser.add_argument("--no-prep", action="store_true",
                        help="Do not require Interview Prep; check the application documents alone.")
    args = parser.parse_args()
    errors = (check_cv_folder(Path(args.folder), args.expect_phd) if args.cv_only
              else check_folder(Path(args.folder), require_prep=not args.no_prep))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("LaTeX healthcheck PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
