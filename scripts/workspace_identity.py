"""Who this workspace belongs to â€” loaded from identity.toml at the repo root.

The whole point is that a second workspace is the same code with a different
identity.toml, rather than a hand-edited fork. So this module refuses to guess:
a missing or incomplete file raises and names what is wrong. Falling back to a
default person would silently put someone else's name and phone number on a real
application.

    from workspace_identity import load
    ident = load()
    ident.file_prefix                  # "Surname, Firstname"
    ident.doc_name("CV", ".pdf")       # "Surname, Firstname - CV.pdf"
    ident.context("de")                # {"identity_name": â€¦, "identity_address": â€¦}

Self-check:  python scripts/workspace_identity.py
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: `JOBAPP_IDENTITY` points this at a file outside the checkout. It exists for
#: the one caller that has no identity.toml and cannot be given one: CI, where
#: the file is untracked by design and the structure job actively fails if it
#: ever appears. Several modules build their document-name constants at import
#: time, so an absent identity there is not a skipped test — it is a collection
#: error that takes the whole suite down.
#:
#: This is deliberately not a fallback. Nothing is guessed and nothing is
#: relaxed: the override still goes through the same validation, so a stub full
#: of FILL IN fails exactly as it would at the default path.
def default_path(environ: dict | None = None) -> Path:
    """Where `load()` looks when it is not given a path.

    Takes the environment as an argument so the rule can be tested without
    reloading this module — a reload would hand out a second `load` with its own
    cache while every importer still holds the first.
    """
    env = os.environ if environ is None else environ
    return Path(env.get("JOBAPP_IDENTITY") or ROOT / "identity.toml")


IDENTITY_PATH = default_path()

# Every key that must be present before a document can be rendered.
REQUIRED_PERSON = ("full_name", "file_prefix", "email", "phone",
                   "date_of_birth", "linkedin", "street", "city")
REQUIRED_LOCALE = ("city_line", "nationality")
LOCALES = ("en", "de")


@dataclass(frozen=True)
class Identity:
    full_name: str
    file_prefix: str
    email: str
    phone: str
    date_of_birth: str
    linkedin: str
    street: str
    city: str
    # locale -> {"city_line": â€¦, "nationality": â€¦}
    locales: dict

    def doc_name(self, label: str, suffix: str = ".tex") -> str:
        """"CV" -> "Surname, Firstname - CV.tex"."""
        return f"{self.file_prefix} - {label}{suffix}"

    def address(self, locale: str) -> str:
        return f"{self.street}, {self.locales[locale]['city_line']}"

    def context(self, locale: str) -> dict[str, str]:
        """Template placeholders. Locale picks the country/nationality wording.

        Values are deliberately not LaTeX-escaped: they are a fixed, reviewed
        contact block, not payload text from a model, and escaping would break
        anyone whose name legitimately needs a LaTeX accent command.
        """
        if locale not in self.locales:
            raise KeyError(f"identity.toml has no [{locale}] section")
        return {
            "identity_name": self.full_name,
            "identity_email": self.email,
            "identity_phone": self.phone,
            "identity_dob": self.date_of_birth,
            "identity_linkedin": self.linkedin,
            "identity_street": self.street,
            "identity_city": self.city,
            "identity_city_line": self.locales[locale]["city_line"],
            "identity_nationality": self.locales[locale]["nationality"],
            "identity_address": self.address(locale),
        }


@lru_cache(maxsize=None)
def load(path: Path | None = None) -> Identity:
    target = Path(path) if path else IDENTITY_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"{target} is missing. Every workspace needs one; copy it from "
            "another install and replace the values with this person's own.")

    # utf-8-sig: Windows editors (Notepad, older PowerShell) often prepend a
    # BOM, which plain utf-8 hands to the TOML parser as a syntax error on
    # line 1 — an unhelpful failure for exactly the file users edit by hand.
    data = tomllib.loads(target.read_text(encoding="utf-8-sig"))

    def _blank(value: object) -> bool:
        # A freshly cloned workspace ships a stub full of FILL IN. Treating that
        # as a real value would print it onto a CV, and no later check looks for
        # it â€” the healthcheck's placeholder pattern only catches {{...}}.
        text = str(value).strip()
        return not text or text.upper().startswith("FILL IN")

    person = data.get("person") or {}
    missing = [key for key in REQUIRED_PERSON if _blank(person.get(key, ""))]
    if missing:
        raise ValueError(
            f"{target}: [person] is missing or unfilled: {', '.join(missing)}")

    locales: dict[str, dict[str, str]] = {}
    for locale in LOCALES:
        section = data.get(locale) or {}
        absent = [key for key in REQUIRED_LOCALE if _blank(section.get(key, ""))]
        if absent:
            raise ValueError(
                f"{target}: [{locale}] is missing or unfilled: {', '.join(absent)}")
        locales[locale] = {key: str(section[key]) for key in REQUIRED_LOCALE}

    return Identity(
        full_name=str(person["full_name"]),
        file_prefix=str(person["file_prefix"]),
        email=str(person["email"]),
        phone=str(person["phone"]),
        date_of_birth=str(person["date_of_birth"]),
        linkedin=str(person["linkedin"]),
        street=str(person["street"]),
        city=str(person["city"]),
        locales=locales,
    )


def main() -> int:
    ident = load()
    print(f"identity.toml : {IDENTITY_PATH}")
    print(f"name          : {ident.full_name}")
    print(f"documents     : {ident.doc_name('CV', '.pdf')}")
    for locale in LOCALES:
        print(f"address ({locale})  : {ident.address(locale)} "
              f"[{ident.locales[locale]['nationality']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
