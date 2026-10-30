"""How to re-capture scraper fixtures (stub: this script fetches NOTHING).

tests/test_fixtures_are_fresh.py fails once a fixture in tests/fixtures/MANIFEST.json
is older than 120 days, because boards change their markup and the parser tests
would keep passing against HTML that no longer exists.

Manual procedure per source (be polite: 1 request/second, <= 5 requests per site,
the project User-Agent, no proxies, no CAPTCHA solving, stop on 403/429):

1. Run the scraper's own request once and save the raw body, e.g.
       python -c "import requests; open('tests/fixtures/<source>/<file>', 'wb')
                  .write(requests.get(URL, headers={'User-Agent': UA}, timeout=15).content)"
   Use the same URL the scraper builds (see the module docstring in
   src/scrapers/<source>_scraper.py and docs/SOURCES.md).
2. Remove personal data (emails, phone numbers) and trim huge pages if the tests
   still pass; keep at least one card with salary, one without, and a no-results page.
3. Run `pytest tests/test_<source>_scraper.py`; fix the parser if the layout changed.
4. Set "captured_on" of every re-captured file in tests/fixtures/MANIFEST.json
   to today's date (YYYY-MM-DD).

Running this script only prints the stale files and these instructions.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "MANIFEST.json"


def main() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    limit = int(data.get("max_age_days", 120))
    today = date.today()
    stale = [
        (name, (today - date.fromisoformat(info["captured_on"])).days)
        for name, info in data["fixtures"].items()
        if (today - date.fromisoformat(info["captured_on"])).days > limit
    ]
    print(__doc__)
    print(f"{len(stale)} fixture(s) older than {limit} days")
    for name, age in stale:
        print(f"  {name}: {age} days")


if __name__ == "__main__":
    main()
