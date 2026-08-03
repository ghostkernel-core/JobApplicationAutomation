"""Geography: continents, regions, countries, cities.

Job boards write location as free text — `Düsseldorf, Germany`, `Berlin`,
`EMEA`, `Remote (Europe)`, `München / Munich`. Nothing here is authoritative;
it is a best-effort read whose failure mode is deliberately one-sided.

    resolved   -> the filter applies
    unresolved -> the posting passes through to the matcher

That asymmetry is the whole design. A geography filter that drops what it
cannot parse turns every new board format into silent, invisible data loss —
the postings you never hear about are exactly the ones you cannot audit. So an
unknown country is never a rejection; it becomes a notification instead, and
the matcher explains what it thinks the location is.

The tables are shared: `fetchers/base.py` reads country detection from here so
the city list exists in one place rather than drifting between two.
"""

from __future__ import annotations

import re
from typing import Iterable

# ---------------------------------------------------------------------------
# Cities -> ISO 3166-1 alpha-2
#
# Only cities that plausibly appear in a posting you would want. This is a
# recall aid, not a gazetteer: a missing city costs nothing (the country name
# usually appears alongside it), while a wrong one silently misfiles a posting.
#
# Aliases for one city are joined with "/", canonical spelling first. That
# grouping is what lets a hand-written `cities = ["Munich"]` in sources.toml
# match a board that writes `München`, `Muenchen`, or `Munchen` — the user
# should never have to guess which spelling the employer used.
# ---------------------------------------------------------------------------
_CITY_SOURCE: dict[str, tuple[str, ...]] = {
    "DE": (
        "Berlin", "Munich/München/Muenchen/Munchen", "Hamburg",
        "Frankfurt/Frankfurt am Main", "Cologne/Köln/Koeln",
        "Düsseldorf/Duesseldorf/Dusseldorf", "Stuttgart", "Leipzig", "Dresden",
        "Hannover/Hanover", "Nuremberg/Nürnberg/Nuernberg",
        "Dortmund", "Essen", "Bremen", "Bonn", "Karlsruhe", "Mannheim",
        "Münster/Muenster", "Aachen", "Darmstadt", "Heidelberg",
        "Wuppertal", "Bielefeld", "Ingolstadt", "Walldorf", "Erlangen",
        "Freiburg", "Augsburg", "Mainz", "Wiesbaden", "Kassel", "Kiel",
        "Lübeck/Luebeck", "Regensburg", "Ulm", "Braunschweig", "Jena",
        "Potsdam", "Paderborn", "Bochum", "Duisburg", "Krefeld", "Siegen",
        "Osnabrück/Osnabrueck", "Hagen", "Iserlohn", "Soest", "Meschede",
    ),
    "AT": ("Vienna/Wien", "Graz", "Linz", "Salzburg", "Innsbruck", "Klagenfurt"),
    "CH": ("Zurich/Zürich/Zuerich", "Geneva/Genf/Genève", "Basel",
           "Bern/Berne", "Lausanne", "Lugano", "Winterthur", "Zug", "St. Gallen"),
    "NL": ("Amsterdam", "Rotterdam", "Utrecht", "Eindhoven",
           "The Hague/Den Haag", "Delft", "Groningen", "Nijmegen", "Leiden",
           "Enschede", "Tilburg", "Almere", "Haarlem"),
    "BE": ("Brussels/Bruxelles/Brussel", "Antwerp/Antwerpen", "Ghent/Gent",
           "Leuven", "Liège/Liege", "Bruges/Brugge", "Mechelen"),
    "LU": ("Luxembourg/Luxemburg", "Esch-sur-Alzette"),
    "DK": ("Copenhagen/København/Kobenhavn", "Aarhus", "Odense",
           "Aalborg", "Lyngby"),
    "SE": ("Stockholm", "Gothenburg/Göteborg/Goteborg", "Malmö/Malmo",
           "Uppsala", "Lund", "Linköping/Linkoping", "Västerås", "Solna"),
    "NO": ("Oslo", "Bergen", "Trondheim", "Stavanger", "Tromsø/Tromso"),
    "FI": ("Helsinki", "Espoo", "Tampere", "Oulu", "Turku", "Vantaa"),
    "IS": ("Reykjavik/Reykjavík",),
    "IE": ("Dublin", "Cork", "Galway", "Limerick", "Waterford"),
    "ES": ("Madrid", "Barcelona", "Valencia", "Seville/Sevilla", "Bilbao",
           "Malaga/Málaga", "Zaragoza", "Alicante", "Palma", "Granada",
           "San Sebastian", "A Coruña/Coruna"),
    "PT": ("Lisbon/Lisboa", "Porto", "Braga", "Coimbra", "Aveiro", "Faro"),
    "IT": ("Milan/Milano", "Rome/Roma", "Turin/Torino", "Bologna",
           "Naples/Napoli", "Florence/Firenze", "Genoa/Genova",
           "Padua/Padova", "Trento", "Trieste", "Pisa", "Verona", "Bari"),
    "FR": ("Paris", "Lyon", "Toulouse", "Nantes", "Bordeaux", "Lille",
           "Grenoble", "Marseille", "Sophia Antipolis", "Nice", "Rennes",
           "Strasbourg", "Montpellier", "Saclay", "Toulon"),
    "PL": ("Warsaw/Warszawa", "Krakow/Kraków/Cracow", "Wroclaw/Wrocław",
           "Gdansk/Gdańsk", "Poznan/Poznań", "Lodz/Łódź",
           "Katowice", "Szczecin", "Gdynia", "Rzeszow"),
    "CZ": ("Prague/Praha", "Brno", "Ostrava", "Plzen/Plzeň", "Olomouc"),
    "SK": ("Bratislava", "Kosice/Košice"),
    "HU": ("Budapest", "Debrecen", "Szeged"),
    "RO": ("Bucharest/București/Bucuresti", "Cluj/Cluj-Napoca",
           "Timisoara/Timișoara", "Iasi/Iași", "Brasov"),
    "BG": ("Sofia", "Plovdiv", "Varna", "Burgas"),
    "HR": ("Zagreb", "Split", "Rijeka", "Osijek"),
    "SI": ("Ljubljana", "Maribor"),
    "GR": ("Athens/Athina", "Thessaloniki", "Patras", "Heraklion"),
    "EE": ("Tallinn", "Tartu"),
    "LV": ("Riga/Rīga",),
    "LT": ("Vilnius", "Kaunas", "Klaipeda"),
    "CY": ("Nicosia", "Limassol"),
    "MT": ("Valletta", "Sliema"),
    "RS": ("Belgrade/Beograd", "Novi Sad"),
    "UA": ("Kyiv/Kiev", "Lviv", "Kharkiv", "Odesa"),
    "TR": ("Istanbul", "Ankara", "Izmir"),
    "GB": ("London", "Manchester", "Cambridge", "Oxford", "Edinburgh",
           "Bristol", "Glasgow", "Leeds", "Birmingham", "Reading", "Belfast",
           "Sheffield", "Newcastle", "Nottingham", "Cardiff", "Brighton"),
    "US": ("New York/NYC", "Brooklyn", "San Francisco", "Seattle", "Austin",
           "Boston", "Chicago", "Los Angeles", "Denver", "Atlanta", "Dallas",
           "Houston", "San Diego", "San Jose", "Palo Alto", "Mountain View",
           "Sunnyvale", "Bellevue", "Redmond", "Portland", "Miami", "Phoenix",
           "Philadelphia", "Washington DC", "Pittsburgh", "Raleigh", "Detroit"),
    "CA": ("Toronto", "Vancouver", "Montreal/Montréal", "Ottawa", "Calgary",
           "Waterloo", "Edmonton", "Quebec"),
    "IN": ("Bangalore/Bengaluru", "Hyderabad", "Pune", "Chennai", "Mumbai",
           "Gurgaon/Gurugram", "Noida", "Delhi/New Delhi", "Kolkata",
           "Ahmedabad", "Kochi"),
    "SG": ("Singapore",),
    "AE": ("Dubai", "Abu Dhabi"),
    "IL": ("Tel Aviv", "Herzliya", "Haifa", "Jerusalem"),
    "AU": ("Sydney", "Melbourne", "Brisbane", "Perth", "Canberra"),
    "NZ": ("Auckland", "Wellington"),
    "JP": ("Tokyo", "Osaka", "Kyoto", "Yokohama"),
    "CN": ("Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Guangzhou"),
    "KR": ("Seoul", "Busan"),
    "BR": ("Sao Paulo/São Paulo", "Rio de Janeiro", "Belo Horizonte"),
    "MX": ("Mexico City", "Guadalajara", "Monterrey"),
    "AR": ("Buenos Aires", "Cordoba"),
    "ZA": ("Cape Town", "Johannesburg", "Pretoria", "Durban"),
    "EG": ("Cairo", "Alexandria"),
    "MA": ("Casablanca", "Rabat"),
    "NG": ("Lagos", "Abuja"),
    "KE": ("Nairobi",),
    "PK": ("Karachi", "Lahore", "Islamabad"),
    "BD": ("Dhaka", "Chittagong"),
    "VN": ("Hanoi", "Ho Chi Minh City/Saigon"),
    "PH": ("Manila", "Cebu"),
    "ID": ("Jakarta", "Bandung"),
    "MY": ("Kuala Lumpur", "Penang"),
    "TH": ("Bangkok",),
}

