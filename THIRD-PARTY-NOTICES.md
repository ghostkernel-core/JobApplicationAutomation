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
- **A LaTeX distribution** (MiKTeX or TeX Live) providing `xelatex`/`latexmk`.
- **Python packages** listed in `automation/requirements.txt`, installed into the watcher's own
  virtual environment, plus `openpyxl` for the tracker workbook.
- **Playwright** and its Chromium download, an optional dependency used only by the
  browser-based portal fetchers.

Job boards and portals are accessed through their own public endpoints and remain subject to
their respective terms of service. Nothing from them is redistributed in this repository.
