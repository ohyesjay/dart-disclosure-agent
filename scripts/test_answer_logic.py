#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import annual_facts, deterministic_financial_answer
from app.retriever import Retriever


FINANCIAL_CASES = [
    (
        "A01-local",
        "삼성전자와 현대차의 2025년 사업보고서 연결 매출액을 비교하고 차이와 격차율을 계산해줘",
        ("333,605,938,000,000원", "186,254,472,000,000원", "147,351,466,000,000원", "79.11%"),
        (),
    ),
    (
        "A02-local",
        "2차전지 기업들의 2025년 사업보고서 연결 영업이익을 기업별로 비교해줘",
        ("1,346,120,000,000원", "-1,722,360,788,760원", "143,309,899,725원"),
        ("134,612,000,000원",),
    ),
    (
        "E02-local",
        "SK하이닉스의 2024년 반기보고서 연결 영업이익은?",
        ("2024년 반기 누적", "8,354,565,000,000원", "8,354,565백만원"),
        ("5,468,536,000,000원",),
    ),
    (
        "A07-local",
        "SK하이닉스의 2024년 2분기 연결 영업이익은 얼마인가?",
        ("2024년 2분기(3개월)", "5,468,536,000,000원", "5,468,536백만원"),
        ("8,354,565,000,000원",),
    ),
    (
        "Q01-mixed-period-local",
        "SK하이닉스의 2024년 2분기 3개월 연결 영업이익과 반기 누적 연결 영업이익을 구분해 제시하고, 두 금액의 차이를 계산해줘.",
        ("5,468,536,000,000원", "8,354,565,000,000원", "2,886,029,000,000원"),
        (),
    ),
    (
        "Q02-growth-local",
        "SK하이닉스의 2024년과 2025년 사업보고서 연결 영업이익을 비교하고 증감액과 증가율을 계산해줘.",
        ("23,467,319,000,000원", "47,206,319,000,000원", "23,739,000,000,000원", "101.16%"),
        ("101.51%",),
    ),
    (
        "Q08-margin-local",
        "삼성전자, 현대자동차, 삼성바이오로직스의 2025년 사업보고서 연결 매출액과 영업이익을 이용해 영업이익률을 계산하고 높은 순서로 비교해줘.",
        ("삼성바이오로직스", "45.41%", "삼성전자", "13.07%", "현대자동차", "6.16%"),
        ("45.28%",),
    ),
]


def main() -> None:
    retriever = Retriever(ROOT / "data/index/search.db")
    failures = []
    for case_id, question, required, forbidden in FINANCIAL_CASES:
        result = retriever.search(question, limit=24)
        context = retriever.format_context(result, max_chars=52000)
        visible = {
            int(value) for value in __import__('re').findall(r'\[근거 (\d+)\] 회사=', context)
        }
        facts = []
        for index, hit in enumerate(result['hits'], 1):
            if index not in visible:
                continue
            for fact in annual_facts(hit, question):
                fact['evidence_index'] = index
                facts.append(fact)
        answer = deterministic_financial_answer(question, facts, result['target_companies'])
        missing = [value for value in required if value not in answer]
        present = [value for value in forbidden if value in answer]
        ok = not missing and not present
        print(f"{'PASS' if ok else 'FAIL'} {case_id} missing={missing} forbidden_present={present}")
        if not ok:
            failures.append(case_id)
            print(answer)

    question = "효성중공업이 2025년에 체결한 주요 계약 중 이후 해지 공시가 나온 계약이 있는지 확인해줘"
    result = retriever.search(question, limit=7)
    termination_jobs = [job for job in result['jobs'] if job['intent'] == 'contract_termination']
    forbidden_receipts = {'20250429800893', '20250508800712'}
    returned_receipts = {hit['rcept_no'] for hit in result['hits']}
    a05_ok = bool(termination_jobs) and not any(job['found'] for job in termination_jobs)
    a05_ok = a05_ok and not (returned_receipts & forbidden_receipts)
    print(f"{'PASS' if a05_ok else 'FAIL'} A05-local termination_jobs={termination_jobs} forbidden_present={sorted(returned_receipts & forbidden_receipts)}")
    if not a05_ok:
        failures.append('A05-local')
    if failures:
        raise SystemExit("ANSWER_LOGIC_FAILURES=" + ",".join(failures))
    print(f"ANSWER_LOGIC_SUMMARY pass={len(FINANCIAL_CASES) + 1}/{len(FINANCIAL_CASES) + 1}")


if __name__ == "__main__":
    main()