# ---------------------------------------------------------------------------
# Country names and demonyms, for when the posting names the country outright.
# ---------------------------------------------------------------------------
_COUNTRY_NAMES: dict[str, tuple[str, ...]] = {
    "DE": ("Germany", "Deutschland", "German", "Allemagne", "BRD"),
    "AT": ("Austria", "Österreich", "Oesterreich", "Austrian"),
    "CH": ("Switzerland", "Schweiz", "Suisse", "Svizzera", "Swiss"),
    "NL": ("Netherlands", "Nederland", "Holland", "Dutch"),
    "BE": ("Belgium", "België", "Belgie", "Belgique", "Belgian"),
    "LU": ("Luxembourg", "Luxemburg"),
    "DK": ("Denmark", "Danmark", "Danish"),
    "SE": ("Sweden", "Sverige", "Swedish"),
    "NO": ("Norway", "Norge", "Noreg", "Norwegian"),
    "FI": ("Finland", "Suomi", "Finnish"),
    "IS": ("Iceland", "Ísland", "Island (IS)"),
    "IE": ("Ireland", "Éire", "Eire", "Irish"),
    "ES": ("Spain", "España", "Espana", "Spanish"),
    "PT": ("Portugal", "Portuguese"),
    "IT": ("Italy", "Italia", "Italian"),
    "FR": ("France", "French"),
    "PL": ("Poland", "Polska", "Polish"),
    "CZ": ("Czech Republic", "Czechia", "Česko", "Cesko", "Czech"),
    "SK": ("Slovakia", "Slovensko", "Slovak"),
    "HU": ("Hungary", "Magyarország", "Magyarorszag", "Hungarian"),
    "RO": ("Romania", "România", "Romanian"),
    "BG": ("Bulgaria", "Bulgarian"),
    "HR": ("Croatia", "Hrvatska", "Croatian"),
    "SI": ("Slovenia", "Slovenija", "Slovenian"),
    "GR": ("Greece", "Ελλάδα", "Hellas", "Greek"),
    "EE": ("Estonia", "Eesti", "Estonian"),
    "LV": ("Latvia", "Latvija", "Latvian"),
    "LT": ("Lithuania", "Lietuva", "Lithuanian"),
    "CY": ("Cyprus",),
    "MT": ("Malta", "Maltese"),
    "RS": ("Serbia", "Srbija"),
    "UA": ("Ukraine", "Ukraina", "Ukrainian"),
    "TR": ("Turkey", "Türkiye", "Turkiye"),
    "GB": ("United Kingdom", "Great Britain", "England", "Scotland", "Wales",
           "Northern Ireland", "UK", "British"),
    "US": ("United States", "USA", "U.S.A.", "U.S.", "America", "American"),
    "CA": ("Canada", "Canadian"),
    "IN": ("India", "Indian"),
    "SG": ("Singapore",),
    "AE": ("United Arab Emirates", "UAE"),
    "IL": ("Israel", "Israeli"),
    "AU": ("Australia", "Australian"),
    "NZ": ("New Zealand",),
    "JP": ("Japan", "Japanese"),
    "CN": ("China", "Chinese"),
    "KR": ("South Korea", "Korea"),
    "BR": ("Brazil", "Brasil", "Brazilian"),
    "MX": ("Mexico", "México"),
    "AR": ("Argentina",),
    "ZA": ("South Africa",),
    "EG": ("Egypt",),
    "MA": ("Morocco", "Maroc"),
    "NG": ("Nigeria",),
    "KE": ("Kenya",),
    "PK": ("Pakistan",),
    "BD": ("Bangladesh",),
    "VN": ("Vietnam", "Viet Nam"),
    "PH": ("Philippines",),
    "ID": ("Indonesia",),
    "MY": ("Malaysia",),
    "TH": ("Thailand",),
}

