"""Reproducible search-quality evaluation harness for the existing matcher.

Usage (PowerShell or Git Bash, from the repository root)::

    export PYTHONUTF8=1
    .venv/Scripts/python.exe scripts/eval_search.py              # collect once, then evaluate
    .venv/Scripts/python.exe scripts/eval_search.py --refresh    # re-scrape the corpus
    .venv/Scripts/python.exe scripts/eval_search.py --corpus my_jobs.json --scale 20000
    .venv/Scripts/python.exe scripts/eval_search.py --only-probes

What it does

1. Corpus: loads jobs from ``--corpus`` (default ``dist/eval_corpus.json``).  When the
   file is missing (or ``--refresh``), it runs the project's own batch scrapers from
   ``src.scrapers.registry.batch_scrapers()`` for six keywords, one page each, waits at
   least one second between requests to the same site (the scrapers add their own 2-3 s
   delay on top), stops querying a source after its first error/block, and stores the
   ETL-transformed DynamoDB items so the ranking sees exactly what production sees.
   No AWS call is made: TopCV (You.com + Secrets Manager) is excluded.
2. Probes: deterministic checks of parse_search, location/salary/date parsing, tag
   extraction, token boundaries and identity.  These do not need the corpus.
3. Queries: 30+ Vietnamese/English queries through ``parse_search`` + ``rank_jobs``.
   For each query it prints the top 10 with relevance score, the result count, and
   heuristic flags: ``FP?`` (title does not look like the intended occupation, or wrong
   city), and suspected false negatives (corpus jobs whose *title* clearly matches the
   intent but which ``rank_jobs`` did not return).
4. Corpus diagnostics: posted_at/salary/location/tag/dedup quality per source.
5. Timing: ``rank_jobs`` on the corpus and on a replicated corpus (``--scale``), with a
   linear extrapolation to 50k/100k jobs, plus a DynamoDB full-scan cost model derived
   from the real item sizes.
6. ``--index``: builds the prebuilt search index (``src.search_index``) from the corpus
   and runs every query through the index path (candidates -> ``rank_jobs``).  Reports
   the recall of the index candidates against the full-scan ranking (share of the
   scan's top 20 and of all scan results present in the candidates; target >= 98 %),
   whether the final top 20 is identical, and per-query latency index vs scan, on the
   corpus and on the ``--scale`` replica.

The heuristics are deliberately simple regexes over accent-stripped titles; they are a
triage aid for a human reviewer, not ground truth.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
import threading
import time
import tracemalloc
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Never reach You.com / Secrets Manager from the harness.
os.environ["VIECLAMBOT_YOU_SEARCH_ENABLED"] = "false"

from src.common.text_utils import clean_vn_text  # noqa: E402
from src.etl.transformer import Transformer  # noqa: E402
from src.matcher.search import (  # noqa: E402
    concepts,
    contains,
    identity,
    normalize_location,
    parse_search,
    rank_jobs,
    relevance,
    timestamp,
)

DEFAULT_CORPUS = ROOT / "dist" / "eval_corpus.json"
CORPUS_KEYWORDS = ("kế toán", "kinh doanh", "lập trình", "marketing", "nhân sự", "logistics")
MIN_REQUEST_INTERVAL = 1.0  # seconds between two keyword requests to the same site

HCM = r"ho chi minh|hcm|sai gon|thu duc"
TITLE_ACCOUNTING = r"ke toan|accountant|accounting"

# Each query: q (what the user types), rel (regex on the accent-stripped title that a
# relevant result should match), optional loc (regex on location), text (regex on the
# whole job text), salary_min (VND), and the parse_search result a human would expect.
QUERIES: list[dict] = [
    {"q": "kế toán", "rel": TITLE_ACCOUNTING},
    {"q": "ke toan tong hop", "rel": r"ke toan tong hop|general accountant"},
    {"q": "accountant", "rel": TITLE_ACCOUNTING},
    {"q": "kế toán | HCM", "rel": TITLE_ACCOUNTING, "loc": HCM},
    {"q": "kế toán | Thủ Đức", "rel": TITLE_ACCOUNTING, "loc": r"thu duc"},
    {"q": "kế toán | BD", "rel": TITLE_ACCOUNTING, "loc": r"binh duong",
     "expect_loc_normalized": "binh duong"},
    {"q": "kế toán, kiểm toán", "rel": r"ke toan|kiem toan|accountant|audit",
     "expect_parse": ("kế toán, kiểm toán", None)},
    {"q": "ke toan tai chinh", "rel": r"ke toan|tai chinh|accountant|finance",
     "expect_parse": ("ke toan tai chinh", None)},
    {"q": "lương trên 15 triệu kế toán", "rel": TITLE_ACCOUNTING, "salary_min": 15_000_000},
    {"q": "data engineer", "rel": r"data engineer|ky su du lieu|data platform"},
    {"q": "DE", "rel": r"data engineer|ky su du lieu"},
    {"q": "IT", "rel": r"(?<!\w)it(?!\w)|cong nghe thong tin|information technology|helpdesk"},
    {"q": "nhân viên kinh doanh Hà Nội", "rel": r"kinh doanh|sales?(?!\w)|ban hang",
     "loc": r"ha noi", "expect_parse": ("nhân viên kinh doanh", "Hà Nội")},
    {"q": "sales B2B", "rel": r"sales?(?!\w)|kinh doanh|business development|b2b",
     "text": r"b2b"},
    {"q": "thực tập marketing", "rel": r"^(?=.*(thuc tap|intern))(?=.*marketing)"},
    {"q": "AI intern",
     "rel": r"^(?=.*((?<!\w)ai(?!\w)|machine learning|computer vision|deep learning))"
            r"(?=.*(intern|thuc tap))"},
    # Skill queries: a mention in the description is a legitimate hit ("skill": True).
    {"q": "java", "rel": r"(?<!\w)java(?!\w|script)", "skill": True},
    {"q": "javascript", "rel": r"javascript|(?<!\w)js(?!\w)|nodejs|node\.js|reactjs|typescript",
     "skill": True},
    {"q": "senior .net", "rel": r"\.net|dotnet", "skill": True},
    {"q": "c++", "rel": r"c\+\+", "skill": True},
    {"q": "remote python", "rel": r"python", "text": r"remote|tu xa|work from home",
     "skill": True},
    {"q": "điều dưỡng", "rel": r"dieu duong|nurse"},
    {"q": "giáo viên tiếng anh",
     "rel": r"^(?=.*(giao vien|teacher|giang vien))(?=.*(tieng anh|english))"},
    {"q": "kỹ sư xây dựng", "rel": r"xay dung|civil|ket cau|cong trinh"},
    {"q": "kỹ sư điện", "rel": r"(?<!\w)dien(?! thoai|\w)|electrical"},
    {"q": "nhân viên kho", "rel": r"(?<!\w)kho(?!\w)|warehouse"},
    {"q": "logistics Bình Dương", "rel": r"logistics|xuat nhap khau|kho|supply chain|giao nhan",
     "loc": r"binh duong", "expect_parse": ("logistics", "Bình Dương")},
    {"q": "HR", "rel": r"(?<!\w)hr(?!\w)|nhan su|human resource|c&b|tuyen dung|recruit|talent"},
    {"q": "C&B", "rel": r"c&b|c & b|compensation|tien luong|payroll"},
    {"q": "tìm việc marketing tại HCM", "rel": r"marketing", "loc": HCM},
    {"q": "marketing | Đà Nẵng", "rel": r"marketing", "loc": r"da nang"},
    {"q": "chăm sóc khách hàng",
     "rel": r"cham soc khach hang|cskh|customer (service|support|care)"},
    {"q": "BA", "rel": r"business analyst|(?<!\w)ba(?!\w)|phan tich nghiep vu"},
]


# ── Corpus ──────────────────────────────────────────────────────────────────────


def collect_corpus(path: Path, keywords: tuple[str, ...], max_pages: int = 1) -> dict:
    """Scrape one page per keyword per batch source through the project's scrapers."""
    from src.common.models import JobSource
    from src.scrapers.registry import batch_scrapers

    scrapers = [s for s in batch_scrapers() if s.source is not JobSource.TOPCV]
    transformer = Transformer()
    lock = threading.Lock()
    items: dict[str, dict] = {}
    status: dict[str, dict] = {}

    def run(scraper) -> None:
        name = scraper.source.value
        record = {"raw": 0, "errors": [], "skipped_keywords": [], "seconds": 0.0}
        started = time.monotonic()
        last_request = 0.0
        for index, keyword in enumerate(keywords):
            pause = MIN_REQUEST_INTERVAL - (time.monotonic() - last_request)
            if pause > 0:
                time.sleep(pause)
            last_request = time.monotonic()
            scraper.last_error = ""
            try:
                raw_jobs = scraper.scrape(keyword, max_pages)
            except Exception as exc:  # one broken board must not stop the corpus
                raw_jobs = []
                scraper.last_error = scraper.last_error or type(exc).__name__
            if scraper.last_error and not raw_jobs:
                record["errors"].append(f"{keyword}: {scraper.last_error}")
                record["skipped_keywords"] = list(keywords[index + 1:])
                break  # blocked or broken: do not keep hitting this site
            record["raw"] += len(raw_jobs)
            for raw in raw_jobs:
                job = transformer.transform(raw)
                if job is None:
                    continue
                item = job.to_dynamo_item()
                item["_posted_at_raw"] = raw.posted_at_raw
                item["_corpus_keyword"] = keyword
                with lock:
                    items.setdefault(item["job_id"], item)
        record["seconds"] = round(time.monotonic() - started, 1)
        try:
            scraper.session.close()
        except Exception:
            pass
        with lock:
            status[name] = record
        print(f"  [{name}] raw={record['raw']} errors={record['errors']} "
              f"{record['seconds']}s", flush=True)

    logging.disable(logging.INFO)  # scraper INFO logs would drown the report
    print(f"Collecting corpus from {len(scrapers)} sources x {len(keywords)} keywords "
          f"(max_pages={max_pages}); this takes a few minutes ...", flush=True)
    # Different sources run in parallel; requests to one site stay sequential.
    with ThreadPoolExecutor(max_workers=min(6, len(scrapers) or 1)) as pool:
        list(pool.map(run, scrapers))
    corpus = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "keywords": list(keywords),
        "max_pages": max_pages,
        "sources": status,
        "jobs": list(items.values()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(corpus, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Saved {len(corpus['jobs'])} unique jobs to {path}")
    return corpus


def retransform(jobs: list[dict]) -> list[dict]:
    """Re-run ETL on the cached items (description is the stored, truncated one)."""
    from src.common.models import JobSource, RawJob

    transformer, rebuilt = Transformer(), []
    for item in jobs:
        scraped = datetime.fromisoformat(item["scraped_at"])
        raw = RawJob(
            title=item.get("title", ""), company=item.get("company", ""),
            location=item.get("location", ""), description=item.get("description", ""),
            requirements=item.get("requirements", ""), salary_raw=item.get("salary_raw", ""),
            posted_at_raw=item.get("_posted_at_raw", ""), source=JobSource(item["source"]),
            source_url=item.get("source_url", ""), scraped_at=scraped,
        )
        job = transformer.transform(raw)
        if job is None:
            continue
        fresh = job.to_dynamo_item()
        fresh["first_seen_at"] = item["scraped_at"]
        fresh["_posted_at_raw"] = item.get("_posted_at_raw", "")
        rebuilt.append(fresh)
    return rebuilt


def load_corpus(path: Path, refresh: bool, keywords: tuple[str, ...]) -> dict:
    if refresh or not path.exists():
        return collect_corpus(path, keywords)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):  # plain list of job dicts is accepted too
        data = {"jobs": data, "sources": {}, "collected_at": "?"}
    return data


# ── Probes (deterministic, corpus-independent) ──────────────────────────────────


def _salary(text):
    return Transformer._parse_salary(text)


def _age(text, base):
    parsed = Transformer._parse_date(text, base)
    return None if parsed is None else round((base - parsed).total_seconds() / 3600, 1)


def _tags(text):
    from src.common.models import JobSource, RawJob

    return Transformer._extract_tags(RawJob(title="x", description=text, source=JobSource.ITVIEC))


def _score(title, description, query, *, company="", tagged=False):
    """relevance() of a job built exactly the way ETL builds DynamoDB items."""
    from src.common.models import JobSource, RawJob

    raw = RawJob(title=title, company=company, description=description,
                 source=JobSource.ITVIEC)
    job = Transformer().transform(raw)
    item = job.to_dynamo_item()
    if not tagged:
        item["tags"] = []
        item["search_blob"] = clean_vn_text(f"{title} {company} {description}")
    return relevance(item, query)


def probes() -> list[tuple[str, str, object, object]]:
    """(area, input, actual, expected) — expected is what a user would reasonably want."""
    base = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    m = 1_000_000
    checks = [
        ("parse_search", "kế toán, kiểm toán", parse_search("kế toán, kiểm toán"),
         ("kế toán, kiểm toán", None)),
        ("parse_search", "ke toan tai chinh", parse_search("ke toan tai chinh"),
         ("ke toan tai chinh", None)),
        ("parse_search", "logistics Bình Dương", parse_search("logistics Bình Dương"),
         ("logistics", "Bình Dương")),
        ("parse_search", "kế toán | HCM", parse_search("kế toán | HCM"), ("kế toán", "HCM")),
        ("location", "BD", normalize_location("BD"), "binh duong"),
        ("location", "ĐN", normalize_location("ĐN"), "da nang"),
        ("location", "HCMC", normalize_location("HCMC"), "ho chi minh"),
        ("location", "Thủ Đức (district kept)", normalize_location("Thủ Đức"),
         "thu duc ho chi minh"),
        ("location", "Q.1", normalize_location("Q.1"), "quan 1 ho chi minh"),
        ("location", "Vũng Tàu", normalize_location("Vũng Tàu"), "ba ria vung tau"),
        ("location", "'Ðà Nẵng' with U+00D0 (Indeed)", normalize_location("Ðà Nẵng"), "da nang"),
        ("location", "Indeed 'Hà Nội, HN'", normalize_location("Hà Nội, HN"), "ha noi"),
        ("salary", "15-20 triệu", _salary("15-20 triệu"), (15 * m, 20 * m)),
        ("salary", "15tr", _salary("15tr"), (15 * m, 15 * m)),
        ("salary", "7.5 - 10 triệu", _salary("7.5 - 10 triệu"), (7_500_000, 10 * m)),
        ("salary", "15-20 triệu + 2 tháng lương", _salary("Lương 15-20 triệu + 2 tháng lương"),
         (15 * m, 20 * m)),
        ("salary", "Up to $2,000 (attractive)", _salary("Up to $2,000 (attractive)"),
         (None, 50 * m)),
        ("salary", "$30,000/year", _salary("$30,000/year"), (62_500_000, 62_500_000)),
        ("salary", "Lên đến 20 triệu", _salary("Lên đến 20 triệu"), (None, 20 * m)),
        ("salary", "Thỏa thuận", _salary("Thỏa thuận"), (None, None)),
        ("posted_at", "3 ngày trước (hours ago)", _age("3 ngày trước", base), 72.0),
        ("posted_at", "2 tháng trước", _age("2 tháng trước", base), 1440.0),
        ("posted_at", "Đăng 2 ngày trước", _age("Đăng 2 ngày trước", base), 48.0),
        ("posted_at", "18 phút trước", _age("18 phút trước", base), 0.3),
        ("posted_at", "Hôm nay", _age("Hôm nay", base), 0.0),
        ("posted_at", "Hôm qua", _age("Hôm qua", base), 24.0),
        ("posted_at", "1 year ago", _age("1 year ago", base), 8760.0),
        ("posted_at", "5 minutes ago", _age("5 minutes ago", base), 0.1),
        ("posted_at", "30/10/2026 (VN midnight)", _age("30/10/2026", base), 19.0),
        ("tags", "javascript-only text", _tags("ReactJS, JavaScript, TypeScript"),
         ["javascript", "react", "typescript"]),
        ("tags", "trust + scalable", _tags("trustworthy, scalable systems"), []),
        ("tags", "c++ / c#", _tags("Experience with C++ and C# required"), ["c#", "c++"]),
        ("contains", "'.net' in 'asp.net core'", contains("asp.net core", ".net"), True),
        ("relevance", "IT vs 'Ít nhất 2 năm' in a sales job",
         _score("Nhân viên bán hàng", "Ít nhất 2 năm kinh nghiệm", "IT"), 0),
        ("relevance", "kỹ sư điện vs 'Kỹ sư ... đại diện'",
         _score("Kỹ sư dự toán", "Làm việc với đại diện chủ đầu tư", "kỹ sư điện"), 0),
        ("relevance", "nhân viên kho vs 'khó khăn'",
         _score("Nhân viên bán hàng", "Không ngại khó khăn", "nhân viên kho"), 0),
        ("relevance", "DE vs 'để' in description",
         _score("Nhân viên kinh doanh", "Có cơ hội để phát triển", "DE"), 0),
        ("relevance", "java vs JavaScript job (via tags)",
         _score("Frontend Developer", "JavaScript, ReactJS", "java", tagged=True), 0),
        ("relevance", "xây dựng vs accountant at a construction company",
         _score("Kế toán nội bộ", "", "xây dựng",
                company="Công ty CP Kỹ thuật Xây dựng ABC"), 0),
        ("concepts", "lương trên 15 triệu kế toán", concepts("lương trên 15 triệu kế toán"),
         (("ke toan", "accountant", "accounting"),)),
        ("identity", "same job, 2 boards (company suffix/city detail)",
         identity({"title": "Kế toán tổng hợp", "company": "Công ty TNHH ABC",
                   "location": "Quận 1, Hồ Chí Minh"})
         == identity({"title": "Kế Toán Tổng Hợp", "company": "CÔNG TY TNHH ABC VIỆT NAM",
                      "location": "Hồ Chí Minh"}), True),
    ]
    return [(area, label, actual, expected) for area, label, actual, expected in checks]


def print_probes() -> int:
    failures = 0
    print("\n=== PROBES (deterministic) ===")
    for area, label, actual, expected in probes():
        ok = actual == expected
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'} [{area}] {label!r}: got {actual!r}"
              + ("" if ok else f" (want {expected!r})"))
    print(f"  -> {failures} probe(s) fail")
    return failures


