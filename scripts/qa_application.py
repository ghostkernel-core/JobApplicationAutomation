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

Two halves of the report carry different weight. `errors`, `fingerprints` and
`files.unexpected` decide the verdict. `style` and `ats` never do: they are
measurements handed to the reader, and a wrong call in either must not be able
to fail a finished application.

Usage:
    python scripts/qa_application.py "<folder>"
    python scripts/qa_application.py "<folder>" --require-prep      # final pass
    python scripts/qa_application.py "<folder>" --no-images         # skip raster
    python scripts/qa_application.py "<folder>" --posting-text jd.txt
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ats_report  # noqa: E402
from clean_deliverable import payload_dir  # noqa: E402
from latex_healthcheck import (  # noqa: E402
    PAGE_LIMITS,
    PREP_PDF,
    PREP_TEX,
    VENDOR_PATTERN,
    VENDOR_PATTERN_LOOSE,
    check_folder,
    pdf_metadata,
    pdf_text_and_pages,
    read_text,
)

ROOT = Path(__file__).resolve().parents[1]

# The fingerprint list that rules/07-humanlike-anti-ai.md section F describes in
# prose and that the proofreader and final-verifier slices used to each carry as
# their own grep. One definition, so the two agents cannot drift apart on what
# counts as a hit.
#
# Section F bans a trace of *who wrote this document*. Two of these patterns
# used to match the topic instead, and on the whole archive they were wrong
# every time they fired: six hits across 43 applications -- "AI-assisted
# maintenance", "AI-assisted SDLC", "brands itself as an AI Center of
# Excellence" -- and not one of them a claim about authorship. Both now require
# the shape that makes the phrase self-referential. An over-broad rule that
# fails a correct document is not a stricter rule; it is the one that gets
# worked around.
FINGERPRINTS: dict[str, list[str]] = {
    "authorship": [
        r"generated (?:with|by)",
        r"AI[- ]generated",
        # "AI-assisted CV" is a confession; "AI-assisted maintenance" is a job.
        r"AI[- ]assisted\s+(?:CV|r[eé]sum[eé]|letter|application|document|"
        r"writing|draft|content|text)\b",
        # …and the predicative form of the same confession, "this was
        # AI-assisted", which puts the noun on the other side.
        r"\b(?:was|is|were|are)\s+AI[- ]assisted\b",
        r"\bAI assistance\b",
        r"written by AI",
        r"drafted with",
        r"created using",
    ],
    "assistant_voice": [
        # The comma is what separates the refusal boilerplate ("As an AI, I
        # cannot") from a company that brands itself as an AI Center of
        # Excellence.
        r"\bAs an AI\b\s*(?:,|language model|assistant|model\b)",
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
    # The pattern itself lives in latex_healthcheck so the .tex check and this
    # one cannot drift; they used to be a 13-name list and a 24-name list.
    "vendor": [VENDOR_PATTERN],
}

PREP_EXEMPT_CATEGORIES = {"vendor"}

# What a .tex file adds that its PDF does not: comments. Body prose is already
# scanned in the rendered PDF, and the whole .tex is already scanned for vendor
# names by latex_healthcheck.check_bad_patterns, so scanning the source again
# for either would only double-report. A `% generated by ...` line is the one
# thing that never reaches the PDF and that nothing has ever looked for.
#
# Scoping this to comments is also what keeps it honest on Interview Prep: that
# document legitimately discusses AI-assisted development as interview content
# ("no team-level rollout of an AI-assisted SDLC" is a do-not-claim note), and
# scanning its prose for authorship phrases turns real content into a failure.
TEX_SCAN_CATEGORIES = ("authorship", "assistant_voice", "meta_commentary")

# A LaTeX comment: an unescaped % to end of line.
_TEX_COMMENT = re.compile(r"(?<!\\)%(.*)$", re.MULTILINE)

# Transcribed from rules/07-humanlike-anti-ai.md section A (:7-18) and F4
# (:86-90), the same way FINGERPRINTS was transcribed from section F. None of
# these had a regex anywhere in the repo, so "robust" sat in a delivered CV
# unflagged. Keep this and rule 07 in step.
#
# Unlike FINGERPRINTS these never change the verdict. Style is a judgement call
# -- a gate that fails a finished application over one word gets worked around
# -- and rule 07 already routes section A to the human-reading proofreader. This
# hands that reader a list instead of an instruction to go looking.
STYLE_TELLS: dict[str, list[str]] = {
    "cliche": [
        r"\bleverag(?:e|es|ed|ing)\b",
        r"\bdelv(?:e|es|ed|ing)\b",
        r"\brobust\b",
        r"\b(?:thrilled|delighted|excited) to\b",
        r"\bkeen interest\b",
        r"\bpassionate about\b",
        r"\bsynerg(?:y|ies|istic)\b",
        r"\bideal candidate\b",
        r"\bunique blend\b",
        r"\bproven track record\b",
        r"\btestament to\b",
        r"\btapestry\b",
        r"\bspearhead(?:s|ed|ing)?\b",
        r"\bmeticulous(?:ly)?\b",
        r"\bever[- ]evolving\b",
        r"\bfast[- ]paced\b",
    ],
    "construction": [
        r"\bnot only\b.{1,80}?\bbut also\b",
        r"\bIn today'?s\b.{0,40}?\bworld\b",
        r"\bIn the ever[- ]evolving landscape\b",
    ],
}

# Rule 07 A calls out em-dash overuse rather than any em-dash, so this is a rate,
# not a ban. The two documents measured when this was written used none at all;
# one per thousand characters is roughly one per short paragraph, which is where
# it stops reading like this candidate.
EM_DASH_PER_1K_LIMIT = 1.0

# Rule 07 F4 names "Furthermore,/Moreover,/Additionally," opening three
# paragraphs in a row. FINGERPRINTS["scaffolding"] matches them singly, so one
# "Moreover" -- ordinary in real prose -- currently reads the same as three in a
# row. This is the run that the rule actually forbids.
_CADENCE_OPENER = re.compile(r"^\s*(?:Furthermore|Moreover|Additionally)\s*,", re.MULTILINE)
CADENCE_RUN_LIMIT = 3

# Rule 07 F4: "every bullet starting with the same verb".
SAME_VERB_RUN_LIMIT = 3

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


def _context(text: str, match: re.Match) -> str:
    """The whole line a match sits on, trimmed for a report."""
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    return text[line_start:line_end if line_end != -1 else len(text)].strip()[:160]


def scan_fingerprints(
    name: str,
    text: str,
    *,
    categories: tuple[str, ...] | None = None,
    from_pdf: bool = False,
    field: str = "",
) -> list[dict[str, str]]:
    """Every fingerprint hit in one document's text.

    `categories` restricts the scan (a .tex file is not scanned for everything a
    PDF is). `from_pdf` swaps in the vendor pattern that tolerates pypdf's
    kerning splits. `field` labels a hit that came from somewhere other than the
    body -- PDF metadata, say -- so a reader can tell where to go and fix it.
    """
    is_prep = name in (PREP_PDF, PREP_TEX)
    hits: list[dict[str, str]] = []
    for category, patterns in FINGERPRINTS.items():
        if categories is not None and category not in categories:
            continue
        if is_prep and category in PREP_EXEMPT_CATEGORIES:
            continue
        for pattern in patterns:
            active = VENDOR_PATTERN_LOOSE if (from_pdf and pattern == VENDOR_PATTERN) else pattern
            for match in re.finditer(active, text, flags=re.IGNORECASE | re.MULTILINE):
                hit = {
                    "document": name,
                    "category": category,
                    "match": match.group(0).strip(),
                    "context": _context(text, match),
                }
                if field:
                    hit["field"] = field
                hits.append(hit)
    return hits


def scan_style(name: str, text: str) -> list[dict[str, str]]:
    """Every rule 07 section A / F4 style tell in one document's text.

    Reported, never gated -- see the note on STYLE_TELLS.

    Interview Prep is skipped outright. Section A is about the document reading
    as the candidate wrote it, and prep is a private study aid nobody outside
    this workspace ever sees. Measured across every August folder, including it
    produced five findings on prep -- all of them em-dashes in a bullet list --
    against one on an application document. That ratio is how a report gets
    ignored.
    """
    if name in (PREP_PDF, PREP_TEX):
        return []
    hits: list[dict[str, str]] = []
    for category, patterns in STYLE_TELLS.items():
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
                hits.append({
                    "document": name,
                    "category": category,
                    "match": match.group(0).strip(),
                    "context": _context(text, match),
                })

    # Cadence is about repetition, so it is a count over the document rather
    # than one regex hit. Adjacency is deliberately not tested: pypdf renders a
    # paragraph break as a blank line, a single newline, or nothing depending on
    # the environment, so "three paragraphs in a row" is not recoverable from
    # extracted text. Three of these in one short document is the tell either way.
    openers = list(_CADENCE_OPENER.finditer(text))
    if len(openers) >= CADENCE_RUN_LIMIT:
        hits.append({
            "document": name,
            "category": "cadence",
            "match": f"{len(openers)}x paragraph opening with "
                     f"Furthermore/Moreover/Additionally",
            "context": " | ".join(_context(text, m)[:60] for m in openers[:3]),
        })
    return hits


def style_metrics(name: str, text: str) -> dict | None:
    """The style signal that is a rate rather than a phrase.

    None for Interview Prep, for the reason given on scan_style.
    """
    if name in (PREP_PDF, PREP_TEX):
        return None
    chars = len(text)
    em_dashes = text.count("\u2014")
    per_1k = round(em_dashes * 1000 / chars, 2) if chars else 0.0
    return {
        "document": name,
        "chars": chars,
        "em_dashes": em_dashes,
        "em_dash_per_1k": per_1k,
        "em_dash_overuse": per_1k > EM_DASH_PER_1K_LIMIT,
    }


def same_verb_runs(name: str, tex: str) -> list[dict]:
    """Runs of consecutive \\item bullets opening on the same word.

    Read from the .tex rather than the PDF: extracted PDF text renders a bullet
    as a glyph, a dash, or nothing at all depending on the list environment,
    while `\\item` is unambiguous. Interview Prep is skipped, as in scan_style.
    """
    if name in (PREP_PDF, PREP_TEX):
        return []
    items = [m.group(1) for m in re.finditer(r"\\item\b\s*(.{0,60})", tex)]
    runs: list[dict] = []
    current = ""
    count = 0
    for raw in items + [""]:
        cleaned = re.sub(r"[^A-Za-z]+", " ", re.sub(r"\\[A-Za-z]+", " ", raw)).strip()
        first = cleaned.split(" ")[0].lower() if cleaned else ""
        if first and first == current:
            count += 1
            continue
        if count >= SAME_VERB_RUN_LIMIT:
            runs.append({"document": name, "word": current, "count": count})
        current, count = first, 1 if first else 0
    return runs


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


#: Where the two report-only halves are left for anything downstream that wants
#: them -- today, the Telegram message announcing the finished build.
QA_SUMMARY_NAME = "qa_summary.json"


def _persist_summary(folder: Path, report: dict) -> str | None:
    """Write `style` and `ats` beside the archived payloads, not into the folder.

    `inventory()` calls every suffix outside DELIVERABLE_SUFFIXES unexpected, and
    `files.unexpected` decides the verdict -- so a JSON file dropped into the
    deliverable folder would fail the very run that produced it, in the window
    between 06A writing it and step 09 moving it out. `_tmp/payloads/<Company>
    <date> <Role>/` is where step 09 would have relocated it anyway, so writing
    it there in the first place gives one path that resolves the same before and
    after cleanup -- which is what lets the notifier look in a single place for
    a message it sends both before cleanup and after it.

    Only the two measurements go in. The verdict deliberately does not: this
    file exists to be read by something reporting on a build, and a reader that
    can see a verdict here will eventually act on one that was computed before
    the run finished.

    Returns None when the folder is not a `<YYYY>/<Company>/<date> - <Role>`
    deliverable (a scratch directory, a test tmpdir) or the write fails.
    """
    target = payload_dir(folder)
    if target is None:
        return None
    payload = {"folder": folder.name,
               "ats": report.get("ats"),
               "style": report.get("style")}
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / QA_SUMMARY_NAME
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        return None
    return str(path)


def ats_scan(folder: Path, posting_text: Path | None = None) -> dict:
    """Keyword coverage of the rendered CV against the brief and the posting.

    The blanket except is deliberate. This is a measurement bolted onto a gate:
    an unusual Match Brief or a malformed archive must cost the run its ATS
    number and nothing else. Nothing here can move the verdict.
    """
    try:
        return ats_report.report(folder, posting_text)
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        return {"error": f"{type(exc).__name__}: {exc}",
                "note": ats_report.DISCLAIMER}


def run(folder: Path, require_prep: bool, want_images: bool,
        posting_text: Path | None = None) -> dict:
    if not folder.is_dir():
        return {"folder": str(folder), "verdict": "FAIL",
                "errors": [f"not a folder: {folder}"]}

    health_errors = check_folder(folder, require_prep=require_prep)

    documents: list[dict] = []
    fingerprints: list[dict[str, str]] = []
    raster_errors: list[str] = []
    style_hits: list[dict[str, str]] = []
    style_stats: list[dict] = []
    bullet_runs: list[dict] = []

    for pdf in sorted(folder.glob("*.pdf")):
        pages, text, pdf_errors = pdf_text_and_pages(pdf)
        limit = PAGE_LIMITS.get(pdf.name)
        metadata = pdf_metadata(pdf)
        entry: dict = {
            "name": pdf.name,
            "pages": pages,
            "limit": limit,
            "within_limit": bool(limit is None or not pages or pages <= limit),
            "text_extractable": bool(text.strip()),
            "metadata": metadata,
        }
        if want_images:
            images, error = _rasterise(pdf)
            entry["images"] = images
            if error:
                raster_errors.append(error)
        documents.append(entry)
        health_errors.extend(pdf_errors)
        if text:
            fingerprints.extend(scan_fingerprints(pdf.name, text, from_pdf=True))
            style_hits.extend(scan_style(pdf.name, text))
            stat = style_metrics(pdf.name, text)
            if stat is not None:
                style_stats.append(stat)
        if metadata:
            # Rule 07 F1 bans an authorship trace in PDF metadata as firmly as in
            # body text, and rules/slices/10-final-verifier.md already tells that
            # agent to check it -- without ever putting the field in front of it.
            fingerprints.extend(scan_fingerprints(
                pdf.name,
                "\n".join(f"{key}: {value}" for key, value in metadata.items()),
                categories=("authorship", "assistant_voice", "vendor"),
                field="metadata",
            ))

    # The .tex pass. A LaTeX comment never reaches the PDF, so this is the only
    # place a "% generated by ..." line can be caught -- and the Interview Prep
    # .tex is skipped by the healthcheck entirely, which leaves this as its only
    # authorship check. F5 exempts prep from vendor names, not from F1.
    for tex in sorted(folder.glob("*.tex")):
        source = read_text(tex)
        comments = "\n".join(m.group(1) for m in _TEX_COMMENT.finditer(source))
        if comments.strip():
            fingerprints.extend(scan_fingerprints(
                tex.name, comments,
                categories=TEX_SCAN_CATEGORIES, field="latex comment"))
        bullet_runs.extend(same_verb_runs(tex.name, source))

    files = inventory(folder)
    ats = ats_scan(folder, posting_text)
    errors = health_errors + raster_errors
    verdict = "PASS" if not errors and not fingerprints and not files["unexpected"] else "FAIL"

    report = {
        "folder": str(folder),
        "verdict": verdict,
        "healthcheck_passed": not health_errors,
        "errors": errors,
        "documents": documents,
        "fingerprints": fingerprints,
        # Reported for the proofreader to weigh; deliberately absent from the
        # verdict above.
        "style": {
            "hits": style_hits,
            "metrics": style_stats,
            "bullet_runs": bullet_runs,
        },
        # Keyword coverage of the CV. A proxy, not a recruiter's number, and --
        # like `style` -- absent from the verdict on purpose.
        "ats": ats,
        "files": files,
        "images_root": str(ROOT / "_tmp" / "pdf_pages") if want_images else None,
    }
    report["summary_path"] = _persist_summary(folder, report)
    return report


def _print_ats(ats: dict) -> None:
    """The ATS block, printed as measurements rather than as findings.

    Every line here is prefixed `ATS` and none of them says ERROR or FAIL, so a
    reader skimming for defects does not mistake a missing keyword for one.
    """
    if ats.get("error"):
        print(f"  ATS: not measured ({ats['error']})")
        return
    skipped = ats.get("skipped") or {}
    for label, key in (("brief", "brief"), ("posting", "posting")):
        section = ats.get(key)
        if not section:
            reason = skipped.get(key) or skipped.get("cv")
            if reason:
                print(f"  ATS {label}: n/a ({reason})")
            continue
        coverage = section.get("coverage")
        pct = "n/a" if coverage is None else f"{round(coverage * 100)}%"
        print(f"  ATS {label}: {pct} "
              f"({len(section['matched'])}/{section['total']}) -- {section['source']}")
        if section.get("missing"):
            print(f"      missing: {', '.join(section['missing'])}")
        if section.get("optional_missing"):
            print(f"      missing (nice-to-have): "
                  f"{', '.join(section['optional_missing'])}")
    if ats.get("parse_warnings"):
        print(f"  ATS parse warnings (in the CV, split by the text extractor): "
              f"{', '.join(ats['parse_warnings'])}")


def _print_human(report: dict) -> None:
    print(f"QA {report['verdict']}: {report['folder']}")
    for doc in report.get("documents", []):
        flag = "" if doc["within_limit"] else "  <-- OVER LIMIT"
        limit = doc["limit"] if doc["limit"] is not None else "-"
        print(f"  {doc['name']}: {doc['pages']} pages (limit {limit}){flag}")
        for image in doc.get("images", []):
            print(f"      {image}")
    for hit in report.get("fingerprints", []):
        where = f" ({hit['field']})" if hit.get("field") else ""
        print(f"  FINGERPRINT [{hit['category']}] {hit['document']}{where}: "
              f"{hit['match']!r} -- {hit['context']}")
    style = report.get("style", {})
    for hit in style.get("hits", []):
        print(f"  STYLE [{hit['category']}] {hit['document']}: "
              f"{hit['match']!r} -- {hit['context']}")
    for stat in style.get("metrics", []):
        if stat.get("em_dash_overuse"):
            print(f"  STYLE [em-dash] {stat['document']}: {stat['em_dashes']} "
                  f"({stat['em_dash_per_1k']} per 1k chars)")
    for run in style.get("bullet_runs", []):
        print(f"  STYLE [same-verb] {run['document']}: {run['count']} bullets "
              f"in a row opening {run['word']!r}")
    _print_ats(report.get("ats") or {})
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
    parser.add_argument("--posting-text", type=Path, default=None,
                        help="file holding the job description as plain text, "
                             "for the ATS coverage number. Preferred over "
                             "re-parsing the archived .html.")
    args = parser.parse_args()

    report = run(Path(args.folder).resolve(),
                 require_prep=args.require_prep,
                 want_images=not args.no_images,
                 posting_text=args.posting_text)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