# ---------------------------------------------------------------------------
# Continents and named regions.
#
# These exist so `sources.toml` can say `regions = ["DACH", "BENELUX"]` instead
# of a wall of ISO codes. Everything resolves down to country codes before any
# comparison happens, so a region and a bare country code are interchangeable
# wherever a list of places is accepted.
# ---------------------------------------------------------------------------
CONTINENTS: dict[str, tuple[str, ...]] = {
    "EUROPE": (
        "DE", "AT", "CH", "NL", "BE", "LU", "DK", "SE", "NO", "FI", "IS", "IE",
        "ES", "PT", "IT", "FR", "PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI",
        "GR", "EE", "LV", "LT", "CY", "MT", "RS", "UA", "GB", "TR",
    ),
    "NORTH_AMERICA": ("US", "CA", "MX"),
    "SOUTH_AMERICA": ("BR", "AR"),
    "ASIA": ("IN", "SG", "AE", "IL", "JP", "CN", "KR", "PK", "BD", "VN", "PH",
             "ID", "MY", "TH", "TR"),
    "AFRICA": ("ZA", "EG", "MA", "NG", "KE"),
    "OCEANIA": ("AU", "NZ"),
}

REGIONS: dict[str, tuple[str, ...]] = {
    # Political / economic blocs
    "EU": ("DE", "AT", "NL", "BE", "LU", "DK", "SE", "FI", "IE", "ES", "PT",
           "IT", "FR", "PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "GR",
           "EE", "LV", "LT", "CY", "MT"),
    "EFTA": ("CH", "NO", "IS", "LI"),
    # The one that matters for a residence permit: EU + EFTA is where the
    # existing German permit converts most cleanly.
    "EEA": ("DE", "AT", "NL", "BE", "LU", "DK", "SE", "FI", "IE", "ES", "PT",
            "IT", "FR", "PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "GR",
            "EE", "LV", "LT", "CY", "MT", "NO", "IS", "LI"),
    # Cultural / linguistic groupings people actually think in
    "DACH": ("DE", "AT", "CH"),
    "BENELUX": ("NL", "BE", "LU"),
    "NORDICS": ("DK", "SE", "NO", "FI", "IS"),
    "SCANDINAVIA": ("DK", "SE", "NO"),
    "BALTICS": ("EE", "LV", "LT"),
    "IBERIA": ("ES", "PT"),
    "CEE": ("PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "EE", "LV", "LT"),
    "UK_IE": ("GB", "IE"),
    # Recruiter shorthand that shows up in posting locations verbatim
    "EMEA": CONTINENTS["EUROPE"] + ("AE", "IL", "ZA", "EG", "MA", "NG", "KE"),
}


def _key(value: str) -> str:
    """Fold a place name to its lookup key: accents expanded, punctuation gone.

    German transliteration first (`ü` -> `ue`), matching how a German board
    spells its own cities when it drops the umlaut, so `München`, `Muenchen`
    and `Munchen` land on one key.
    """
    text = value.translate(str.maketrans({
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
        "Ä": "ae", "Ö": "oe", "Ü": "ue",
        "å": "a", "ø": "o", "æ": "ae", "Å": "a", "Ø": "o", "Æ": "ae",
    }))
    import unicodedata

    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    ).casefold()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


