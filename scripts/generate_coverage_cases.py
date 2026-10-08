#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data/index/search.db"
OUTPUT = ROOT / "eval/coverage_70plus.json"


def clean_report_name(value: str) -> str:
    value = re.sub(r"\[[^]]*정정[^]]*\]", "", value or "")
    value = re.sub(r"\s+", "", value)
    return value


def latest_receipt(rows: list[sqlite3.Row]) -> sqlite3.Row:
    # The corpus keeps originals and corrections together. For a fixed filing
    # family, the newest receipt is the expected effective disclosure.
    return max(rows, key=lambda row: (row["rcept_dt"], row["rcept_no"]))


def main() -> None:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    companies = db.execute(
        "SELECT corp_code, corp_name, listed_name, sector FROM companies ORDER BY rowid"
    ).fetchall()
    cases: list[dict] = []

    # One independently derived annual-report target for every company.
    for index, company in enumerate(companies, 1):
        rows = db.execute(
            """
            SELECT * FROM documents
             WHERE corp_code=? AND doc_group='periodic' AND doc_subtype='annual'
             ORDER BY base_year DESC, rcept_dt DESC
            """,
            (company["corp_code"],),
        ).fetchall()
        preferred_year = 2025 if any(row["base_year"] == 2025 for row in rows) else max(
            row["base_year"] for row in rows
        )
        same_period = [row for row in rows if row["base_year"] == preferred_year]
        # Select the latest effective filing that actually contains the requested
        # evidence. A later navigation-only PDF/HTML replacement is not a complete
        # source for financial figures.
        metric = "영업수익" if company["sector"] == "금융·보험" else "매출액"
        supported = []
        for row in same_period:
            has_metric = db.execute(
                "SELECT 1 FROM chunks WHERE doc_id=? AND text LIKE ? LIMIT 1",
                (row["doc_id"], f"%{metric}%"),
            ).fetchone()
            has_operating = db.execute(
                "SELECT 1 FROM chunks WHERE doc_id=? AND (text LIKE '%영업이익%' OR text LIKE '%영업손익%') LIMIT 1",
                (row["doc_id"],),
            ).fetchone()
            if has_metric and has_operating:
                supported.append(row)
        chosen = latest_receipt(supported or same_period)
        listed = company["listed_name"] or company["corp_name"]
        metric_phrase = "연결기준 영업수익과 영업이익" if metric == "영업수익" else "연결기준 매출액과 영업이익"
        cases.append(
            {
                "id": f"COV-P{index:02d}",
                "category": "periodic_all_companies",
                "question": f"{listed}의 {preferred_year}년 사업보고서 {metric_phrase}은 얼마인가?",
                "expected_receipts": [chosen["rcept_no"]],
                "expected_targets": [company["corp_name"]],
                "expected_intents": ["periodic"],
                "expected_groups": ["periodic"],
                "expected_years": [preferred_year],
                "require_numeric_context": True,
            }
        )

    # Representative event and ownership filings, selected from metadata rather
    # than from the retriever's output so the expected receipt is independent.
    templates = [
        ("major", "유상증자", "유상증자 결정의 조달금액과 자금 사용목적을 정리해줘"),
        ("major", "전환사채권발행", "전환사채 발행 결정의 권면총액과 자금 사용목적을 정리해줘"),
        ("major", "회사합병결정", "회사합병 결정의 합병비율과 주요 일정을 정리해줘"),
        ("major", "회사분할", "회사분할 결정의 분할 목적과 주요 일정을 정리해줘"),
        ("major", "자기주식취득결정", "자기주식 취득 결정의 주식수와 예정금액을 정리해줘"),
        ("major", "자기주식처분결정", "자기주식 처분 결정의 주식수와 예정금액을 정리해줘"),
        ("major", "무상증자결정", "무상증자 결정의 신주 수와 배정기준을 정리해줘"),
        ("major", "주식교환", "주식교환 결정의 교환비율과 주요 일정을 정리해줘"),
        ("exchange", "단일판매ㆍ공급계약체결", "공급계약의 계약금액, 상대방, 매출액 대비 비율을 정리해줘"),
        ("exchange", "신규시설투자", "신규시설투자의 투자금액, 기간, 목적을 정리해줘"),
        ("exchange", "단일판매ㆍ공급계약해지", "공급계약 해지의 해지금액과 사유를 정리해줘"),
        ("exchange", "투자판단관련주요경영사항", "투자판단 관련 주요경영사항의 핵심 수치와 내용을 정리해줘"),
        ("holding", "주식등의대량보유상황보고서", "5% 대량보유 보고의 보고자, 보유주식수와 보유비율을 정리해줘"),
    ]
    event_index = 1
    used_companies: set[tuple[str, str]] = set()
    for group, report_pattern, suffix in templates:
        rows = db.execute(
            """
            SELECT * FROM documents
             WHERE doc_group=? AND report_nm LIKE ?
             ORDER BY rcept_dt DESC
            """,
            (group, f"%{report_pattern}%"),
        ).fetchall()
        # Use up to two companies per filing family to probe naming and document diversity.
        selected: list[sqlite3.Row] = []
        for row in rows:
            key = (group + report_pattern, row["corp_code"])
            if key in used_companies:
                continue
            used_companies.add(key)
            family_rows = [
                candidate
                for candidate in rows
                if candidate["corp_code"] == row["corp_code"]
                and clean_report_name(candidate["report_nm"]) == clean_report_name(row["report_nm"])
                and candidate["rcept_dt"][:4] == row["rcept_dt"][:4]
            ]
            selected.append(latest_receipt(family_rows))
            if len(selected) == 2:
                break
        for chosen in selected:
            year = int(chosen["rcept_dt"][:4])
            listed = chosen["listed_name"] or chosen["corp_name"]
            intent = {
                "단일판매ㆍ공급계약체결": "contract",
                "신규시설투자": "facility_investment",
                "단일판매ㆍ공급계약해지": "contract_termination",
                "주식등의대량보유상황보고서": "holding",
                "유상증자": "fundraising",
                "전환사채권발행": "fundraising",
                "자기주식취득결정": "shareholder_return",
                "자기주식처분결정": "shareholder_return",
            }.get(report_pattern, "general")
            cases.append(
                {
                    "id": f"COV-E{event_index:02d}",
                    "category": f"{group}:{report_pattern}",
                    "question": f"{listed}가 {year}년에 공시한 {suffix}",
                    "expected_receipts": [chosen["rcept_no"]],
                    "expected_targets": [chosen["corp_name"]],
                    "expected_intents": [intent],
                    "expected_groups": [group],
                    "expected_years": [year],
                    "require_numeric_context": True,
                }
            )
            event_index += 1

    OUTPUT.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"generated={len(cases)} companies={len(companies)} output={OUTPUT}")


if __name__ == "__main__":
    main()