# ── Query evaluation ────────────────────────────────────────────────────────────


def _clean_loc(job: dict) -> str:
    # Indeed writes U+00D0 (Ð) for Đ; fold it so the heuristic itself is not fooled.
    text = f"{job.get('location', '')} {job.get('location_normalized', '')}"
    return clean_vn_text(text).replace("ð", "d")


def _is_relevant(job: dict, spec: dict) -> tuple[bool, str]:
    title = clean_vn_text(job.get("title", ""))
    target = f"{title} {job.get('search_blob', '')}" if spec.get("skill") else title
    if not re.search(spec["rel"], target):
        return False, "title-off-intent"
    if spec.get("loc") and not re.search(spec["loc"], _clean_loc(job)):
        return False, "wrong-location"
    if spec.get("text"):
        text = f"{job.get('search_blob', '')} {_clean_loc(job)} {title}"
        if not re.search(spec["text"], text):
            return False, "missing-attribute"
    floor = spec.get("salary_min")
    salary = job.get("salary_max") or job.get("salary_min")
    if floor and salary is not None and int(salary) < floor:
        return False, "salary-below"
    return True, ""


def _age_days(job: dict, now: datetime) -> float | None:
    posted = timestamp(job.get("posted_at")) or timestamp(job.get("scraped_at"))
    return None if posted is None else (now - posted).total_seconds() / 86400