# city key -> country code, and city key -> a display name
CITY_COUNTRY: dict[str, str] = {}
CITY_DISPLAY: dict[str, str] = {}
for _code, _entries in _CITY_SOURCE.items():
    for _entry in _entries:
        _aliases = [a.strip() for a in _entry.split("/") if a.strip()]
        _canonical = _aliases[0]
        for _alias in _aliases:
            _k = _key(_alias)
            if not _k:
                continue
            # setdefault, not assignment: a name shared by two countries
            # (Frankfurt, Cordoba, Waterloo) keeps whichever was declared
            # first, and the country-name check in `country_of` outranks the
            # city lookup anyway.
            CITY_COUNTRY.setdefault(_k, _code)
            CITY_DISPLAY.setdefault(_k, _canonical)

COUNTRY_NAME_KEYS: dict[str, str] = {}
for _code, _names in _COUNTRY_NAMES.items():
    for _name in _names:
        # A name written in a non-Latin script folds to the empty string, and
        # an empty alternative in the regex below matches at every position —
        # which silently made every location resolve to whichever country
        # owned it. Non-Latin spellings are dropped rather than special-cased;
        # boards write locations in Latin script.
        _nk = _key(_name)
        if _nk:
            COUNTRY_NAME_KEYS.setdefault(_nk, _code)

