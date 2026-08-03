# German / EU Market Rules

The target market is always Germany / the EU. Every application ships an English set and a German set unless the user explicitly says otherwise.

## German document names

- CV -> Lebenslauf: `<file_prefix> - Lebenslauf.tex` and `.pdf`.
- Cover letter -> Anschreiben: `<file_prefix> - Anschreiben.tex` and `.pdf`.
- `<file_prefix>` comes from `identity.toml` `[person].file_prefix`.
- Interview prep stays private Markdown, not a German application document.

## Lebenslauf conventions

- Tabellarischer Lebenslauf, reverse-chronological, maximum 2 pages.
- Sections in German: `PERSOENLICHE DATEN` or `PERSÖNLICHE DATEN`, `BERUFSERFAHRUNG`, `PROJEKTE`, `AUSBILDUNG`, `TECHNOLOGIE-STACKS`, `SPRACHKENNTNISSE`.
- Personal data labels in German: Adresse, E-Mail, Telefon, Geburtsdatum, Nationalität, LinkedIn.
- Dates use `MM/YYYY`; use `aktuell` for `Present`.
- End with `Ort, Datum: <city>, <date>` (where `<city>` is `identity.toml` `[person].city`) or equivalent template field.
- Keep proper nouns, company names, product names, and tool names unchanged.
- Translate role content idiomatically, not word-for-word.

## Anschreiben conventions

- Formal business-letter style, one page maximum.
- Formal register: `Sie`, never `du`.
- Salutation: `Sehr geehrte Frau <Name>,`, `Sehr geehrter Herr <Name>,`, or `Sehr geehrte Damen und Herren,`.
- Subject line is bold and has no `Betreff:` label, e.g. `Bewerbung als <Rolle>`.
- Closing: `Mit freundlichen Grüßen` + name.
- Enclosure: `Anlagen: Lebenslauf`.
- Tone: sachlich, konkret, glaubwürdig. Prefer evidence over overselling.
- Use proper German umlauts and ß in all German documents and German addresses/names where known. Do not use ASCII transliterations such as `fuer`, `ueber`, `Gruessen`, `Strasse`, `Muenchen`, `Duesseldorf`, `Nationalitaet`, or `PERSOENLICHE` unless quoting an exact ASCII-only source string.
- English documents use the opposite convention: use established English names where available, otherwise transliterate German names with `ae/oe/ue/ss` and avoid `ä/ö/ü/ß`.

## Language level to state

- German: B1+.
- English: C1+.
- Native language: Muttersprache / native.
- Do not claim B2, C1, native, or negotiation-level German unless `rules/00-canonical-profile.md` is updated.
- Prefer simple wording such as `Deutsch: B1+`, not `verhandlungssicher`.

## EU / GDPR niceties

- No salary expectation unless the posting explicitly requires it.
- Keep `(m/w/d)` in German role titles when quoting the employer's title.
- Add availability only if the posting asks for it.
- Do not add private details beyond the template and canonical profile.

## Posting language

- German posting -> German set is primary; still produce English set.
- English posting -> English set is primary; still produce German set.
