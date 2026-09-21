"""Measure the shared search against public jobs saved by diagnose.py --sources."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.matcher.search import rank_jobs

path = Path(__file__).resolve().parents[1] / "dist/probe-jobs.json"
jobs = json.loads(path.read_text(encoding="utf-8"))
for query in ("data engineer", "kỹ sư dữ liệu", "nhân viên kế toán", "accountant"):
    started = time.perf_counter()
    matches = rank_jobs(jobs, query, limit=None)
    print(
        json.dumps(
            {
                "query": query,
                "input_jobs": len(jobs),
                "matched_jobs": len(matches),
                "sources": sorted({j["source"] for j in matches}),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            },
            ensure_ascii=True,
        )
    )