ALL_COUNTRIES: frozenset[str] = frozenset(_CITY_SOURCE) | frozenset(_COUNTRY_NAMES)

# Longest-first so `New York` is not shadowed by a shorter overlapping entry,
# and `\b` so `Bern` never matches inside `Bernburg`.
_CITY_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(k) for k in sorted(CITY_COUNTRY, key=len, reverse=True)
    ) + r")\b"
)
_COUNTRY_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(k) for k in sorted(COUNTRY_NAME_KEYS, key=len, reverse=True)
    ) + r")\b"
)

# Two-letter codes are only trusted when they stand alone as a field, because
# `DE` also opens `DEvOps` and `IN` is an English preposition.
_BARE_CODE_RE = re.compile(r"(?:^|[,(\[/|]\s*)([A-Z]{2})(?:\s*[,)\]/|]|$)")


def expand(names: Iterable[str]) -> set[str]:
    """Resolve a mixed list of continents, regions, and countries to ISO codes.

    `["DACH", "NORDICS", "PT"]` -> `{DE, AT, CH, DK, SE, NO, FI, IS, PT}`.
    Unknown tokens are ignored rather than raising: a typo in `sources.toml`
    should cost you one region, not the whole watcher at boot.
    """
    out: set[str] = set()
    for raw in names or ():
        token = str(raw).strip().upper().replace(" ", "_").replace("-", "_")
        if not token:
            continue
        if token in CONTINENTS:
            out.update(CONTINENTS[token])
        elif token in REGIONS:
            out.update(REGIONS[token])
        elif token == "WORLDWIDE" or token == "ANY":
            out.update(ALL_COUNTRIES)
        elif len(token) == 2 and token.isalpha():
            out.add(token)
        else:
            # Maybe it is a country spelled out: "Germany", "Czech Republic".
            code = COUNTRY_NAME_KEYS.get(_key(str(raw)))
            if code:
                out.add(code)
    return out


def expand_cities(names: Iterable[str]) -> set[str]:
    """Normalise a hand-written city list to lookup keys."""
    return {_key(str(n)) for n in names or () if str(n).strip()}


def city_of(*fragments: object) -> str:
    """Best-effort city name from free-text location fragments. '' when unsure.

    Returns the canonical display spelling, so `Muenchen` and `München` both
    come back as `Munich` — which is what makes a hand-written `cities` list in
    `sources.toml` work without the user guessing the board's spelling.
    """
    text = _key(" ".join(str(f) for f in fragments if f))
    if not text:
        return ""
    match = _CITY_RE.search(text)
    if not match:
        return ""
    return CITY_DISPLAY.get(match.group(0), match.group(0).title())


def city_key_of(*fragments: object) -> str:
    """The lookup key for the detected city, for comparison against a config list."""
    text = _key(" ".join(str(f) for f in fragments if f))
    if not text:
        return ""
    match = _CITY_RE.search(text)
    return match.group(0) if match else ""


def country_of(*fragments: object) -> str:
    """Best-effort ISO country code. '' when the text does not say.

    Country names beat cities: `Frankfurt, Kentucky, United States` is US, and
    reading the city first would call it Germany.
    """
    raw = " ".join(str(f) for f in fragments if f)
    if not raw:
        return ""
    text = _key(raw)
    match = _COUNTRY_RE.search(text)
    if match:
        return COUNTRY_NAME_KEYS[match.group(0)]
    bare = _BARE_CODE_RE.search(raw)
    if bare and bare.group(1) in ALL_COUNTRIES:
        return bare.group(1)
    city = _CITY_RE.search(text)
    if city:
        return CITY_COUNTRY[city.group(0)]
    return ""