def evaluate_query(jobs: list[dict], spec: dict, now: datetime,
                   min_score: int | None = None) -> dict:
    keyword, location = parse_search(spec["q"])
    started = time.perf_counter()
    extra = {} if min_score is None else {"min_score": min_score}
    ranked = rank_jobs(jobs, keyword, location=location, limit=None, **extra)
    elapsed_ms = (time.perf_counter() - started) * 1000
    returned = {identity(j) for j in ranked}
    top = []
    for job in ranked[:10]:
        ok, reason = _is_relevant(job, spec)
        score = relevance(job, keyword)
        if not ok and score <= 60:
            reason += "+desc-only"
        top.append({"title": job.get("title", ""), "score": score, "source": job.get("source"),
                    "location": job.get("location", ""), "flag": "" if ok else f"FP? {reason}"})
    relevant_ids, missed = set(), []
    for job in jobs:
        ok, _ = _is_relevant(job, spec)
        if not ok:
            continue
        key = identity(job)
        if key in relevant_ids:
            continue
        relevant_ids.add(key)
        if key not in returned:
            missed.append(job)
    p10 = (sum(1 for t in top if not t["flag"]) / len(top)) if top else None
    # Alerts send every hit with score > 0, so precision over *all* results matters too.
    p_all = (sum(1 for j in ranked if _is_relevant(j, spec)[0]) / len(ranked)) if ranked else None
    # What an alert would send with a "title must contain every concept" floor (score >= 80).
    strict = [j for j in ranked if relevance(j, keyword) >= 80]
    p_strict = (sum(1 for j in strict if _is_relevant(j, spec)[0]) / len(strict)) if strict else None
    strict_ids = {identity(j) for j in strict}
    recall_strict = (len(relevant_ids & strict_ids) / len(relevant_ids)) if relevant_ids else None
    recall = ((len(relevant_ids) - len(missed)) / len(relevant_ids)) if relevant_ids else None
    ages = [a for a in (_age_days(j, now) for j in ranked[:10]) if a is not None]
    result = {
        "query": spec["q"], "parsed": [keyword, location], "count": len(ranked),
        "elapsed_ms": round(elapsed_ms, 2), "p_at_10": p10, "p_all": p_all, "recall": recall,
        "count_ge80": len(strict), "p_all_ge80": p_strict, "recall_ge80": recall_strict,
        "relevant_in_corpus": len(relevant_ids), "missed": len(missed),
        "missed_examples": [
            {"title": j.get("title"), "location": j.get("location"), "source": j.get("source"),
             "why_not": _why_missed(j, keyword, location)} for j in missed[:3]],
        "top10": top, "top10_sources": len({t["source"] for t in top}),
        "top10_median_age_days": round(statistics.median(ages), 1) if ages else None,
        "parse_ok": spec.get("expect_parse") is None or
        tuple(spec["expect_parse"]) == (keyword, location),
    }
    if spec.get("expect_loc_normalized") and location:
        result["parse_ok"] = normalize_location(location) == spec["expect_loc_normalized"]
    return result


