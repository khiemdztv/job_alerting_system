"""Probe every registered job source live and report what each one returns.

Usage:
    python scripts/test_scrapers_live.py ["kế toán"] [--live]

--live tunes each scraper the way an interactive /search does (short timeout,
no retries) so the numbers reflect what the Telegram bot can actually reach.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.config import get_settings  # noqa: E402
from src.etl.transformer import Transformer  # noqa: E402
from src.matcher.search import rank_jobs  # noqa: E402
from src.scrapers.registry import available_sources  # noqa: E402


def probe(spec, keyword: str, live: bool):
    settings = get_settings()
    scraper = spec.factory()
    if live:
        scraper.use_live_budget(
            time.monotonic() + 18, spec.live_timeout or settings.live_search_timeout_seconds
        )
    started = time.monotonic()
    try:
        raw = scraper.scrape(keyword, max_pages=1)
        error = scraper.last_error
    except Exception as exc:
        raw, error = [], f"{type(exc).__name__}: {exc}"[:60]
    finally:
        scraper.session.close()
    jobs = [job.to_dynamo_item() for job in Transformer().transform_batch(raw)]
    relevant = rank_jobs(jobs, keyword, limit=None)
    sample = ""
    if relevant:
        first = relevant[0]
        sample = f"{first['title'][:45]} @ {first.get('company', '')[:25]} | {first.get('location', '')[:18]}"
    return (
        f"{spec.label:14s} raw={len(raw):3d} relevant={len(relevant):3d} "
        f"t={time.monotonic() - started:4.1f}s err={error or '-':22s} {sample}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keyword", nargs="?", default="data engineer")
    parser.add_argument("--live", action="store_true", help="use interactive live-search budget")
    args = parser.parse_args()
    specs = available_sources()
    print(f"Probing {len(specs)} sources for '{args.keyword}' (live={args.live})")
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        for line in pool.map(lambda spec: probe(spec, args.keyword, args.live), specs):
            print(line)


if __name__ == "__main__":
    main()