def continent_of(country: str) -> str:
    """Which continent a country code sits on. '' for unknown codes."""
    code = (country or "").upper()
    for name, members in CONTINENTS.items():
        if code in members:
            return name
    return ""


def describe(country: str) -> str:
    """Human label for a country code, for log lines and Telegram messages."""
    code = (country or "").upper()
    names = _COUNTRY_NAMES.get(code)
    return names[0] if names else code


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

# (location text, expected country, expected city)
_FIXTURES: tuple[tuple[str, str, str], ...] = (
    ("Düsseldorf, Germany", "DE", "Düsseldorf"),
    ("Duesseldorf", "DE", "Düsseldorf"),
    ("München", "DE", "Munich"),
    ("Muenchen, DE", "DE", "Munich"),
    ("Berlin", "DE", "Berlin"),
    ("Frankfurt am Main", "DE", "Frankfurt"),
    ("Amsterdam, Netherlands", "NL", "Amsterdam"),
    ("Zurich, Switzerland", "CH", "Zurich"),
    ("Wien", "AT", "Vienna"),
    ("Copenhagen, Denmark", "DK", "Copenhagen"),
    ("London, United Kingdom", "GB", "London"),
    ("New York, NY, United States", "US", "New York"),
    ("Bengaluru, India", "IN", "Bangalore"),
    # The country name must beat the city: Kentucky's Frankfort aside, a
    # posting that spells out the country has already told us the answer.
    ("Frankfurt, Kentucky, United States", "US", "Frankfurt"),
    ("Remote (Europe)", "", ""),
    ("EMEA", "", ""),
    ("", "", ""),
)

_EXPAND_FIXTURES: tuple[tuple[tuple[str, ...], set[str]], ...] = (
    (("DACH",), {"DE", "AT", "CH"}),
    (("BENELUX",), {"NL", "BE", "LU"}),
    (("NORDICS",), {"DK", "SE", "NO", "FI", "IS"}),
    (("DACH", "PT"), {"DE", "AT", "CH", "PT"}),
    (("Germany",), {"DE"}),
    (("NOT_A_REGION",), set()),
)


def _self_test() -> int:
    failures = 0
    print("country_of / city_of")
    for text, want_country, want_city in _FIXTURES:
        got_country, got_city = country_of(text), city_of(text)
        ok = got_country == want_country and got_city == want_city
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        detail = "" if ok else f"  (want {want_country or '-'}/{want_city or '-'})"
        label = f"{got_country or '-'}/{got_city or '-'}"
        print(f"  {mark} {text!r:38} -> {label}{detail}")

    print("\nexpand")
    for names, want in _EXPAND_FIXTURES:
        got = expand(names)
        ok = got == want
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        shown = ",".join(sorted(got)) or "-"
        print(f"  {mark} {list(names)!s:24} -> {shown}"
              + ("" if ok else f"  (want {','.join(sorted(want)) or '-'})"))

    print(f"\ntables: {len(CITY_COUNTRY)} city keys, {len(ALL_COUNTRIES)} countries, "
          f"{len(REGIONS)} regions, {len(CONTINENTS)} continents")
    total = len(_FIXTURES) + len(_EXPAND_FIXTURES)
    print(f"{total - failures}/{total} passed.")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--resolve", help="resolve one location string")
    parser.add_argument("--expand", nargs="+",
                        help="expand region/continent names to country codes")
    args = parser.parse_args(argv)

    if args.resolve:
        code = country_of(args.resolve)
        print(f"country: {code or '(unknown)'}"
              f"  city: {city_of(args.resolve) or '(unknown)'}"
              f"  continent: {continent_of(code) or '(unknown)'}")
    if args.expand:
        codes = sorted(expand(args.expand))
        print(f"{len(codes)} countries: {', '.join(codes) or '(none)'}")
    if args.self_test or not (args.resolve or args.expand):
        return _self_test()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
