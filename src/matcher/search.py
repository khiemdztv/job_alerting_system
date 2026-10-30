"""Shared relevance and filters for search and alerts.

Matching folds Vietnamese accents, so "ke toan" still finds "Kế toán", but it
keeps enough of them to avoid collisions:

* a query word typed with diacritics ("điện") only matches the same accented word
  or the word written without any accents, never "diện" (đại diện);
* a short unaccented word ("IT", "kho", "DE") only matches that literal word,
  never "ít", "khó" or "để" — when it is an abbreviation (known, typed in capitals
  or the whole query) or the rest of the query is typed with accents.  A query
  typed entirely without accents ("ke toan tong hop") still folds every word;
* abbreviations are ambiguous in running Vietnamese text ("làm ca ba"), so
  outside the skill list they must appear in the title or tags.

The company name is never matched: working for "Công ty Xây dựng ABC" does not
make an accountant a construction job.

Score tiers returned by :func:`relevance`:

* 100 – the whole query phrase is in the title
* 80  – every concept is in the title
* 60  – every occupation concept is in the title; skills (Python, SQL, ...) are
  only in the description or tags.  This is the alert floor (``ALERT_MIN_SCORE``).
* 30–50 – some occupation concept is only mentioned in the description.  Only
  interactive ``/search`` shows these, after the title hits.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from src.common.text_utils import clean_vn_text, fold_vn_text, normalize_vn_text
from src.matcher.synonyms import _MATCH_GROUPS, _SYN_INDEX, expand_token

# Alerts and /myjobs only send postings whose title names the occupation.
ALERT_MIN_SCORE = 60
# Interactive /search also lists description-only mentions, ranked after them.
INTERACTIVE_MIN_SCORE = 1

_FILLERS = (
    "toi muon tim",
    "tim viec lam",
    "tim viec",
    "viec lam",
    "tuyen dung",
    "nhan vien",
    "chuyen vien",
    "ung tuyen",
    "tim job",
)
# Skills outside TECH_SKILLS that may legitimately be matched in a description.
_EXTRA_SKILLS = frozenset({
    "php", "html", "css", "ruby", "swift", "kotlin", "flutter", "react native", "android",
    "ios", "django", "spring", "spring boot", "laravel", "devops", "selenium", "unity",
    "excel", "autocad", "photoshop", "illustrator", "figma", "sap", "erp", "seo", "r", "c",
    "solidworks", "revit", "sketchup", "power bi", "nodejs", "node.js", "reactjs",
})
# Ambiguous short words that stay title/tag-only even though they are skills.
_AMBIGUOUS_SHORT_SKILLS = frozenset({"ai", "go", "r", "c"})
# Short tokens that are abbreviations, not Vietnamese syllables.
_ABBREVIATIONS = frozenset({
    "it", "de", "ba", "hr", "qa", "qc", "ai", "pr", "bd", "cs", "da", "ds", "pm", "po",
    "ui", "ux", "kol", "koc", "seo", "sem", "cfo", "cto", "ceo", "coo",
})
_SHORT_WORD = re.compile(r"[a-z]{1,3}")
_USD_RATE = 25_000


@lru_cache(maxsize=4096)
def _term_pattern(term: str) -> re.Pattern:
    # A boundary is only required where the term itself starts/ends with a word
    # character: ".net" must still match inside "asp.net".
    left = r"(?<!\w)" if re.match(r"\w", term) else ""
    right = r"(?!\w)" if re.search(r"\w$", term) else ""
    return re.compile(left + re.escape(term) + right)


def contains(text: str, term: str) -> bool:
    return bool(term and _term_pattern(term).search(text))


def timestamp(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


# ── Accent signatures ──────────────────────────────────────────────────────────


@lru_cache(maxsize=8192)
def _accent_signature(text: str) -> tuple[str, ...]:
    """Diacritics of each word, independent of tone placement ("hòa" == "hoà")."""
    words = []
    for word in text.split(" "):
        marks = [
            "đ" if char == "đ" else char
            for char in unicodedata.normalize("NFD", word)
            if char == "đ" or unicodedata.combining(char)
        ]
        words.append("".join(marks))
    return tuple(words)


@dataclass(frozen=True)
class _Term:
    folded: str
    # None: any spelling matches.  Otherwise the accent signature an occurrence
    # must have (an occurrence written without any accent is always accepted).
    signature: tuple[str, ...] | None
    title_only: bool


def _hit(term: _Term, accented: str, folded: str,
         exclude: tuple[int, int] | None = None) -> bool:
    pattern = _term_pattern(term.folded)
    if term.signature is None and exclude is None:
        return bool(pattern.search(folded))
    for match in pattern.finditer(folded):
        if exclude and match.start() < exclude[1] and match.end() > exclude[0]:
            continue  # inside the company name
        if term.signature is None:
            return True
        found = _accent_signature(accented[match.start():match.end()])
        if found == term.signature or not any(found):
            return True
    return False


@lru_cache(maxsize=1)
def _dictionary_accents() -> dict[str, str]:
    accents: dict[str, str] = {}
    for group in _MATCH_GROUPS:
        for term in group:
            accented = normalize_vn_text(term)
            folded = fold_vn_text(accented)
            if accented != folded:
                accents.setdefault(folded, accented)
    return accents


@lru_cache(maxsize=1)
def _skill_terms() -> frozenset[str]:
    from src.etl.transformer import TECH_SKILLS

    return frozenset(clean_vn_text(skill) for skill in TECH_SKILLS) | _EXTRA_SKILLS


def _make_term(folded: str, accented: str | None, *, abbreviation: bool,
               literal: bool) -> _Term:
    short = bool(_SHORT_WORD.fullmatch(folded))
    abbreviation = short and (abbreviation or folded in _ABBREVIATIONS)
    if accented and accented != folded:
        signature = _accent_signature(accented)
    elif short and (literal or abbreviation):
        signature = _accent_signature(folded)  # literal: no accents allowed
    else:
        signature = None
    skills = _skill_terms()
    title_only = abbreviation and (folded not in skills or folded in _AMBIGUOUS_SHORT_SKILLS)
    return _Term(folded, signature, title_only)


# ── Query understanding ─────────────────────────────────────────────────────────

_QUERY_SALARY = re.compile(
    r"(?<!\w)(?:(?:muc\s+)?luong\s*:?\s*)?"
    r"(?:(tren|tu|toi thieu|it nhat|>=?|from|over|above|min)\s*)?"
    r"(?:(duoi|toi da|khong qua|len den|up to|<=?|under|below|max)\s*)?"
    r"(\d+(?:[.,]\d+)?)\s*(?:(?:-|–|den|toi|to)\s*(\d+(?:[.,]\d+)?)\s*)?"
    r"(trieu|tr|million|usd|\$)(?!\w)"
)


def _salary_amount(number: str, unit: str) -> int:
    value = float(number.replace(",", "."))
    return int(value * (_USD_RATE if unit in {"usd", "$"} else 1_000_000))


def extract_salary_filter(query: str) -> tuple[str, int | None, int | None]:
    """Split "lương trên 15 triệu kế toán" into ("kế toán", 15_000_000, None).

    "trên/từ N" and a bare "N triệu" set a minimum, "dưới/tối đa N" a maximum and
    "từ A-B triệu" both.  Salary words never become required search terms.
    """
    accented = normalize_vn_text(query)
    folded = fold_vn_text(accented)
    match = _QUERY_SALARY.search(folded)
    if not match:
        return str(query or "").strip(), None, None
    lower_word, upper_word, first, second, unit = match.groups()
    first_value = _salary_amount(first, unit)
    if second:
        low, high = first_value, _salary_amount(second, unit)
        salary_min, salary_max = min(low, high), max(low, high)
    elif upper_word:
        salary_min, salary_max = None, first_value
    else:
        salary_min, salary_max = first_value, None
    rest = accented[: match.start()] + " " + accented[match.end():]
    return re.sub(r"\s+", " ", rest).strip(" ,"), salary_min, salary_max


def _blank(pattern: re.Pattern, folded: str, accented: str) -> tuple[str, str]:
    """Replace every match with spaces in both strings, keeping them aligned."""
    for match in pattern.finditer(folded):
        start, end = match.span()
        pad = " " * (end - start)
        folded = folded[:start] + pad + folded[end:]
        accented = accented[:start] + pad + accented[end:]
    return folded, accented


@lru_cache(maxsize=1)
def _multiword_terms() -> tuple[str, ...]:
    return tuple(sorted((t for t in _SYN_INDEX if " " in t), key=len, reverse=True))


@lru_cache(maxsize=1)
def _protected_slash_terms() -> tuple[str, ...]:
    return tuple(t for t in _skill_terms() | set(_SYN_INDEX) if "/" in t)


def _alternative_spans(folded: str) -> list[tuple[int, int]]:
    """Commas, semicolons and "/" separate alternatives ("kế toán, kiểm toán")."""
    protected = folded
    for term in _protected_slash_terms():
        protected = _term_pattern(term).sub(lambda m: m.group(0).replace("/", "_"), protected)
    spans, start = [], 0
    for match in re.finditer(r"[,;/|]", protected):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(folded)))
    return spans


def _groups(folded: str, accented: str, capitals: frozenset[str] = frozenset(),
            ) -> tuple[tuple[_Term, ...], ...]:
    tokens: list[tuple[str, str]] = []
    work = folded
    for term in _multiword_terms():
        pattern = _term_pattern(term)
        first = pattern.search(work)
        if not first:
            continue
        tokens.append((term, accented[first.start():first.end()]))
        work, _ = _blank(pattern, work, work)
    for match in re.finditer(r"[^,\s\-/|]+", work):
        if re.search(r"\w", match.group(0)):
            tokens.append((match.group(0), accented[match.start():match.end()]))
    # Someone who types accents means the unaccented short words literally
    # ("nhân viên kho" is not "khó"); a fully unaccented query is just folded.
    typed_with_accents = folded != accented
    single = len(tokens) == 1
    accents = _dictionary_accents()
    groups, seen = [], set()
    for token, typed in tokens:
        if token in seen:
            continue
        seen.add(token)
        groups.append(tuple(
            _make_term(term, typed, abbreviation=single or token in capitals,
                       literal=typed_with_accents)
            if term == token
            # Dictionary synonyms carry their own (correct) accents.
            else _make_term(term, accents.get(term), abbreviation=False, literal=True)
            for term in expand_token(token)
        ))
    return tuple(groups)


@lru_cache(maxsize=512)
def _query_plan(query: str) -> tuple[tuple[str, tuple[tuple[_Term, ...], ...]], ...]:
    """Alternatives of (phrase, AND-of-OR term groups) for one query."""
    accented = normalize_vn_text(query)
    folded = fold_vn_text(accented)
    # "IT", "BA" typed in capitals are abbreviations, not the syllables "it"/"ba".
    capitals = frozenset(w.lower() for w in re.findall(r"(?<!\w)[A-Z]{2,4}(?!\w)", str(query)))
    folded, accented = _blank(_QUERY_SALARY, folded, accented)
    for filler in _FILLERS:
        folded, accented = _blank(_term_pattern(filler), folded, accented)
    plan = []
    for start, end in _alternative_spans(folded):
        groups = _groups(folded[start:end], accented[start:end], capitals)
        if groups:
            plan.append((re.sub(r"\s+", " ", folded[start:end]).strip(), groups))
    return tuple(plan)


@lru_cache(maxsize=512)
def concepts(query: str) -> tuple[tuple[str, ...], ...]:
    """Required concept groups (folded synonyms) across every alternative."""
    groups = []
    for _, alternative in _query_plan(query):
        for group in alternative:
            folded = tuple(term.folded for term in group)
            if folded not in groups:
                groups.append(folded)
    return tuple(groups)


# ── Relevance ────────────────────────────────────────────────────────────────────


# Per-job text is derived once per snapshot item and reused by every query.
# The snapshot is cached per container, so these hit on every search after the
# first one; sized for the current table, beyond that they only cost a lookup.
_JOB_CACHE_SIZE = 32_768


@lru_cache(maxsize=_JOB_CACHE_SIZE)
def _title_fields(raw_title: str) -> tuple[str, str]:
    title = normalize_vn_text(raw_title)
    return title, fold_vn_text(title)


@lru_cache(maxsize=_JOB_CACHE_SIZE)
def _tags_text(tags: tuple[str, ...]) -> str:
    return clean_vn_text(" ".join(tags))


@lru_cache(maxsize=_JOB_CACHE_SIZE)
def _body_fields(blob: str, company: str) -> tuple[str, str, tuple[int, int] | None]:
    """(accented blob, folded blob, span of the company name to ignore)."""
    # ETL stores search_blob already normalized (older items: also folded).
    # Re-normalizing kilobytes per job made webhook searches time out.
    if not blob.islower():
        blob = blob.lower()
    folded = blob if blob.isascii() else fold_vn_text(blob)
    exclude = None
    company = normalize_vn_text(company)
    if len(company) >= 3:
        for haystack, needle in ((blob, company), (folded, fold_vn_text(company))):
            position = haystack.find(needle)
            if position >= 0:
                exclude = (position, position + len(needle))
                break
    return blob, folded, exclude


@lru_cache(maxsize=2048)
def _legacy_blob(title, description, requirements, tags) -> str:
    return normalize_vn_text(" ".join(str(v) for v in (title, description, requirements,
                                                       " ".join(tags)) if v))


def relevance(job: dict, query: str) -> int:
    plan = _query_plan(query)
    if not plan:
        return 0
    title, folded_title = _title_fields(str(job.get("title") or job.get("title_normalized", "")))
    tags = tuple(job.get("tags") or ())
    body = None
    best = 0
    for phrase, groups in plan:
        title_hits = [any(_hit(term, title, folded_title) for term in group) for group in groups]
        if not all(title_hits):
            if body is None:
                search_blob = str(job.get("search_blob") or "")
                if search_blob:
                    body = _body_fields(search_blob, str(job.get("company") or ""))
                else:
                    body = _body_fields(_legacy_blob(
                        title, job.get("description", ""), job.get("requirements", ""), tags
                    ), "")
            blob, folded_blob, exclude = body
            tag_text = _tags_text(tags)
            if not all(
                hit or any(
                    contains(tag_text, term.folded)
                    or (not term.title_only and _hit(term, blob, folded_blob, exclude))
                    for term in group
                )
                for hit, group in zip(title_hits, groups)
            ):
                continue
        best = max(best, _score(title_hits, groups, folded_title, phrase))
    return best


def _score(title_hits: list[bool], groups, folded_title: str, phrase: str) -> int:
    if all(title_hits):
        return 100 if contains(folded_title, phrase) else 80
    skills = _skill_terms()
    if all(hit or any(term.folded in skills for term in group)
           for hit, group in zip(title_hits, groups)):
        return 60
    return min(50, 30 + 10 * sum(title_hits))


# ── Locations ───────────────────────────────────────────────────────────────────

_NUMBERED_DISTRICT = re.compile(r"(?<!\w)(?:quan|q\.?|district)\s*(\d{1,2})(?!\d)")
# Ambiguous abbreviations are honoured after "|"/"tại" but not as a trailing word
# ("sales BD" is business development, "kế toán DN" is doanh nghiệp).
_IMPLICIT_SHORT_PLACES = frozenset({"hn", "hcm", "sg"})

# Kept frozen so identity() — and with it the SENT# alert dedup keys — does not
# change when new aliases are added to LOCATION_MAP.
_LEGACY_LOCATION_MAP = {
    "hồ chí minh": "Ho Chi Minh", "ho chi minh": "Ho Chi Minh", "hcm": "Ho Chi Minh",
    "tp.hcm": "Ho Chi Minh", "tp hcm": "Ho Chi Minh", "tphcm": "Ho Chi Minh",
    "sài gòn": "Ho Chi Minh", "saigon": "Ho Chi Minh", "quận 1": "Ho Chi Minh",
    "quận 7": "Ho Chi Minh", "thủ đức": "Ho Chi Minh", "hà nội": "Ha Noi",
    "ha noi": "Ha Noi", "hanoi": "Ha Noi", "hn": "Ha Noi", "đà nẵng": "Da Nang",
    "da nang": "Da Nang", "danang": "Da Nang", "bình dương": "Binh Duong",
    "đồng nai": "Dong Nai", "hải phòng": "Hai Phong", "cần thơ": "Can Tho",
    "bắc ninh": "Bac Ninh", "hưng yên": "Hung Yen",
}


@lru_cache(maxsize=8192)
def normalize_location(value: str) -> str:
    """Folded location text with every known alias replaced by its province."""
    from src.etl.transformer import _location_pattern

    pattern, aliases = _location_pattern()
    return pattern.sub(lambda m: clean_vn_text(aliases[m.group(1)]), clean_vn_text(value))


@lru_cache(maxsize=8192)
def _legacy_location_key(value: str) -> str:
    text = clean_vn_text(value)
    for alias, canonical in sorted(_LEGACY_LOCATION_MAP.items(), key=lambda p: -len(p[0])):
        text = re.sub(
            r"(?<!\w)" + re.escape(clean_vn_text(alias)) + r"(?!\w)", clean_vn_text(canonical), text
        )
    return text


@lru_cache(maxsize=1)
def _district_and_nationwide_patterns() -> tuple[re.Pattern, dict, re.Pattern]:
    from src.etl.transformer import DISTRICT_MAP, NATIONWIDE_TERMS

    districts = {clean_vn_text(k): (d, clean_vn_text(p)) for k, (d, p) in DISTRICT_MAP.items()}
    district_pattern = re.compile(
        r"(?<!\w)(" + "|".join(map(re.escape, sorted(districts, key=len, reverse=True)))
        + r")(?!\w)"
    )
    nationwide = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(clean_vn_text(t)) for t in NATIONWIDE_TERMS)
        + r")(?!\w)"
    )
    return district_pattern, districts, nationwide


@lru_cache(maxsize=8192)
def location_parts(value: str) -> tuple[frozenset[str], frozenset[str], bool]:
    """(provinces, districts, nationwide/remote) named in a location string."""
    from src.etl.transformer import _location_pattern

    text = clean_vn_text(value)
    district_pattern, districts_map, nationwide = _district_and_nationwide_patterns()
    provinces: set[str] = set()
    districts: set[str] = set()
    for match in _NUMBERED_DISTRICT.finditer(text):
        districts.add(f"quan {int(match.group(1))}")
        provinces.add("ho chi minh")
    for match in district_pattern.finditer(text):
        district, province = districts_map[match.group(1)]
        districts.add(district)
        provinces.add(province)
    pattern, aliases = _location_pattern()
    for match in pattern.finditer(text):
        provinces.add(clean_vn_text(aliases[match.group(1)]))
    return frozenset(provinces), frozenset(districts), bool(nationwide.search(text))


def _is_place(text: str, *, implicit: bool = False) -> bool:
    from src.etl.transformer import _location_pattern

    folded = clean_vn_text(text).strip(" ,.")
    folded = re.sub(r"^(?:tp\.?|thanh pho|tinh)\s*", "", folded)
    if not folded or (implicit and len(folded) < 4 and folded not in _IMPLICIT_SHORT_PLACES):
        return False
    pattern, _ = _location_pattern()
    district_pattern, _, _ = _district_and_nationwide_patterns()
    return any(p.fullmatch(folded) for p in (pattern, district_pattern, _NUMBERED_DISTRICT))


def location_match_level(job: dict, location: str | None) -> int:
    """0 = wrong place, 1 = possible (province-only posting for a district
    query, or a nationwide/remote posting), 2 = the requested place."""
    if not location:
        return 2
    wanted_provinces, wanted_districts, wanted_remote = location_parts(location)
    job_text = f"{job.get('location', '')} {job.get('location_normalized', '')}"
    if not wanted_provinces and not wanted_districts:
        if wanted_remote:
            return 2 if location_parts(job_text)[2] else 0
        return 2 if contains(normalize_location(job_text), normalize_location(location)) else 0
    provinces, districts, nationwide = location_parts(job_text)
    if wanted_districts:
        # A posting that names a district must name this one; a posting that
        # names only the province may still be in it.
        if districts & wanted_districts:
            return 2
        if not districts and provinces & wanted_provinces:
            return 1
    elif provinces & wanted_provinces:
        return 2
    return 1 if nationwide else 0


# ── Filters and ranking ─────────────────────────────────────────────────────────


def _posted(job: dict, now: datetime) -> datetime | None:
    posted = timestamp(job.get("posted_at"))
    # Some sources accidentally expose the closing date as the posting date.
    return posted if posted and posted <= now + timedelta(days=1) else None


def published_at(job: dict, now: datetime | None = None) -> datetime | None:
    """Best known publication time: posted_at, else first_seen_at, else scraped_at."""
    now = now or datetime.now(timezone.utc)
    return (
        _posted(job, now)
        or timestamp(job.get("first_seen_at"))
        or timestamp(job.get("scraped_at"))
    )


def matches_filters(
    job: dict,
    location: str | None = None,
    salary_min: int | None = None,
    since: datetime | None = None,
    max_age_days: int = 60,
    now: datetime | None = None,
    posted_since: datetime | None = None,
    salary_max: int | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    expiry = timestamp(job.get("expires_at"))
    if expiry and expiry <= now:
        return False
    if job.get("ttl") is not None and int(job["ttl"]) <= now.timestamp():
        return False
    # Every re-scrape refreshes scraped_at, so age is measured from the posting
    # date (or the first time we saw the job) whenever one is known.
    age_reference = published_at(job, now)
    if age_reference and age_reference < now - timedelta(days=max_age_days):
        return False
    scraped = timestamp(job.get("scraped_at"))
    if since and (not scraped or scraped < since):
        return False
    if posted_since:
        published = _posted(job, now) or timestamp(job.get("first_seen_at"))
        if not published or published < posted_since:
            return False
    if location and not location_match_level(job, location):
        return False
    salary = job.get("salary_max") or job.get("salary_min")
    if salary_min and salary is not None and int(salary) < salary_min:
        return False
    lowest = job.get("salary_min") or job.get("salary_max")
    if salary_max and lowest is not None and int(lowest) > salary_max:
        return False
    return True


def identity(job: dict) -> str:
    """Deduplicate mirrors, but never merge anonymous employers or different cities."""
    company = clean_vn_text(job.get("company", ""))
    if company and company not in {"n/a", "unknown", "ybox employer"}:
        key = "|".join(
            (
                clean_vn_text(job.get("title", "")),
                company,
                _legacy_location_key(job.get("location", "")),
            )
        )
    else:
        key = job.get("source_url") or job.get("job_id") or repr(job)
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def rank_jobs(
    jobs: list[dict],
    query: str,
    *,
    location=None,
    salary_min=None,
    since=None,
    posted_since=None,
    max_age_days=60,
    limit: int | None = 20,
    min_score: int = ALERT_MIN_SCORE,
    salary_max=None,
) -> list[dict]:
    """Rank matching jobs; ``min_score`` defaults to the alert-grade floor.

    Salary phrases in the query ("lương trên 15 triệu") become salary filters
    unless the caller passes explicit ones.
    """
    now = datetime.now(timezone.utc)
    _, query_min, query_max = extract_salary_filter(query)
    salary_min = salary_min if salary_min is not None else query_min
    salary_max = salary_max if salary_max is not None else query_max
    floor = max(1, min_score)
    ranked = []
    for job in jobs:
        if not matches_filters(
            job,
            location=location,
            salary_min=salary_min,
            since=since,
            max_age_days=max_age_days,
            now=now,
            posted_since=posted_since,
            salary_max=salary_max,
        ):
            continue
        score = relevance(job, query)
        if score >= floor:
            recent = published_at(job, now) or datetime.min.replace(tzinfo=timezone.utc)
            # Within a score, postings in the exact requested place come before
            # province-only / nationwide ones.
            tier = (score, location_match_level(job, location) if location else 2)
            ranked.append((tier, recent, job))
    ranked.sort(key=lambda r: (r[0], r[1], r[2].get("job_id", "")), reverse=True)
    tiers, seen = defaultdict(lambda: defaultdict(deque)), set()
    for tier, _, job in ranked:
        key = identity(job)
        if key not in seen:
            seen.add(key)
            tiers[tier][job.get("source", "unknown")].append(job)
    result = []
    # Diversity only breaks ties in relevance; weak hits never outrank exact hits.
    for sources in tiers.values():
        while any(sources.values()):
            for queue in sources.values():
                if queue:
                    result.append(queue.popleft())
                    if limit is not None and len(result) >= limit:
                        return result
    return result


def query_variants(query: str) -> list[str]:
    """Phrasings a board or ranker should also accept for this query.

    Seniority wording such as "AI Engineer Intern" is broadened to the related
    internship titles employers actually post, while the field is retained.
    """
    variants = [re.sub(r"\s+", " ", str(query or "")).strip()]
    cleaned = clean_vn_text(query)
    if "intern" in cleaned and re.search(r"(?<!\w)ai(?!\w)", cleaned):
        variants.extend(["ai intern", "machine learning intern", "computer vision intern"])
    return list(dict.fromkeys(item for item in variants if item))


def rank_jobs_multi(
    jobs: list[dict],
    queries: list[str],
    *,
    location=None,
    limit: int | None = 20,
    max_age_days: int = 60,
    min_score: int = INTERACTIVE_MIN_SCORE,
) -> list[dict]:
    """Rank for the primary query first, then append hits for each variant.

    Results from different providers are merged here, so an exact hit from the
    database never sits below a weak hit from a live board.  This serves
    interactive search, so description-only mentions are kept (ranked last).
    """
    ranked, seen = [], set()
    for query in queries:
        for job in rank_jobs(jobs, query, location=location, limit=None,
                             max_age_days=max_age_days, min_score=min_score):
            key = identity(job)
            if key in seen:
                continue
            seen.add(key)
            ranked.append(job)
            if limit is not None and len(ranked) >= limit:
                return ranked
    return ranked


_SEPARATED_LOCATION = re.compile(r"\s+(?:tại|ở)\s+", re.I)
_UNACCENTED_LOCATION = re.compile(r"\s+(?:tai|o)\s+", re.I)


def parse_search(text: str) -> tuple[str, str | None]:
    """Split "<keywords> | <location>" / "<keywords> tại <location>" / "<keywords> Hà Nội".

    Commas separate keywords, never a location.  The unaccented "tai"/"o" only
    separate a location when what follows is a known place ("ke toan tai chinh"
    stays one query), and a trailing city name is recognised without a separator.
    """
    text = text.strip()
    if "|" in text:
        keyword, location = text.split("|", 1)
        return keyword.strip(), location.strip() or None
    parts = _SEPARATED_LOCATION.split(text, maxsplit=1)
    if len(parts) > 1:
        return parts[0].strip(), parts[1].strip() or None
    for match in _UNACCENTED_LOCATION.finditer(text):
        rest = text[match.end():]
        if _is_place(rest):
            return text[: match.start()].strip(), rest.strip()
    words = text.split()
    for size in range(min(5, len(words) - 1), 0, -1):
        tail = " ".join(words[-size:])
        if _is_place(tail, implicit=True):
            return " ".join(words[:-size]).strip(" ,"), tail
    return text, None
