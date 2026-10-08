#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.retriever import Retriever


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=ROOT / "eval/sector_cases.json")
    parser.add_argument("--db", default=str(ROOT / "data/index/search.db"))
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    retriever = Retriever(args.db)
    passed = 0
    for case in cases:
        started = time.monotonic()
        result = retriever.search(case["question"], limit=24)
        hit_companies = {hit["corp_name"] for hit in result["hits"]}
        checks = {
            "scope": result.get("scope_label") == case["expected_scope"],
            "target_count": len(result.get("target_companies", [])) == case["expected_target_count"],
            "groups": result.get("groups") == case["expected_groups"],
            "company_coverage": set(case["expected_hit_companies"]).issubset(hit_companies),
            "scope_header": "[검색 범위]" in retriever.format_context(result, max_chars=52000),
        }
        ok = all(checks.values())
        passed += int(ok)
        failed = [name for name, value in checks.items() if not value]
        print(
            f"{'PASS' if ok else 'FAIL'} {case['id']} {time.monotonic()-started:.3f}s "
            f"scope={result.get('scope_label')} companies={len(hit_companies)} failed={failed}"
        )
    print(f"SECTOR_SUMMARY pass={passed}/{len(cases)} rate={passed/len(cases):.1%}")
    raise SystemExit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
