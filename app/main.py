from __future__ import annotations

import logging
import os
import re
import time
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app.llm import generate_answer
from app.retriever import Retriever
from app.evidence import annual_facts, comparison_note, deterministic_financial_answer


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("mirae-agent")
DB_PATH = Path(os.getenv("SEARCH_DB", "/opt/mirae-agent/data/index/search.db"))
retriever = Retriever(DB_PATH)
app = FastAPI(title="Mirae Disclosure Agent", docs_url=None, redoc_url=None)


class AnswerResponse(BaseModel):
    question_id: str
    question: str
    retrieved_context: str
    think_trace: str
    answer: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@lru_cache(maxsize=256)
def answer_cached(question_id: str, question: str) -> AnswerResponse:
    started = time.monotonic()
    company_count = len(retriever.resolve_companies(question) or retriever.resolve_scope(question)[0])
    retrieval_limit = 7 if company_count <= 2 else min(24, company_count * 3)
    context_limit = 14000 if company_count <= 2 else min(52000, 7000 * company_count)
    result = retriever.search(question, limit=retrieval_limit)
    context = retriever.format_context(result, max_chars=context_limit)
    visible = {int(n) for n in re.findall(r'\[근거 (\d+)\] 회사=', context)}
    facts = []
    for i, hit in enumerate(result['hits'], 1):
        if i not in visible:
            continue
        for fact in annual_facts(hit, question):
            fact['evidence_index'] = i
            facts.append(fact)
    calculation = comparison_note(facts, question)
    if calculation:
        context += '\n\n' + calculation
    if not result["hits"]:
        return AnswerResponse(
            question_id=str(question_id), question=str(question),
            retrieved_context="검색된 공시 근거가 없습니다.",
            think_trace="질문에서 기업·기간·공시유형과 핵심어를 추출했으나 일치하는 근거를 찾지 못했습니다.",
            answer="제공된 공시 코퍼스에서 답변 근거를 확인할 수 없습니다.",
        )
    company_names = list(dict.fromkeys(hit["corp_name"] for hit in result["hits"]))
    companies = ", ".join(company_names) or "명시되지 않음"
    scope = result.get("scope_label") or "개별기업"
    trace = (
        f"검색범위={scope}; 대상기업={companies}; 공시유형={','.join(result['groups']) or '전체'}; "
        f"기준연도={','.join(map(str, result['years'])) or '미지정'}; "
        f"질문의도={','.join(result.get('intents', []))}; 검색어={','.join(result['terms'])}; "
        f"근거청크={len(result['hits'])}개. "
        "메타데이터로 후보를 제한하고 본문 전문검색 결과에서 정정공시와 직접 근거를 우선했습니다."
    )
    termination_jobs = [job for job in result.get('jobs', []) if job['intent'] == 'contract_termination']
    simple_termination_check = not any(
        cue in question for cue in ('정정', '최초', '최종', '변동액', '변경액')
    )
    if (
        'contract' in result.get('intents', [])
        and termination_jobs
        and not any(job['found'] for job in termination_jobs)
        and '이후' in question
        and simple_termination_check
    ):
        contract_refs = [
            i for i, hit in enumerate(result['hits'], 1)
            if i in visible and hit['doc_subtype'] == '단일판매공급계약체결'
        ]
        ref_text = f" [근거 {', '.join(map(str, contract_refs[:4]))}]" if contract_refs else ''
        years_text = ', '.join(map(str, result.get('years', []))) or '질문 기간'
        generated = (
            f"제공된 공시 코퍼스에서 {years_text}년에 체결된 주요 계약과 연결되는 "
            f"후속 계약 해지 공시는 확인되지 않았습니다.{ref_text} "
            "해지 공시일이 질문 기간에 속하더라도 원계약 체결일이 다른 연도인 건은 제외했습니다."
        )
    else:
        generated = deterministic_financial_answer(question, facts, result.get('target_companies', []))
        if not generated:
            generated = generate_answer(question, context)
    repair_reasons: list[str] = []
    if len(result.get("target_entities", [])) > 1:
        missing_companies = []
        for entity in result["target_entities"]:
            aliases = {entity["corp_name"], entity["listed_name"]}
            if not any(alias and alias in generated for alias in aliases):
                missing_companies.append(entity["listed_name"] or entity["corp_name"])
        if missing_companies:
            repair_reasons.append("대상기업 누락: " + ", ".join(missing_companies))
    numeric_cues = (
        "얼마", "금액", "매출", "영업이익", "순이익", "자산", "부채", "자본", "비율",
        "증감", "규모", "주식수", "보유비율", "배당", "투자", "조달",
    )
    if any(cue in question for cue in numeric_cues) and not re.search(r"\d", generated):
        repair_reasons.append("질문이 요구한 공시상 숫자·단위 또는 확인 불가 표시가 없음")
    if result['hits'] and not re.search(r'\[근거\s+\d+', generated):
        repair_reasons.append("각 핵심 주장과 수치 뒤에 실제 사용한 [근거 N]을 표시")
    if (
        any(cue in question for cue in ('계약금액', '투자금액', '조달액', '조달금액'))
        and re.search(r'\d[\d,]*\s*억\s*원', generated)
    ):
        repair_reasons.append("원문이 원 단위인 금액은 원 숫자를 그대로 쓰고 임의로 억원 환산하지 않음")
    if '정정' in question and any(cue in question for cue in ('최초', '최종', '변동액', '변경액')):
        requested_parts = ('최초', '최종', '변동')
        if not all(part in generated for part in requested_parts):
            repair_reasons.append("정정 전 최초 금액, 정정 후 최종 금액, 변동액을 각각 답하거나 근거 부족을 항목별로 명시")
    if '해지' in question and '해지' not in generated:
        repair_reasons.append("후속 해지 여부를 별도 항목으로 답함")
    decision_only = (
        'fundraising' in result.get('intents', [])
        and result['hits']
        and all('결정' in hit['report_nm'] for hit in result['hits'] if hit['doc_group'] == 'major')
    )
    if decision_only and re.search(r"조달하였|조달했다|실시하였|완료하였", generated):
        repair_reasons.append("근거는 결정 공시뿐이므로 실제 납입·조달 완료로 표현하지 말고 예정 내역으로 표현")
    deterministic = bool(deterministic_financial_answer(question, facts, result.get('target_companies', [])))
    deterministic = deterministic or (
        'contract' in result.get('intents', []) and termination_jobs
        and not any(job['found'] for job in termination_jobs) and '이후' in question
        and simple_termination_check
    )
    if repair_reasons and not deterministic:
        generated = generate_answer(question, context, "\n".join(repair_reasons))
    if decision_only:
        generated = re.sub(
            r"유상증자를\s*통해\s*자금을\s*조달하였음을\s*알\s*수\s*있습니다[.]?",
            "유상증자 결정 공시가 확인됩니다.",
            generated,
        )
        caution = (
            "검색된 근거는 유상증자 결정 공시입니다. 아래 금액은 예정 조달액이며, "
            "실제 납입·조달 완료 여부는 이 근거만으로 확인할 수 없습니다."
        )
        if caution not in generated:
            generated = caution + "\n\n" + generated
    # Guarantee machine-visible provenance even if the model omits the requested footer.
    if result["hits"]:
        # Replace model-written source footers with authoritative metadata.
        generated = re.split(r'(?m)^\s*(?:\*\*)?출처\s*:', generated, maxsplit=1)[0].rstrip()
        citations: list[str] = []
        cited = set()
        for group in re.findall(r'\[근거\s+([\d,;\s]+)\]', generated):
            cited.update(int(value) for value in re.findall(r'\d+', group))
        citation_indices = (cited & visible) or visible
        for index, hit in enumerate(result["hits"], 1):
            if index not in citation_indices:
                continue
            date = hit["rcept_dt"]
            if len(date) == 8:
                date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            citations.append(
                f"[근거 {index}] {hit['report_nm']} | 접수일 {date} | 접수번호 {hit['rcept_no']}"
            )
            if len(citations) == 12:
                break
        generated = generated.rstrip() + "\n\n출처: " + "; ".join(citations)
    logger.info("answered question_id=%s seconds=%.2f", question_id, time.monotonic() - started)
    return AnswerResponse(
        question_id=str(question_id), question=str(question),
        retrieved_context=str(context), think_trace=str(trace), answer=str(generated),
    )


@app.get("/answer", response_model=AnswerResponse)
def answer(
    question_id: str = Query(..., min_length=1, max_length=200),
    question: str = Query(..., min_length=1, max_length=4000),
) -> AnswerResponse:
    try:
        return answer_cached(str(question_id), str(question))
    except ValueError as exc:
        return AnswerResponse(
            question_id=str(question_id), question=str(question),
            retrieved_context="검색된 공시 근거가 없습니다.",
            think_trace=f"검색 요청을 처리하지 못했습니다: {exc}",
            answer="제공된 공시 코퍼스에서 답변 근거를 확인할 수 없습니다.",
        )
    except httpx.HTTPError as exc:
        logger.exception("HCX request failed")
        raise HTTPException(status_code=503, detail="HCX-005 temporarily unavailable") from exc
    except Exception as exc:
        logger.exception("answer failed")
        raise HTTPException(status_code=500, detail="answer generation failed") from exc