def _why_missed(job: dict, keyword: str, location: str | None) -> str:
    from src.matcher.search import matches_filters

    if not matches_filters(job, location=location, max_age_days=10_000):
        return f"location filter ({job.get('location')!r} vs {location!r})"
    if relevance(job, keyword) == 0:
        missing = [g[0] for g in concepts(keyword)
                   if not any(contains(clean_vn_text(job.get("title", "")) + " "
                                       + str(job.get("search_blob", "")), t) for t in g)]
        return f"relevance=0, unmatched concept(s) {missing}"
    return "age/expiry filter"


def print_query(result: dict) -> None:
    def pct(value):
        return "n/a" if value is None else f"{value:.0%}"

    print(f"\n--- {result['query']!r} -> parse_search={tuple(result['parsed'])}"
          f"{'' if result['parse_ok'] else '  [PARSE?]'}")
    print(f"    results={result['count']}  P@10~{pct(result['p_at_10'])}  "
          f"P@all~{pct(result['p_all'])}  recall~{pct(result['recall'])} "
          f"({result['relevant_in_corpus']} title-relevant in "
          f"corpus, {result['missed']} missed)  {result['elapsed_ms']} ms  "
          f"sources@10={result['top10_sources']}  median-age@10={result['top10_median_age_days']}d")
    for i, row in enumerate(result["top10"], 1):
        print(f"    {i:2}. [{row['score']:3}] {row['title'][:70]:70} | {str(row['source'])[:12]:12}"
              f" | {row['location'][:28]:28} {row['flag']}")
    for miss in result["missed_examples"]:
        print(f"    FN? {str(miss['title'])[:60]:60} | {miss['source']} | {miss['why_not']}")


