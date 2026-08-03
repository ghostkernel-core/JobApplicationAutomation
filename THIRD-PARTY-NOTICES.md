# Third-party notices

The source code in this repository is MIT licensed (see [LICENSE](./LICENSE)). It also
redistributes a small number of third-party font files, which carry their own licences. Those
licences are reproduced in full alongside the fonts, as their terms require.

## Bundled fonts

Both bundled families are licensed under the **SIL Open Font License, Version 1.1**, which
permits redistribution — including inside this repository and inside the PDFs it produces —
provided the licence travels with the font and the fonts are not sold on their own.

| Font | Version | Copyright | Licence text |
|---|---|---|---|
| Liberation Sans (Regular, Bold, Italic, Bold Italic) | 2.1.5 | Copyright (c) 2012 Red Hat, Inc., with Reserved Font Name "Liberation"; digitized data copyright (c) 2010 Google Corporation, with Reserved Font Name "Arimo", "Tinos" and "Cousine" | [`master/LaTeX/fonts/LICENSE-Liberation.txt`](./master/LaTeX/fonts/LICENSE-Liberation.txt) |
| Noto Sans Symbols (Regular) | 2.003 | Copyright 2022 The Noto Project Authors (https://github.com/notofonts/symbols) | [`master/LaTeX/fonts/LICENSE-NotoSansSymbols.txt`](./master/LaTeX/fonts/LICENSE-NotoSansSymbols.txt) |

Liberation Sans is the automatic fallback body font, used whenever the preferred font is not
installed. Noto Sans Symbols supplies glyphs the body font lacks. A clone therefore compiles
every document with no font installation step and no proprietary dependency.

Note on versions: Liberation Sans **1.x** was licensed under the GPL v2 with a font exception,
not the OFL. This repository deliberately ships **2.1.5**, so every redistributed font here is
OFL, and the repository as a whole carries no copyleft obligation.

## Fonts that are *not* bundled

The documents are designed for **Helvetica Neue**, which is proprietary and is **not included**.
`master/LaTeX/shared/common.tex` and the two letter templates test for it at compile time and
fall back to Liberation Sans when it is absent. If you own a licence for Helvetica Neue you may
place the `.ttf` files in `master/LaTeX/fonts/`; those filenames are git-ignored so they are
never committed. See [docs/customising.md](./docs/customising.md) for how to substitute a
different font.

## Runtime dependencies

These are required to run the system but are **not** redistributed here — you install them
yourself, under their own terms:

- **Claude Code CLI** and the Anthropic API, which the pipeline and the headless builds invoke.
  A commercial service, not an open-source component.
- **A LaTeX distribution** (MiKTeX or TeX Live) providing `xelatex`/`latexmk`.
- **Python packages** from `automation/requirements.txt`, installed into the watcher's own
  virtual environment:

  | Package | Licence |
  |---|---|
  | requests | Apache-2.0 |
  | beautifulsoup4 | MIT |
  | openpyxl | MIT |
  | playwright | Apache-2.0 |
  | python-telegram-bot | LGPL-3.0 |
  | PyMuPDF | AGPL-3.0 **or** commercial |

Two of those are copyleft and worth understanding before you build on this.

**python-telegram-bot (LGPL-3.0)** is used unmodified as a library, which is precisely the case
the LGPL exists to permit. It imposes no conditions on this repository's own code.

**PyMuPDF (AGPL-3.0)** is the one to be deliberate about. It is used in a single function
(`pdf_text_and_pages` in `scripts/latex_healthcheck.py`) to read a compiled PDF back as text and
count its pages, behind an optional import that degrades to a warning when the package is
absent. The AGPL's obligations attach to *distributing* the software or *offering it to users
over a network* — neither applies to running this workspace locally on your own machine, which
is unrestricted private use. This repository does not redistribute PyMuPDF; `pip` installs it
separately, so the MIT licence above remains an accurate statement about the code committed
here. If you intend to host this as a network service, read AGPL section 13 first, or replace
the dependency: `pypdf` (BSD-3-Clause) or a `pdftotext` subprocess covers the same two calls.

Job boards and portals are accessed through their own public endpoints and remain subject to
their respective terms of service. Nothing from them is redistributed in this repository.
