"""Prebuilt search index (P3): built hourly from DynamoDB, read by search.

* :mod:`src.search_index.builder` scans the jobs table once and writes an SQLite
  FTS5 file (or, where FTS5 is unavailable, a pure-Python inverted index) to
  ``s3://<data-lake>/index/``.
* :mod:`src.search_index.reader` downloads it to ``/tmp`` when its ETag changes and
  returns a *candidate set* for a query; :func:`src.matcher.search.rank_jobs`
  stays the final ranker, so ranking semantics do not change.
"""

from src.search_index.reader import SearchIndex, reset_cache

__all__ = ["SearchIndex", "reset_cache"]
