"""Find which job board a company actually uses.

    python -m watcher.discover --company "Remerge"
    python -m watcher.discover --company "Zillow Group" --slug zillowgroup

Adding a company to sources.toml means knowing both its ATS provider and the
exact slug that provider filed it under, and neither is guessable from the
company name — `Zillow Group` might be `zillow`, `zillowgroup`, or something
unrelated. This probes every supported provider against a set of plausible
slugs and prints a TOML block ready to paste.

Workday is not probed: it needs a host, tenant, and site that only appear in
the company's own careers URL. Open the careers page, copy the URL, and read
the three values out of `<host>/<site>/...` and `/wday/cxs/<tenant>/<site>/`.
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .fetchers.base import session
from .logsetup import force_utf8


@dataclass(frozen=True)
class Probe:
    provider: str
    slug: str
    count: int
    sample: str


def slug_variants(name: str, extra: list[str] | None = None) -> list[str]:
    base = unicodedata.normalize("NFKD", name)
    base = "".join(c for c in base if not unicodedata.combining(c))
    base = re.sub(r"[^A-Za-z0-9\s-]", "", base).strip()
    words = base.split()
    lower = [w.lower() for w in words]

    out: list[str] = list(extra or [])
    if lower:
        out += [
            "".join(lower),
            "-".join(lower),
            lower[0],
            "".join(w.capitalize() for w in words),
            "".join(words),
        ]
        # Companies routinely file without the legal/marketing tail.
        if len(lower) > 1 and lower[-1] in {"group", "ag", "gmbh", "se", "inc", "holding"}:
            out += ["".join(lower[:-1]), "-".join(lower[:-1])]
    seen: set[str] = set()
    return [s for s in out if s and not (s in seen or seen.add(s))]


def _probe(provider: str, slug: str, timeout: int) -> Probe | None:
    sess = session()
    try:
        if provider == "greenhouse":
            r = sess.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                         timeout=timeout)
            if r.status_code != 200:
                return None
            jobs = r.json().get("jobs", [])
            titles = [j.get("title", "") for j in jobs]
        elif provider == "lever":
            r = sess.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                         timeout=timeout)
            if r.status_code != 200 or not isinstance(r.json(), list):
                return None
            jobs = r.json()
            titles = [j.get("text", "") for j in jobs]
        elif provider == "ashby":
            r = sess.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                         timeout=timeout)
            if r.status_code != 200:
                return None
            jobs = r.json().get("jobs", [])
            titles = [j.get("title", "") for j in jobs]
        elif provider == "smartrecruiters":
            r = sess.get(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=5",
                timeout=timeout)
            if r.status_code != 200:
                return None
            body = r.json()
            jobs = body.get("content", [])
            if not int(body.get("totalFound", 0)):
                return None
            titles = [j.get("name", "") for j in jobs]
        elif provider == "personio":
            r = sess.get(f"https://{slug}.jobs.personio.de/xml", timeout=timeout)
            if r.status_code != 200 or "<position" not in r.text:
                return None
            titles = re.findall(r"<name>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</name>", r.text)
            jobs = titles
        else:
            return None
    except Exception:
        return None

    if not jobs:
        return None
    return Probe(provider, slug, len(jobs), "; ".join(t for t in titles[:3] if t))


PROVIDERS = ("greenhouse", "lever", "ashby", "smartrecruiters", "personio")


def discover(company: str, extra_slugs: list[str] | None = None,
             timeout: int = 15) -> list[Probe]:
    slugs = slug_variants(company, extra_slugs)
    jobs = [(p, s) for p in PROVIDERS for s in slugs]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = pool.map(lambda a: _probe(a[0], a[1], timeout), jobs)
    hits = [r for r in results if r]
    return sorted(hits, key=lambda p: -p.count)


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--company", required=True)
    parser.add_argument("--slug", action="append", default=[],
                        help="extra slug to try; repeatable")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args(argv)

    hits = discover(args.company, args.slug, args.timeout)
    if not hits:
        print(f"No board found for {args.company!r}.")
        print("Tried slugs:", ", ".join(slug_variants(args.company, args.slug)))
        print("Open the company's careers page and check the URL — the slug is "
              "usually in it. Pass it with --slug. If the URL contains "
              "'myworkdayjobs.com', it is Workday and needs a manual entry.")
        return 1

    for hit in hits:
        print(f"  {hit.provider:16} {hit.slug:24} {hit.count:4} postings   {hit.sample[:70]}")
    best = hits[0]
    print("\nAdd to sources.toml:\n")
    print("[[ats]]")
    print(f'company = "{args.company}"')
    print(f'provider = "{best.provider}"')
    print(f'token = "{best.slug}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
