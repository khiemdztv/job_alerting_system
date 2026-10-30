"""Probe every registered job source live and report what each one returns.

Usage:
    python scripts/test_scrapers_live.py ["kế toán"] [--live] [--latest] [--location "Hà Nội"] [--json]

--live tunes each scraper the way an interactive /search does (short timeout,
no retries) so the numbers reflect what the Telegram bot can actually reach.
"""
from __future__ import annotations

import argparse
import json
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


def probe_data(spec, keyword: str, live: bool, latest: bool = False, location: str | None = None):
    """Run one source and return its numbers as a dict."""
    settings = get_settings()
    scraper = spec.factory()
    if live:
        scraper.use_live_budget(
            time.monotonic() + 18, spec.live_timeout or settings.live_search_timeout_seconds
        )
    started = time.monotonic()
    mode = "search"
    try:
        if latest and scraper.supports_latest:
            mode = "latest"
            raw = scraper.scrape_latest(max_pages=1)
        elif location and scraper.supports_location:
            raw = scraper.scrape(keyword, max_pages=1, location=location)
        else:
            raw = scraper.scrape(keyword, max_pages=1)
        error = scraper.last_error
    except Exception as exc:
        raw, error = [], f"{type(exc).__name__}: {exc}"[:60]
    finally:
        scraper.session.close()
    jobs = [job.to_dynamo_item() for job in Transformer().transform_batch(raw)]
    # Newest-listing pages are not keyword-ranked, so every parsed job counts.
    relevant = jobs if mode == "latest" else rank_jobs(jobs, keyword, location=location, limit=None)
    sample = ""
    if relevant:
        first = relevant[0]
        sample = f"{first['title'][:45]} @ {first.get('company', '')[:25]} | {first.get('location', '')[:18]}"
    return {
        "source": spec.source.value, "label": spec.label, "mode": mode, "raw": len(raw),
        "relevant": len(relevant), "seconds": round(time.monotonic() - started, 1),
        "error": error or "", "sample": sample,
    }


def format_line(row: dict) -> str:
    return (
        f"{row['label']:14s} raw={row['raw']:3d} relevant={row['relevant']:3d} "
        f"t={row['seconds']:4.1f}s err={row['error'] or '-':22s} {row['sample']}"
    )


def probe(spec, keyword: str, live: bool, latest: bool = False, location: str | None = None):
    return format_line(probe_data(spec, keyword, live, latest, location))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keyword", nargs="?", default="data engineer")
    parser.add_argument("--live", action="store_true", help="use interactive live-search budget")
    parser.add_argument("--latest", action="store_true",
                        help="call scrape_latest where the source supports it")
    parser.add_argument("--location", help="location filter for sources that support one")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    args = parser.parse_args()
    specs = available_sources()
    if not args.json:
        print(f"Probing {len(specs)} sources for '{args.keyword}' (live={args.live})")
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        rows = list(pool.map(
            lambda spec: probe_data(spec, args.keyword, args.live, args.latest, args.location),
            specs,
        ))
    if args.json:
        print(json.dumps({
            "keyword": args.keyword, "live": args.live, "latest": args.latest,
            "location": args.location, "sources": rows,
        }, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(format_line(row))


if __name__ == "__main__":
    main()
