#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.retriever import Retriever

FIELDS = ["question_id", "question", "retrieved_context", "think_trace", "answer"]


def load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_retrieval(cases: list[dict], db_path: str) -> int:
    retriever = Retriever(db_path)
    passed = 0
    durations: list[float] = []
    for case in cases:
        started = time.monotonic()
        result = retriever.search(case["question"], limit=10)
        elapsed = time.monotonic() - started
        durations.append(elapsed)
        receipts = {hit["rcept_no"] for hit in result["hits"]}
        context = retriever.format_context(result, max_chars=52000)
        missing = [x for x in case.get("expected_receipts", []) if x not in receipts]
        forbidden = [x for x in case.get("forbidden_receipts", []) if x in receipts]
        no_hits_ok = not result["hits"] if case.get("expected_no_hits") else True
        intents_ok = all(x in result.get("intents", []) for x in case.get("expected_intents", []))
        companies_ok = all(x in result.get("target_companies", []) for x in case.get("expected_targets", []))
        context_missing = [x for x in case.get("context_must_contain", []) if x not in context]
        ok = not missing and not forbidden and no_hits_ok and intents_ok and companies_ok and not context_missing
        passed += int(ok)
        print(
            f"{'PASS' if ok else 'FAIL'} {case['id']} {elapsed:.3f}s "
            f"missing={missing} forbidden={forbidden} no_hits_ok={no_hits_ok} "
            f"intents_ok={intents_ok} companies_ok={companies_ok} context_missing={context_missing}"
        )
    print(
        f"RETRIEVAL_SUMMARY pass={passed}/{len(cases)} rate={passed/len(cases):.1%} "
        f"mean={statistics.mean(durations):.3f}s max={max(durations):.3f}s"
    )
    return 0 if passed == len(cases) else 1


def call_endpoint(endpoint: str, case: dict) -> tuple[dict, float]:
    query = urllib.parse.urlencode({"question_id": case["id"], "question": case["question"]})
    started = time.monotonic()
    with urllib.request.urlopen(f"{endpoint}?{query}", timeout=300) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        data = json.loads(response.read().decode("utf-8"))
    return data, time.monotonic() - started


def evaluate_endpoint(cases: list[dict], endpoint: str) -> int:
    endpoint = endpoint.rstrip("/")
    if not endpoint.endswith("/answer"):
        endpoint += "/answer"
    passed = 0
    durations: list[float] = []
    for case in cases:
        try:
            data, elapsed = call_endpoint(endpoint, case)
            durations.append(elapsed)
            context = data.get("retrieved_context", "")
            answer = data.get("answer", "")
            checks = {
                "fields": list(data) == FIELDS,
                "strings": all(isinstance(data.get(k), str) for k in FIELDS),
                "question_echo": data.get("question_id") == case["id"] and data.get("question") == case["question"],
                "receipts": all(x in context for x in case.get("expected_receipts", [])),
                "answer_values": all(x in answer for x in case.get("answer_must_contain", [])),
                "answer_forbidden": all(x not in answer for x in case.get("answer_must_not_contain", [])),
                "citation": (
                    "확인할 수 없습니다" in answer
                    if case.get("expected_no_hits")
                    else "근거" in answer and "접수번호" in answer
                ),
                "timeout": elapsed < 300,
            }
            ok = all(checks.values())
            passed += int(ok)
            failed = [name for name, value in checks.items() if not value]
            print(f"{'PASS' if ok else 'FAIL'} {case['id']} {elapsed:.2f}s failed={failed}")
        except Exception as exc:
            print(f"FAIL {case['id']} error={type(exc).__name__}: {exc}")
    mean = statistics.mean(durations) if durations else 0
    maximum = max(durations) if durations else 0
    print(f"API_SUMMARY pass={passed}/{len(cases)} rate={passed/len(cases):.1%} mean={mean:.2f}s max={maximum:.2f}s")
    return 0 if passed == len(cases) else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=ROOT / "eval/cases.json")
    parser.add_argument("--db", default=str(ROOT / "data/index/search.db"))
    parser.add_argument("--endpoint", help="Example: http://127.0.0.1")
    parser.add_argument("--limit", type=int, help="Run only the first N cases")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if args.limit:
        cases = cases[: args.limit]
    raise SystemExit(
        evaluate_endpoint(cases, args.endpoint) if args.endpoint
        else evaluate_retrieval(cases, args.db)
    )


if __name__ == "__main__":
    main()
