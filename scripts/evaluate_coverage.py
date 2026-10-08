#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.retriever import Retriever


def main() -> None:
    cases = json.loads((ROOT / "eval/coverage_70plus.json").read_text(encoding="utf-8"))
    retriever = Retriever(ROOT / "data/index/search.db")
    passed = 0
    durations: list[float] = []
    failures: Counter[str] = Counter()
    category_total: Counter[str] = Counter()
    category_pass: Counter[str] = Counter()

    for case in cases:
        started = time.monotonic()
        result = retriever.search(case["question"], limit=12)
        elapsed = time.monotonic() - started
        durations.append(elapsed)
        context = retriever.format_context(result, max_chars=52000)
        receipts = {hit["rcept_no"] for hit in result["hits"]}
        hit_groups = {hit["doc_group"] for hit in result["hits"]}
        hit_companies = {hit["corp_name"] for hit in result["hits"]}
        hit_years = {
            hit["base_year"] if hit["doc_group"] == "periodic" else int(hit["rcept_dt"][:4])
            for hit in result["hits"]
        }
        checks = {
            "receipt": all(x in receipts for x in case["expected_receipts"]),
            "target": all(x in result.get("target_companies", []) for x in case["expected_targets"]),
            "company_isolation": hit_companies.issubset(set(case["expected_targets"])),
            "intent": all(x in result.get("intents", []) for x in case["expected_intents"]),
            "group": all(x in hit_groups for x in case["expected_groups"]),
            "period": all(x in hit_years for x in case["expected_years"]),
            "numeric_context": bool(re.search(r"\d", context)) if case.get("require_numeric_context") else True,
        }
        ok = all(checks.values())
        category = case["category"].split(":", 1)[0]
        category_total[category] += 1
        if ok:
            passed += 1
            category_pass[category] += 1
        else:
            failed = [name for name, value in checks.items() if not value]
            failures.update(failed)
            print(
                f"FAIL {case['id']} {elapsed:.3f}s failed={failed} "
                f"expected={case['expected_receipts']} got={sorted(receipts)} q={case['question']}"
            )

    print("\nCOVERAGE_BY_CATEGORY")
    for category in category_total:
        print(f"{category}: {category_pass[category]}/{category_total[category]}")
    print(f"FAILURE_REASONS {dict(failures)}")
    print(
        f"COVERAGE_SUMMARY pass={passed}/{len(cases)} rate={passed/len(cases):.1%} "
        f"mean={statistics.mean(durations):.3f}s max={max(durations):.3f}s"
    )
    raise SystemExit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