# ── Corpus diagnostics ──────────────────────────────────────────────────────────


def _company_key(name: str) -> str:
    text = clean_vn_text(name)
    text = re.sub(r"\b(cong ty|cty|tnhh|co phan|cp|jsc|ltd|limited|company|co\.?|"
                  r"mtv|mot thanh vien|tap doan|chi nhanh|viet nam|vietnam)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _title_key(title: str) -> str:
    text = clean_vn_text(title)
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    text = re.sub(r"(luong|thu nhap|upto|up to|tu)\s.*$", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def diagnostics(jobs: list[dict]) -> dict:
    by_source = defaultdict(list)
    for job in jobs:
        by_source[job.get("source", "?")].append(job)
    print("\n=== CORPUS DIAGNOSTICS ===")
    print(f"  {len(jobs)} jobs from {len(by_source)} sources")
    print(f"  {'source':14} {'n':>4} {'posted%':>8} {'ngay/thang-as-hours':>20} "
          f"{'salary%':>8} {'salary-odd':>10} {'loc-mapped%':>11} {'multi-city':>10}")
    canonical = {"Ho Chi Minh", "Ha Noi", "Da Nang", "Binh Duong", "Dong Nai", "Hai Phong",
                 "Can Tho", "Bac Ninh", "Hung Yen"}
    report: dict = {"per_source": {}}
    odd_salaries = []
    for source, items in sorted(by_source.items()):
        n = len(items)
        posted = sum(1 for j in items if j.get("posted_at"))
        hour_bug = sum(1 for j in items
                       if re.search(r"ngay|thang|nam|day|year|ago",
                                    clean_vn_text(j.get("_posted_at_raw", "")))
                       and re.search(r"ngay|thang", clean_vn_text(j.get("_posted_at_raw", "")))
                       and not re.search(r"\d{1,2}/\d{1,2}/\d{4}", j.get("_posted_at_raw", "")))
        salary = sum(1 for j in items if j.get("salary_min") or j.get("salary_max"))
        odd = []
        for j in items:
            lo, hi = j.get("salary_min"), j.get("salary_max")
            if (lo and hi and hi < lo) or (hi and (hi > 300_000_000 or hi < 1_000_000)):
                odd.append(j)
        odd_salaries.extend(odd)
        mapped = sum(1 for j in items if j.get("location_normalized") in canonical)
        multi = sum(1 for j in items
                    if len(re.findall(r"ha noi|ho chi minh|da nang|binh duong|dong nai|hai phong",
                                      clean_vn_text(j.get("location", "")))) > 1)
        report["per_source"][source] = {"n": n, "posted": posted, "hours_bug": hour_bug,
                                        "salary": salary, "odd_salary": len(odd),
                                        "loc_mapped": mapped, "multi_city": multi}
        print(f"  {source:14} {n:4} {posted / n:8.0%} {hour_bug:20} {salary / n:8.0%} "
              f"{len(odd):10} {mapped / n:11.0%} {multi:10}")
    for j in odd_salaries[:8]:
        print(f"    odd salary: {j.get('salary_raw')!r} -> ({j.get('salary_min')}, "
              f"{j.get('salary_max')}) [{j.get('source')}]")
    raw_dates = Counter(re.sub(r"\d+", "N", j.get("_posted_at_raw", ""))[:30]
                        for j in jobs if j.get("_posted_at_raw"))
    print("  posted_at_raw shapes:", raw_dates.most_common(8))
    unmapped = Counter(j.get("location", "")[:40] for j in jobs
                       if j.get("location_normalized") not in canonical)
    print("  unmapped locations:", unmapped.most_common(12))
    # Tags polluting relevance
    js_as_java = sum(1 for j in jobs if "java" in (j.get("tags") or [])
                     and not re.search(r"(?<!\w)java(?!\w|script)", j.get("search_blob", "")))
    substr_tags = Counter(t for j in jobs for t in (j.get("tags") or [])
                          if t in {"rust", "scala", "react", "go", "ai", "git"})
    print(f"  jobs tagged 'java' whose text only says javascript: {js_as_java}; "
          f"short/substring tags seen: {dict(substr_tags)}")
    # Cross-board duplicates
    groups = defaultdict(list)
    for j in jobs:
        if _company_key(j.get("company", "")):
            groups[(_title_key(j.get("title", "")), _company_key(j.get("company", "")))].append(j)
    cross = [g for g in groups.values() if len({x.get("source") for x in g}) > 1]
    unmerged = [g for g in cross if len({identity(x) for x in g}) > 1]
    print(f"  cross-board duplicate groups (fuzzy title+company): {len(cross)}; "
          f"not merged by identity(): {len(unmerged)}")
    for g in unmerged[:4]:
        print("    e.g. " + " || ".join(f"{x.get('source')}: {x.get('title')!r} / "
                                       f"{x.get('company')!r} / {x.get('location')!r}"
                                       for x in g[:2]))
    # Accent-collision tokens in descriptions
    collisions = {
        "it (ít)": r"(?<!\w)it nhat(?!\w)",
        "dien (dien thoai/dai dien)": r"dien thoai|dai dien",
        "de (để)": r"(?<!\w)de(?!\w)",
        "kho (khó)": r"kho khan",
        "ba (ba/bà)": r"(?<!\w)ba(?!\w)",
    }
    collision_counts = {name: sum(1 for j in jobs if re.search(rx, j.get("search_blob", "")))
                        for name, rx in collisions.items()}
    print(f"  jobs whose text contains an accent-collision for a short query: "
          f"{collision_counts}")
    report.update({"cross_board_groups": len(cross), "unmerged_groups": len(unmerged),
                   "java_tag_on_js": js_as_java, "collisions": collision_counts,
                   "odd_salaries": len(odd_salaries)})
    return report


# ── Timing and scan cost model ──────────────────────────────────────────────────


def _dynamo_size(item: dict) -> int:
    """Approximate DynamoDB item size: attribute names + UTF-8 values."""
    size = 0
    for key, value in item.items():
        if key.startswith("_"):
            continue
        size += len(key.encode())
        if isinstance(value, list):
            size += 3 + sum(len(str(v).encode()) + 1 for v in value)
        elif isinstance(value, (int, float)):
            size += 21 // 2
        else:
            size += len(str(value).encode())
    return size


def replicate(jobs: list[dict], n: int) -> list[dict]:
    out = []
    for i in range(n):
        job = dict(jobs[i % len(jobs)])
        job["job_id"] = f"{job.get('job_id')}-{i}"
        job["company"] = f"{job.get('company', '')} r{i // len(jobs)}"
        out.append(job)
    return out


def timing(jobs: list[dict], scale: int) -> dict:
    print("\n=== TIMING ===")
    sample = ["kế toán", "data engineer", "nhân viên kinh doanh"]
    results: dict = {}
    for label, corpus in (("corpus", jobs), (f"x{scale}", replicate(jobs, scale))):
        per_query = []
        for q in sample:
            rank_jobs(corpus, q, limit=None)  # warm lru caches as a warm Lambda would
            runs = []
            for _ in range(3):
                started = time.perf_counter()
                rank_jobs(corpus, q, limit=None)
                runs.append(time.perf_counter() - started)
            per_query.append(min(runs))
        started = time.perf_counter()
        rank_jobs(corpus, "kế toán", location="HCM", limit=None)
        loc_s = time.perf_counter() - started
        us_per_job = statistics.mean(per_query) / len(corpus) * 1e6
        results[label] = {"jobs": len(corpus), "ms": [round(x * 1000, 1) for x in per_query],
                          "with_location_ms": round(loc_s * 1000, 1),
                          "us_per_job": round(us_per_job, 2)}
        print(f"  {label:8} jobs={len(corpus):6}  rank_jobs ms={results[label]['ms']}  "
              f"+location={results[label]['with_location_ms']} ms  "
              f"{us_per_job:.1f} us/job")
    us = results[f"x{scale}"]["us_per_job"]
    myjobs_subs = 10
    for n in (10_000, 50_000, 100_000):
        one = us * n / 1e6
        print(f"  extrapolated {n:6} jobs: /search rank {one:5.2f} s local CPU "
              f"(~{one * 2.5:5.1f} s on a 1024 MB Lambda ≈0.6 vCPU); "
              f"/myjobs with {myjobs_subs} subs ~{one * myjobs_subs * 2.5:5.0f} s")
        results[f"extrapolated_{n}_s"] = round(one, 2)
    sizes = [_dynamo_size(j) for j in jobs]
    avg = statistics.mean(sizes)
    tracemalloc.start()
    copy = json.loads(json.dumps(jobs[: min(len(jobs), 2000)], ensure_ascii=False))
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    py_bytes = current / len(copy)
    print(f"  DynamoDB item size: mean {avg / 1024:.1f} KB, p90 "
          f"{sorted(sizes)[int(len(sizes) * 0.9)] / 1024:.1f} KB; Python heap per item "
          f"~{py_bytes / 1024:.1f} KB")
    for n in (10_000, 50_000, 100_000):
        total = avg * n
        rru = total / 4096 * 0.5  # eventually consistent Scan: 0.5 RRU per 4 KB scanned
        pages = total / (1024 * 1024)
        print(f"  full Scan of {n:6} jobs: {total / 1e6:6.0f} MB read, ~{rru:8.0f} RRU "
              f"(ProjectionExpression does NOT reduce RRU), {pages:5.0f} sequential 1 MB pages "
              f"(~{pages * 0.08:4.0f}-{pages * 0.2:4.0f} s), ~${rru * 0.125 / 1e6:.4f}/scan, "
              f"Lambda heap ~{py_bytes * n / 1e6:5.0f} MB")
        results[f"scan_{n}"] = {"mb": round(total / 1e6), "rru": round(rru),
                                "pages": round(pages), "heap_mb": round(py_bytes * n / 1e6)}
    results["item_kb"] = round(avg / 1024, 2)
    return results


# ── Search index ────────────────────────────────────────────────────────────────


def index_eval(jobs: list[dict], scale: int, min_score: int | None,
               formats: tuple[str, ...]) -> dict:
    """Candidate recall and latency of the search-index path vs the full scan."""
    import tempfile

    from src.search_index.builder import build_from_items
    from src.search_index.common import fts5_available, sqlite_info
    from src.search_index.reader import DEFAULT_CANDIDATE_LIMIT, SearchIndex

    print("\n=== SEARCH INDEX ===")
    print(f"  sqlite: {sqlite_info()}")
    floors = [1, 60] if min_score is None else [min_score]
    report: dict = {"floors": floors, "candidate_limit": DEFAULT_CANDIDATE_LIMIT}
    workdir = Path(tempfile.mkdtemp(prefix="vieclambot-index-eval-"))
    corpora = (("corpus", jobs), (f"x{scale}", replicate(jobs, scale)))
    for fmt in formats:
        if fmt == "sqlite" and not fts5_available():
            print("  sqlite: FTS5 unavailable in this interpreter; skipped")
            continue
        for label, corpus in corpora:
            path = workdir / f"{label}.{fmt}"
            started = time.perf_counter()
            built = build_from_items(corpus, path, fmt=fmt)
            build_s = time.perf_counter() - started
            index = SearchIndex.open_file(path, fmt)
            rows = []
            top_hits = top_total = all_hits = all_total = same_top = 0
            for spec in QUERIES:
                keyword, location = parse_search(spec["q"])
                for floor in floors:
                    started = time.perf_counter()
                    scan = rank_jobs(corpus, keyword, location=location, limit=None,
                                     min_score=floor)
                    scan_ms = (time.perf_counter() - started) * 1000
                    started = time.perf_counter()
                    candidates = index.candidates([keyword], location,
                                                  limit=DEFAULT_CANDIDATE_LIMIT)
                    cand_ms = (time.perf_counter() - started) * 1000
                    via_index = rank_jobs(candidates, keyword, location=location, limit=20,
                                          min_score=floor)
                    total_ms = (time.perf_counter() - started) * 1000
                    ids = {job.get("job_id") for job in candidates}
                    top = [job.get("job_id") for job in scan[:20]]
                    hits_top = sum(1 for job_id in top if job_id in ids)
                    hits_all = sum(1 for job in scan if job.get("job_id") in ids)
                    top_hits += hits_top
                    top_total += len(top)
                    all_hits += hits_all
                    all_total += len(scan)
                    same = [job.get("job_id") for job in via_index] == top
                    same_top += same
                    rows.append({
                        "query": spec["q"], "floor": floor, "scan_results": len(scan),
                        "candidates": len(candidates),
                        "top20_recall": hits_top / len(top) if top else None,
                        "all_recall": hits_all / len(scan) if scan else None,
                        "same_top20": same, "scan_ms": round(scan_ms, 1),
                        "candidates_ms": round(cand_ms, 1), "index_ms": round(total_ms, 1),
                    })
            index.close()
            key = f"{fmt}:{label}"
            index_ms = [r["index_ms"] for r in rows]
            scan_ms = [r["scan_ms"] for r in rows]
            summary = {
                "jobs": built.job_count, "build_s": round(build_s, 2),
                "file_mb": round(path.stat().st_size / 1e6, 2),
                "top20_recall": top_hits / top_total if top_total else None,
                "all_recall": all_hits / all_total if all_total else None,
                "same_top20": f"{same_top}/{len(rows)}",
                "index_ms_median": round(statistics.median(index_ms), 1),
                "index_ms_p90": round(sorted(index_ms)[int(len(index_ms) * 0.9)], 1),
                "index_ms_max": round(max(index_ms), 1),
                "scan_ms_median": round(statistics.median(scan_ms), 1),
                "scan_ms_max": round(max(scan_ms), 1),
                "candidates_median": statistics.median(r["candidates"] for r in rows),
                "queries": rows,
            }
            report[key] = summary
            print(f"  {key:16} jobs={summary['jobs']:6} build={summary['build_s']:5.2f}s "
                  f"file={summary['file_mb']:6.2f}MB  top20 recall="
                  f"{summary['top20_recall']:.2%}  all recall={summary['all_recall']:.2%}  "
                  f"same top20={summary['same_top20']}")
            print(f"  {'':16} index ms median/p90/max={summary['index_ms_median']}/"
                  f"{summary['index_ms_p90']}/{summary['index_ms_max']}  scan ms "
                  f"median/max={summary['scan_ms_median']}/{summary['scan_ms_max']}  "
                  f"candidates median={summary['candidates_median']}")
            for row in rows:
                if row["top20_recall"] is not None and row["top20_recall"] < 1:
                    print(f"    recall<100%: {row['query']!r} floor={row['floor']} "
                          f"top20={row['top20_recall']:.0%}")
                elif not row["same_top20"]:
                    print(f"    top20 differs: {row['query']!r} floor={row['floor']}")
    return report


# ── Main ────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--refresh", action="store_true", help="re-scrape the corpus")
    parser.add_argument("--keywords", default=",".join(CORPUS_KEYWORDS))
    parser.add_argument("--scale", type=int, default=10_000, help="replicated corpus size")
    parser.add_argument("--only-probes", action="store_true")
    parser.add_argument("--json", type=Path, default=ROOT / "dist" / "eval_report.json")
    parser.add_argument("--quiet", action="store_true", help="summary table only")
    parser.add_argument("--min-score", type=int, default=None,
                        help="rank_jobs floor (default: rank_jobs' own default, the alert "
                             "floor; 1 = interactive /search)")
    parser.add_argument("--retransform", action="store_true",
                        help="rebuild every cached item through the current Transformer "
                             "(what the table holds after the next ETL run)")
    parser.add_argument("--index", action="store_true",
                        help="also evaluate the prebuilt search index path (recall/latency)")
    parser.add_argument("--index-formats", default="sqlite,pickle",
                        help="index formats to evaluate with --index")
    args = parser.parse_args()

    probe_failures = print_probes()
    if args.only_probes:
        return 0
    keywords = tuple(k.strip() for k in args.keywords.split(",") if k.strip())
    corpus = load_corpus(args.corpus, args.refresh, keywords)
    jobs = corpus["jobs"]
    if args.retransform:
        jobs = retransform(jobs)
    if not jobs:
        print("Corpus is empty; nothing to evaluate.")
        return 1
    print(f"\nCorpus: {len(jobs)} jobs collected {corpus.get('collected_at')} "
          f"sources={ {k: v.get('raw') for k, v in corpus.get('sources', {}).items()} }")
    now = datetime.now(timezone.utc)
    results = [evaluate_query(jobs, spec, now, args.min_score) for spec in QUERIES]
    if not args.quiet:
        print("\n=== QUERIES ===")
        for result in results:
            print_query(result)
    print("\n=== SUMMARY ===")
    print(f"  {'query':32} {'n':>4} {'P@10':>5} {'P@all':>6} {'recall':>6} {'missed':>6} "
          f"{'parse':>5} {'ms':>6} | {'n>=80':>5} {'P>=80':>6} {'R>=80':>6}")
    for r in results:
        p = "-" if r["p_at_10"] is None else f"{r['p_at_10']:.0%}"
        pa = "-" if r["p_all"] is None else f"{r['p_all']:.0%}"
        rc = "-" if r["recall"] is None else f"{r['recall']:.0%}"
        ps = "-" if r["p_all_ge80"] is None else f"{r['p_all_ge80']:.0%}"
        rs = "-" if r["recall_ge80"] is None else f"{r['recall_ge80']:.0%}"
        print(f"  {r['query'][:32]:32} {r['count']:4} {p:>5} {pa:>6} {rc:>6} {r['missed']:6} "
              f"{'ok' if r['parse_ok'] else 'BAD':>5} {r['elapsed_ms']:6.0f} | "
              f"{r['count_ge80']:5} {ps:>6} {rs:>6}")
    scored = [r for r in results if r["p_at_10"] is not None]
    recalled = [r for r in results if r["recall"] is not None]
    if scored:
        print(f"  mean P@10~{statistics.mean(r['p_at_10'] for r in scored):.0%} over "
              f"{len(scored)} queries; mean P@all~"
              f"{statistics.mean(r['p_all'] for r in scored):.0%} "
              f"(score>=80 floor: P@all~"
              f"{statistics.mean(r['p_all_ge80'] for r in results if r['p_all_ge80'] is not None):.0%}"
              f", recall~"
              f"{statistics.mean(r['recall_ge80'] for r in results if r['recall_ge80'] is not None):.0%}"
              f"); mean recall~"
              f"{statistics.mean(r['recall'] for r in recalled):.0%} over {len(recalled)}; "
              f"zero-result queries: {sum(1 for r in results if not r['count'])}; "
              f"parse issues: {sum(1 for r in results if not r['parse_ok'])}")
    diag = diagnostics(jobs)
    timings = timing(jobs, args.scale)
    report = {
        "generated_at": now.isoformat(), "corpus_jobs": len(jobs),
        "probe_failures": probe_failures, "queries": results,
        "diagnostics": diag, "timing": timings,
    }
    if args.index:
        formats = tuple(f.strip() for f in args.index_formats.split(",") if f.strip())
        report["search_index"] = index_eval(jobs, args.scale, args.min_score, formats)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                         encoding="utf-8")
    print(f"\nJSON report: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
