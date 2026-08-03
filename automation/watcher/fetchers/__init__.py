"""Fetcher registry.

`fetch_source` dispatches a sources.toml entry to the right implementation and
always raises on failure rather than returning partial results — the caller
records the failure against source health and moves on to the next source, so
one broken portal never costs a poll.
"""

from __future__ import annotations

from typing import Any

from ..normalize import Posting
from . import ats

# Portals implemented so far. Anything listed in sources.toml but absent here
# is reported as pending rather than treated as an error.
PORTAL_MODULES: dict[str, str] = {
    "arbeitsagentur": "arbeitsagentur",
    "hiringcafe": "hiringcafe",
    "stepstone": "stepstone",
}


class SourceNotImplemented(RuntimeError):
    """Configured in sources.toml but no fetcher exists yet."""


def _load_portal(name: str):
    module_name = PORTAL_MODULES.get(name)
    if not module_name:
        raise SourceNotImplemented(f"no fetcher for portal {name!r}")
    try:
        import importlib

        return importlib.import_module(f".{module_name}", __package__)
    except ModuleNotFoundError as exc:
        raise SourceNotImplemented(f"portal {name!r} not implemented yet") from exc


def fetch_source(entry: dict[str, Any], timeout: int) -> list[Posting]:
    if entry.get("kind") == "ats" or "provider" in entry:
        return ats.fetch(entry, timeout)
    module = _load_portal(entry.get("name", ""))
    return module.fetch(entry, timeout)


def hydrate(posting: Posting, timeout: int) -> str:
    """Fetch a posting body that the list endpoint did not include.

    Called only for postings that already passed the prefilter, so we never
    spend a request on a description that was about to be discarded.
    """
    if not posting.detail_url:
        return posting.description
    hydrator = ats.HYDRATORS.get(posting.provider)
    if hydrator:
        return hydrator(posting, timeout)
    try:
        module = _load_portal(posting.provider)
    except SourceNotImplemented:
        return posting.description
    if hasattr(module, "hydrate"):
        return module.hydrate(posting, timeout)
    return posting.description
