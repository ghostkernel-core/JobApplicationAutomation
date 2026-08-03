"""The candidate profile the matcher scores against.

`rules/00-canonical-profile.md` is the workspace's source of truth, but it is
written for document authoring — verbatim experience wording, approved
vocabulary, publication lists. Feeding all of it into every scoring batch pays
for a lot of text that cannot change a match decision.

So it is condensed once into a matching-oriented digest and cached. The cache
is keyed on the canonical file's size and modification time, so editing the
canonical profile regenerates the digest on the next run and nothing else has
to remember to.

The digest is a derived artifact. It is never a fact source for documents.
"""

from __future__ import annotations

import logging

from . import store
from .claude_cli import run
from .config import CANONICAL_PROFILE_PATH, PROFILE_DIGEST_PATH, PROFILE_KB_PATH

log = logging.getLogger("watcher.profile")

_DIGEST_PROMPT = """\
Condense the candidate profile below into a compact digest used to judge whether \
a job posting is a good fit. Target 350-450 words.

Keep: current title and seniority, years of experience, core technical skills and \
tools, domains worked in, education status, languages spoken with levels, work \
authorisation and location, and the kinds of roles being targeted.

Include in-progress or conditionally-used qualifications, marked as such — the \
profile may say a degree is ongoing, or that a credential is only mentioned for \
certain postings. Whether it goes on a CV is decided later; a screener still \
needs to know it exists and what its status is.

Drop: publication lists, prose style guidance, formatting rules, and anything \
that only matters when writing a CV.

Be strictly factual. Do not add, upgrade, or soften any claim. If the profile \
does not state something, it is absent — say nothing about it. Output only the \
digest, with no preamble.

--- CANDIDATE PROFILE ---
{profile}
"""


def _cache_key() -> str:
    stat = CANONICAL_PROFILE_PATH.stat()
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def get_digest(model: str = "sonnet", force: bool = False,
               timeout: int = 300) -> str:
    """Return the cached matching digest, regenerating it when stale."""
    if not CANONICAL_PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"canonical profile not found at {CANONICAL_PROFILE_PATH}"
        )

    key = _cache_key()
    store.init_db()
    with store.connect() as conn:
        cached_key = store.get_meta(conn, "profile_digest_key")

    if not force and PROFILE_DIGEST_PATH.exists() and cached_key == key:
        return PROFILE_DIGEST_PATH.read_text(encoding="utf-8")

    log.info("regenerating profile digest from %s", CANONICAL_PROFILE_PATH.name)
    profile = CANONICAL_PROFILE_PATH.read_text(encoding="utf-8")
    digest = run(_DIGEST_PROMPT.format(profile=profile), model=model,
                 timeout=timeout).strip()
    if len(digest) < 200:
        raise RuntimeError(f"profile digest came back too short: {digest!r}")

    PROFILE_DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_DIGEST_PATH.write_text(digest, encoding="utf-8")
    with store.connect() as conn:
        store.set_meta(conn, "profile_digest_key", key)
    return digest


def get_kb() -> str:
    """Learned matching preferences. Empty string if the file is missing."""
    if not PROFILE_KB_PATH.exists():
        return ""
    return PROFILE_KB_PATH.read_text(encoding="utf-8")
