"""Rasterise a compiled PDF to PNGs, so a page can be looked at rather than read.

    python scripts/pdf_to_images.py "<folder>/Name - CV.pdf"
    python scripts/pdf_to_images.py "<...>.pdf" --pages 1 --dpi 200 --outdir _tmp/pages

The healthcheck and proofreading steps read PDFs as *text* with `pypdf`, which
is enough for wording, page counts and forbidden patterns. It is not enough for
the defects that only exist visually: a skill row whose wrapped line sits at a
different spacing from its neighbours, a signature block pushed a few
millimetres onto a third page, a letter that reads as centred because the
ragged-right safeguard did not take. Those need an image.

`pypdf` has no renderer, so this uses **pypdfium2** (BSD-3-Clause / Apache-2.0,
Google's PDFium) and falls back to **pdftoppm**, which ships with both MiKTeX
and Poppler. pdftoppm is GPL, but it is a separate executable invoked across a
process boundary, so nothing propagates into this project.

Deliberately *not* PyMuPDF. It is AGPL-licensed and removing it is the whole
reason this project reads PDFs with pypdf -- see THIRD-PARTY-NOTICES.md. Writing
`import fitz` here, or in an agent step, silently reintroduces the dependency
this repository went out of its way to drop.

Output never lands in a deliverable folder: page images are build artefacts and
step 09 requires the folder to hold only the final .tex/.pdf pair plus one
archived posting. The default output directory is `_tmp/pdf_pages/<pdf stem>/`
at the workspace root.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# scripts/ is this file's own directory, so this resolves however the script
# is invoked. Suppresses the console window each child would otherwise open
# when the parent has none — see scripts/no_console.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import no_console  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTDIR = ROOT / "_tmp" / "pdf_pages"
DEFAULT_DPI = 150


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """Turn "1", "2-4" or "1,3-5" into 1-based page numbers, clamped to the file."""
    if not spec:
        return list(range(1, page_count + 1))
    wanted: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                lo, hi = int(start), int(end)
            except ValueError:
                raise SystemExit(f"bad page range: {part!r}")
            wanted.extend(range(lo, hi + 1))
        else:
            try:
                wanted.append(int(part))
            except ValueError:
                raise SystemExit(f"bad page number: {part!r}")
    pages = sorted({p for p in wanted if 1 <= p <= page_count})
    if not pages:
        raise SystemExit(f"no pages in range: {spec!r} (file has {page_count})")
    return pages


def render_with_pypdfium2(pdf: Path, outdir: Path, spec: str | None, dpi: int) -> list[Path]:
    import pypdfium2  # noqa: PLC0415 - optional, probed by the caller

    document = pypdfium2.PdfDocument(str(pdf))
    try:
        pages = parse_pages(spec, len(document))
        written = []
        for number in pages:
            # PDFium's own unit is 72 dpi, so scale is the ratio to that.
            image = document[number - 1].render(scale=dpi / 72).to_pil()
            target = outdir / f"page-{number:02d}.png"
            image.save(target)
            written.append(target)
        return written
    finally:
        document.close()


def render_with_pdftoppm(pdf: Path, outdir: Path, spec: str | None, dpi: int) -> list[Path]:
    binary = shutil.which("pdftoppm")
    if binary is None:
        return []
    # pdftoppm cannot take a page list, only a first/last window, so a sparse
    # spec is rendered as the span that covers it and filtered afterwards.
    args = [binary, "-png", "-r", str(dpi)]
    requested: list[int] | None = None
    if spec:
        # Page count is unknown without reading the file; use a generous bound
        # and let pdftoppm clamp. Deliverables here are at most six pages.
        requested = parse_pages(spec, 999)
        args += ["-f", str(requested[0]), "-l", str(requested[-1])]
    args += [str(pdf), str(outdir / "page")]

    result = subprocess.run(args, capture_output=True, text=True, **no_console.kwargs())
    if result.returncode != 0:
        raise SystemExit(f"pdftoppm failed: {result.stderr.strip() or result.returncode}")

    # pdftoppm pads the page number to the width of the page count, so a 2-page
    # file gives page-1.png and a 12-page file gives page-01.png. Normalise to
    # the zero-padded form pypdfium2 writes, so callers can rely on one pattern
    # whichever backend ran.
    keep = set(requested) if requested is not None else None
    written = []
    for path in sorted(outdir.glob("page-*.png")):
        try:
            number = int(path.stem.split("-")[-1])
        except ValueError:
            continue
        if keep is not None and number not in keep:
            path.unlink()
            continue
        target = outdir / f"page-{number:02d}.png"
        if path != target:
            path.replace(target)
        written.append(target)
    return sorted(written)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", help="path to the compiled PDF")
    parser.add_argument("--outdir", help="where to write the PNGs "
                                         "(default: _tmp/pdf_pages/<pdf stem>/)")
    parser.add_argument("--pages", help='which pages, e.g. "1", "2-4", "1,3-5" (default: all)')
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                        help=f"render resolution (default: {DEFAULT_DPI})")
    args = parser.parse_args(argv)

    pdf = Path(args.pdf).expanduser()
    if not pdf.is_file():
        print(f"no such PDF: {pdf}", file=sys.stderr)
        return 2

    outdir = Path(args.outdir) if args.outdir else DEFAULT_OUTDIR / pdf.stem
    outdir.mkdir(parents=True, exist_ok=True)
    for stale in outdir.glob("page-*.png"):
        stale.unlink()

    try:
        import pypdfium2  # noqa: F401
        has_pypdfium2 = True
    except ImportError:
        has_pypdfium2 = False

    if has_pypdfium2:
        written = render_with_pypdfium2(pdf, outdir, args.pages, args.dpi)
    else:
        written = render_with_pdftoppm(pdf, outdir, args.pages, args.dpi)
        if not written:
            print("Cannot rasterise: neither pypdfium2 nor pdftoppm is available.\n"
                  "  python -m pip install pypdfium2\n"
                  "or install Poppler / MiKTeX so pdftoppm is on PATH.\n"
                  "Do not use PyMuPDF -- it is AGPL; see THIRD-PARTY-NOTICES.md.",
                  file=sys.stderr)
            return 1

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
