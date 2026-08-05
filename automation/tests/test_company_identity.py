"""One employer is one employer, however the legal form is spelled.

`company_key` is what decides whether the folder on disk belongs to the posting
being built. It is used twice and both readings matter: `locate_output` asks it
whether the run produced anything, and `check_duplicate` asks it whether the
role has already been applied to.

It got `PFALZWERKE AKTIENGESELLSCHAFT` wrong. The suffix list carried `ag` but
not the word spelled out, so the posting keyed to `pfalzwerke aktiengesellschaft`
while the folder the pipeline had just written keyed to `pfalzwerke`. The run had
produced a complete application — CV, cover letter, interview prep, tracker row —
and was reported as `the build reported success but no dated folder appeared`.
Nothing was lost that time, only mislabelled, but the same miss on the other side
of the pipeline would let the same job be applied to twice.

The second half of the file is about a subtler version of the same thing: the
suffix list happily accepted entries like `"b.v."` that could never match,
because keys are punctuation-stripped before they are compared.
"""

from __future__ import annotations

import pytest

from watcher.normalize import LEGAL_SUFFIXES, company_key


# --------------------------------------------------------------------------
# the regression
# --------------------------------------------------------------------------

def test_a_spelled_out_legal_form_is_the_same_employer():
    """The exact pair that cost build #17 its verdict."""
    assert company_key("PFALZWERKE AKTIENGESELLSCHAFT") == company_key("PFALZWERKE")


@pytest.mark.parametrize("written, short", [
    ("Acme Aktiengesellschaft", "Acme"),
    ("Acme AG", "Acme"),
    ("Acme SE", "Acme"),
    ("Acme GmbH", "Acme"),
    ("Acme GmbH & Co. KG", "Acme"),
    ("Acme Holding GmbH", "Acme"),
    ("Acme Deutschland GmbH", "Acme"),
])
def test_legal_forms_do_not_change_who_the_employer_is(written, short):
    assert company_key(written) == company_key(short)


# --------------------------------------------------------------------------
# dotted abbreviations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("dotted, plain", [
    ("Acme B.V.", "Acme BV"),
    ("Acme N.V.", "Acme NV"),
    ("Acme S.A.", "Acme SA"),
    ("Acme A.S.", "Acme AS"),
])
def test_a_dotted_legal_form_reaches_the_suffix_list(dotted, plain):
    """`B.V.` used to survive as the two tokens `b` and `v`, matching neither."""
    assert company_key(dotted) == company_key(plain) == company_key("Acme")


def test_no_suffix_can_be_written_in_a_form_that_never_matches():
    """Keys are punctuation-stripped, so a punctuated entry is unreachable.

    This is the check that would have caught the dotted entries when they were
    added, rather than leaving four dead strings in the set that read as
    coverage.
    """
    unreachable = sorted(s for s in LEGAL_SUFFIXES if not s.isalnum())
    assert unreachable == [], (
        "these suffixes contain punctuation and can never match a token")


# --------------------------------------------------------------------------
# and it still has to tell employers apart
# --------------------------------------------------------------------------

@pytest.mark.parametrize("left, right", [
    ("Pfalzwerke AG", "Pfalzwerke Netz AG"),
    ("Deutsche Börse AG", "Deutsche Bank AG"),
    ("Acme Robotics", "Acme Health"),
])
def test_different_employers_stay_different(left, right):
    """Stripping more suffixes must not start collapsing real distinctions."""
    assert company_key(left) != company_key(right)


def test_the_spellings_already_in_the_tree_still_collapse():
    """Three folders under 2026/ that are one employer."""
    keys = {company_key(n) for n in
            ("Deutsche Börse Group", "Deutsche Boerse Group", "Deutsche Börse AG")}
    assert len(keys) == 1


def test_a_name_that_is_only_a_legal_form_keeps_something():
    """Better a useless key than an empty one that matches every other empty."""
    assert company_key("GmbH")
