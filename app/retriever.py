from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from app.evidence import annual_facts, fact_note


TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9·ㆍ&.()-]*")
YEAR_RE = re.compile(r"(?:19|20)\d{2}")
DATE_KO_RE = re.compile(r"((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
CORRECTION_ORIGINAL_DATE_RE = re.compile(
    r"최초제출일[|:\s]*((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"
)
CONTRACT_ORIGINAL_DATE_RES = (
    re.compile(r"관련공시\s*[|:]\s*((?:19|20)\d{2})-\d{1,2}-\d{1,2}\s*단일판매[ㆍ·]?공급계약체결"),
    re.compile(r"본\s*건은\s*((?:19|20)\d{2})년\s*\d{1,2}월\s*\d{1,2}일[^.]{0,180}?계약을\s*체결"),
    re.compile(r"본\s*건은\s*((?:19|20)\d{2})년\s*\d{1,2}월\s*\d{1,2}일[^.]{0,180}?단일판매[ㆍ·]?\s*공급계약\s*체결"),
)
STOPWORDS = {
    "알려줘", "알려주세요", "설명해줘", "설명해주세요", "무엇인가", "무엇인지", "얼마인가",
    "얼마인지", "어떻게", "어느", "대한", "관련", "기준", "공시", "회사의", "회사는",
    "기업의", "해당", "그리고", "또는", "각각", "비교", "분석", "질문", "답변",
    "사업보고서", "반기보고서", "분기보고서", "최근", "대비", "비율", "체결한",
    "얼마인", "얼마", "년도", "연도", "보고한", "비교해줘",
    "기업", "기업들", "기업들의", "기업별", "회사", "회사들", "회사들의", "업체", "업체들",
    "제약사", "제약사들", "제약사들의", "금융사", "금융사들", "금융지주기업", "금융지주기업들",
    "들의", "함께", "관통해서", "포괄해서", "전체적으로",
}
PARTICLES = (
    "으로부터", "에서의", "에게서", "으로써", "으로서", "까지의", "부터의",
    "에서는", "에게는", "이라는", "라고", "이라", "으로", "에서", "에게", "께서",
    "부터", "까지", "보다", "처럼", "만큼", "하고", "이며", "이고", "에는", "에도",
    "은", "는", "이", "가", "을", "를", "의", "와", "과", "로", "에", "도", "만",
)


@dataclass
class Hit:
    doc_id: str
    corp_code: str
    corp_name: str
    listed_name: str
    stock_code: str
    report_nm: str
    rcept_no: str
    rcept_dt: str
    doc_group: str
    doc_subtype: str
    is_correction: bool
    base_year: int | None
    base_month: int | None
    source_file: str
    chunk_no: int
    text: str
    score: float


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value or "")


def strip_particle(token: str) -> str:
    for suffix in PARTICLES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


class Retriever:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        with self.connect() as db:
            companies = db.execute(
                "SELECT corp_code, stock_code, corp_name, listed_name, corp_eng_name, sector FROM companies"
            ).fetchall()
        self.company_by_code = {row[0]: row for row in companies}
        self.sector_to_codes: dict[str, list[str]] = {}
        self.name_to_code: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for row in companies:
            corp_code, stock_code, corp_name, listed_name, eng_name, sector = row
            self.sector_to_codes.setdefault(nfc(sector or ""), []).append(corp_code)
            for name in (corp_name, listed_name):
                if name:
                    self.name_to_code[nfc(name).casefold()] = corp_code
            for alias in (corp_name, listed_name, stock_code, eng_name):
                alias = nfc(alias or "").strip()
                if len(alias) >= 2:
                    aliases[alias.casefold()] = corp_code
            # Common legal-name suffixes sometimes appear in questions.
            for name in (corp_name, listed_name):
                if name:
                    aliases[(name + "주식회사").casefold()] = corp_code
        self.aliases = sorted(aliases.items(), key=lambda x: len(x[0]), reverse=True)
        self.alias_to_code = dict(self.aliases)
        # Contest corpus taxonomy plus common investor vocabulary.
        self.scope_aliases: list[tuple[str, str, list[str]]] = []
        sector_aliases = {
            "바이오·제약": ("바이오·제약", "바이오제약", "바이오 기업", "바이오기업", "제약사", "제약 기업"),
            "전력기기": ("전력기기", "전력 기업", "전력기업", "변압기 기업", "변압기기업"),
            "2차전지": ("2차전지", "이차전지", "배터리 기업", "배터리기업"),
            "금융·보험": ("금융·보험", "금융보험", "금융 기업", "금융기업", "금융사"),
            "반도체·전자부품": ("반도체·전자부품", "반도체 기업", "반도체기업", "전자부품 기업"),
            "방산·항공우주": ("방산·항공우주", "방산 기업", "방산기업", "항공우주 기업"),
            "AI소프트웨어·플랫폼": ("AI소프트웨어", "AI 소프트웨어", "플랫폼 기업", "플랫폼기업"),
            "자동차·모빌리티": ("자동차·모빌리티", "자동차 기업", "자동차기업", "모빌리티 기업"),
            "신재생에너지": ("신재생에너지", "태양광 기업", "수소 기업"),
            "원전": ("원전 기업", "원전기업"),
            "조선": ("조선 기업", "조선사", "조선업체"),
            "철강": ("철강 기업", "철강사"),
            "통신": ("통신 기업", "통신사"),
            "게임": ("게임 기업", "게임사"),
            "건설": ("건설 기업", "건설사"),
            "엔터테인먼트": ("엔터테인먼트 기업", "엔터사"),
            "소비재·유통": ("소비재·유통", "소비재 기업", "유통 기업", "유통사"),
            "운송·물류": ("운송·물류", "물류 기업", "물류기업"),
            "로봇": ("로봇 기업", "로봇기업"),
            "비철금속": ("비철금속 기업", "비철금속기업"),
        }
        for sector, scope_names in sector_aliases.items():
            codes = self.sector_to_codes.get(sector, [])
            for alias in scope_names:
                self.scope_aliases.append((nfc(alias).casefold(), sector, codes))

        # A financial-holding question should not be diluted by insurers or brokers.
        holding_names = ["KB금융", "메리츠금융지주", "신한지주", "우리금융지주", "하나금융지주"]
        holding_codes = [self.name_to_code[n.casefold()] for n in holding_names if n.casefold() in self.name_to_code]
        for alias in ("금융지주", "금융지주사", "금융 지주"):
            self.scope_aliases.append((alias.casefold(), "금융지주", holding_codes))
        self.scope_aliases.sort(key=lambda x: len(x[0]), reverse=True)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    @lru_cache(maxsize=8192)
    def correction_original_date(self, doc_id: str) -> str | None:
        with self.connect() as db:
            rows = db.execute(
                "SELECT text FROM chunks WHERE doc_id=? ORDER BY chunk_no LIMIT 4", (doc_id,)
            ).fetchall()
        text = "\n".join(row[0] for row in rows)
        match = CORRECTION_ORIGINAL_DATE_RE.search(text)
        if not match:
            return None
        year, month, day = match.groups()
        return f"{int(year):04d}{int(month):02d}{int(day):02d}"

    @lru_cache(maxsize=8192)
    def contract_original_year(self, doc_id: str) -> int | None:
        """Read the linked original signing year from a termination filing."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT text FROM chunks WHERE doc_id=? ORDER BY chunk_no LIMIT 5", (doc_id,)
            ).fetchall()
        text = "\n".join(row[0] for row in rows)
        for pattern in CONTRACT_ORIGINAL_DATE_RES:
            match = pattern.search(text)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def report_family(report_nm: str) -> str:
        value = re.sub(r"\[[^\]]*정정[^\]]*\]", "", nfc(report_nm))
        value = re.sub(r"\s+", "", value)
        return value

    def keep_latest_corrections(self, hits: list[Hit]) -> list[Hit]:
        """Treat a correction as a replacement for the original disclosure."""
        latest_doc: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        doc_key: dict[str, tuple[str, str, str]] = {}
        for hit in hits:
            # A periodic report has exactly one filing chain per company, fiscal
            # period and report subtype. Some PDF/HTML replacement corrections do
            # not expose the "최초제출일" label, so use the fiscal-period metadata
            # as the stable chain key and keep the newest included correction.
            if hit.doc_group == "periodic" and hit.base_year and hit.base_month:
                anchor_date = f"period:{hit.base_year}:{hit.base_month:02d}"
            else:
                original_date = self.correction_original_date(hit.doc_id) if hit.is_correction else None
                anchor_date = original_date or hit.rcept_dt
            key = (hit.corp_code, self.report_family(hit.report_nm), anchor_date)
            doc_key[hit.doc_id] = key
            current = latest_doc.get(key)
            if current is None or (hit.rcept_dt, hit.rcept_no) > current[:2]:
                latest_doc[key] = (hit.rcept_dt, hit.rcept_no, hit.doc_id)
        keep_ids = {doc_id for _date, _receipt, doc_id in latest_doc.values()}
        return [hit for hit in hits if hit.doc_id in keep_ids]

    def resolve_companies(self, question: str) -> list[str]:
        folded = nfc(question).casefold()
        found: list[tuple[int, str]] = []
        occupied: list[tuple[int, int]] = []
        for alias, code in self.aliases:
            start = folded.find(alias)
            if start < 0:
                continue
            span = (start, start + len(alias))
            if any(not (span[1] <= x[0] or span[0] >= x[1]) for x in occupied):
                continue
            occupied.append(span)
            found.append((start, code))
        return list(dict.fromkeys(code for _, code in sorted(found)))

    def resolve_scope(self, question: str) -> tuple[list[str], str | None, str | None]:
        folded = nfc(question).casefold()
        for alias, label, codes in self.scope_aliases:
            if alias in folded:
                return list(codes), label, alias
        return [], None, None

    def infer_groups(self, question: str) -> list[str]:
        q = nfc(question)
        groups: list[str] = []
        if any(x in q for x in ("사업보고서", "분기보고서", "반기보고서", "재무제표", "매출액", "영업이익", "당기순이익", "자산", "부채")):
            groups.append("periodic")
        if any(x in q for x in ("판매", "공급계약", "계약금액", "수주", "신규시설투자", "시설투자", "증설", "투자판단", "계약해지", "라이선스", "기술이전")):
            groups.append("exchange")
        if any(x in q for x in ("대량보유", "보유비율", "보유주식", "5%", "지분변동", "보고자")):
            groups.append("holding")
        if any(x in q for x in ("주요사항보고서", "자기주식", "자사주", "주주환원", "소각", "유상증자", "유증", "무상증자", "합병", "분할", "영업양수", "영업양도", "전환사채")):
            groups.append("major")
        if any(x in q for x in ("배당", "주주환원", "주당배당금", "배당성향")):
            groups.append("periodic")
        return list(dict.fromkeys(groups))

    def infer_intents(self, question: str) -> list[dict]:
        """Create deterministic retrieval jobs from the evaluator's question types."""
        q = nfc(question)
        intents: list[dict] = []

        def add(name: str, groups: list[str], terms: list[str], subtypes: list[str] | None = None) -> None:
            if not any(item["name"] == name for item in intents):
                intents.append({"name": name, "groups": groups, "terms": terms, "subtypes": subtypes or []})

        contract_words = ("수주", "공급계약", "판매계약", "계약금액", "주요 계약", "빅파마", "빅테크", "라이선스", "기술이전")
        if any(x in q for x in contract_words):
            # Bio licensing agreements can be filed as investment-judgement disclosures,
            # so do not force the supply-contract subtype for broad contract questions.
            subtype = [] if any(x in q for x in ("빅파마", "라이선스", "기술이전")) else ["단일판매공급계약체결"]
            add(
                "contract",
                ["exchange"],
                ["계약금액", "계약상대방", "계약기간", "매출액대비", "단일판매", "공급계약", "라이선스"],
                subtype,
            )
        if any(x in q for x in ("계약해지", "해지된 계약", "해지 여부", "해지")):
            add(
                "contract_termination",
                ["exchange"],
                ["계약해지", "해지금액", "해지사유", "계약상대방"],
                ["단일판매공급계약해지"],
            )
        if any(x in q for x in ("설비투자", "시설투자", "신규시설", "증설", "생산능력", "CAPA", "투자 계획", "투자계획")):
            # Open questions about investment plans may be answered by both a formal
            # facilities disclosure and an investment-judgement disclosure.
            add(
                "facility_investment",
                ["exchange"],
                ["신규시설투자", "시설투자", "투자금액", "투자기간", "투자목적", "생산시설", "생산능력", "공장"],
            )
        if any(x in q for x in ("자금조달", "유상증자", "유증", "전환사채", "CB", "신주인수권부사채", "BW", "교환사채", "EB")):
            add(
                "fundraising",
                ["major"],
                ["유상증자", "전환사채", "신주인수권부사채", "교환사채", "조달금액", "자금조달", "자금사용목적", "신주"],
            )
        if any(x in q for x in ("배당", "주당배당금", "배당성향")):
            add(
                "dividend",
                ["periodic"],
                ["현금배당", "주당배당금", "배당금총액", "배당성향", "배당수익률"],
            )
        if any(x in q for x in ("주주환원", "자기주식", "자사주", "소각")):
            add(
                "shareholder_return",
                ["major", "periodic"],
                ["자기주식", "자사주", "취득예정주식", "취득예정금액", "소각", "주주환원"],
            )
        if any(x in q for x in ("대량보유", "보유비율", "보유주식", "5%", "지분변동", "보고자")):
            add(
                "holding",
                ["holding"],
                ["보유비율", "보유주식", "대량보유", "변동", "보고자"],
            )

        periodic_words = (
            "사업보고서", "반기보고서", "분기보고서", "재무제표", "매출액", "영업이익",
            "당기순이익", "자산총계", "부채총계", "자본총계", "핵심 사업", "사업 변화",
        )
        # Contract disclosures already contain the recent-sales denominator and
        # ratio. Avoid pulling a second periodic report unless the question clearly
        # requests a reporting-period financial metric.
        explicit_periodic = any(x in q for x in ("사업보고서", "반기보고서", "분기보고서", "재무제표"))
        financial_without_event = any(x in q for x in periodic_words) and not any(
            item["name"] in {"contract", "facility_investment", "fundraising"} for item in intents
        )
        if explicit_periodic or financial_without_event:
            add(
                "periodic",
                ["periodic"],
                ["매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계", "사업의 내용", "사업부문", "주요 제품"],
            )

        if not intents:
            groups = self.infer_groups(q)
            add("general", groups, [])
        return intents

    def search_terms(self, question: str, company_codes: list[str], scope_alias: str | None = None) -> list[str]:
        text = nfc(question)
        for code in company_codes:
            row = self.company_by_code[code]
            for alias in (row[1], row[2], row[3], row[4]):
                if alias:
                    text = re.sub(re.escape(nfc(alias)), " ", text, flags=re.IGNORECASE)
        if scope_alias:
            text = re.sub(re.escape(scope_alias), " ", text, flags=re.IGNORECASE)
        terms: list[str] = []
        for raw in TOKEN_RE.findall(text):
            term = strip_particle(raw.strip(".()"))
            term = re.sub(r"((?:19|20)\d{2})년$", r"\1", term)
            if len(term) < 2 or term in STOPWORDS or YEAR_RE.fullmatch(term):
                continue
            terms.append(term)
        # Add high-value synonyms used verbatim in filings.
        q = nfc(question)
        if "수주" in q:
            terms.extend(["계약금액", "공급계약"])
        if "빅파마" in q:
            terms.extend(["글로벌 제약", "기술이전", "라이선스", "계약상대방", "계약금액"])
        if "빅테크" in q:
            terms.extend(["데이터센터", "북미", "변압기", "계약상대방", "계약금액"])
        if "증설" in q:
            terms.extend(["신규시설투자", "시설투자", "투자금액", "투자목적", "생산시설", "생산능력", "공장"])
        if "유증" in q:
            terms.extend(["유상증자", "신주", "조달금액", "자금조달"])
        if "배당" in q:
            terms.extend(["현금배당", "주당배당금", "배당금총액", "배당성향"])
        if "주주환원" in q:
            terms.extend(["자기주식", "자사주", "소각", "현금배당"])
        if "지분율" in q:
            terms.append("보유비율")
        if "매출 대비" in q or "매출대비" in q:
            terms.append("매출액대비")
        return list(dict.fromkeys(terms))[:12]

    @staticmethod
    def fts_expression(terms: list[str]) -> str:
        safe = [t.replace('"', '""') for t in terms if t]
        return " OR ".join(f'text:"{t}"' for t in safe)

    @staticmethod
    def infer_subtype(question: str) -> tuple[str | None, int | None]:
        q = nfc(question)
        if "사업보고서" in q or "연간보고서" in q:
            return "annual", 12
        if "반기보고서" in q or "반기" in q:
            return "half", 6
        if "1분기" in q:
            return "quarter", 3
        if "2분기" in q:
            return "half", 6
        if "3분기" in q:
            return "quarter", 9
        if "4분기" in q:
            return "annual", 12
        if "분기보고서" in q:
            return "quarter", None
        return None, None

    def _search_once(
        self,
        expression: str,
        company_code: str | None,
        groups: list[str],
        years: list[int],
        subtypes: list[str] | None,
        base_month: int | None,
        limit: int,
    ) -> list[Hit]:
        where = ["chunks_fts MATCH ?"]
        params: list[object] = [expression]
        if company_code:
            where.append("d.corp_code = ?")
            params.append(company_code)
        if groups:
            where.append("d.doc_group IN (%s)" % ",".join("?" for _ in groups))
            params.extend(groups)
        # Fiscal period filtering applies only to periodic reports.
        if years and groups == ["periodic"]:
            where.append("d.base_year IN (%s)" % ",".join("?" for _ in years))
            params.extend(years)
        elif years and ("periodic" in groups or not groups):
            placeholders = ",".join("?" for _ in years)
            where.append(
                f"((d.doc_group='periodic' AND d.base_year IN ({placeholders})) "
                f"OR (d.doc_group!='periodic' AND substr(d.rcept_dt, 1, 4) IN ({placeholders})))"
            )
            params.extend(years)
            params.extend(str(year) for year in years)
        elif years and "periodic" not in groups:
            where.append("substr(d.rcept_dt, 1, 4) IN (%s)" % ",".join("?" for _ in years))
            params.extend(str(year) for year in years)
        if subtypes and len(groups) == 1:
            where.append("d.doc_subtype IN (%s)" % ",".join("?" for _ in subtypes))
            params.extend(subtypes)
        if base_month and groups == ["periodic"]:
            where.append("d.base_month = ?")
            params.append(base_month)
        sql = f"""
            SELECT d.doc_id, d.corp_code, d.corp_name, d.listed_name, d.stock_code,
                   d.report_nm, d.rcept_no, d.rcept_dt, d.doc_group,
                   coalesce(d.doc_subtype, '') AS doc_subtype, d.is_correction,
                   d.base_year, d.base_month, c.source_file, c.chunk_no, c.text,
                   bm25(chunks_fts, 1.0, 0.0)
                     - CASE WHEN d.is_correction=1 THEN 2.5 ELSE 0 END AS score
              FROM chunks_fts
              JOIN chunks c ON c.chunk_id=chunks_fts.rowid
              JOIN documents d ON d.doc_id=c.doc_id
             WHERE {' AND '.join(where)}
             ORDER BY score ASC, d.rcept_dt DESC
             LIMIT ?
        """
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [
            Hit(
                doc_id=r[0], corp_code=r[1], corp_name=r[2], listed_name=r[3], stock_code=r[4],
                report_nm=r[5], rcept_no=r[6], rcept_dt=r[7], doc_group=r[8], doc_subtype=r[9],
                is_correction=bool(r[10]), base_year=r[11], base_month=r[12], source_file=r[13],
                chunk_no=r[14], text=r[15], score=float(r[16]),
            )
            for r in rows
        ]

    def search(self, question: str, limit: int = 10) -> dict:
        question = nfc(question).strip()
        explicit_company_codes = self.resolve_companies(question)
        scope_codes, scope_label, scope_alias = self.resolve_scope(question)
        company_codes = explicit_company_codes or scope_codes
        intents = self.infer_intents(question)
        groups = list(dict.fromkeys(group for intent in intents for group in intent["groups"]))
        years = sorted({int(y) for y in YEAR_RE.findall(question)})
        exact_dates = {
            f"{int(year):04d}{int(month):02d}{int(day):02d}"
            for year, month, day in DATE_KO_RE.findall(question)
        }
        periodic_subtype, base_month = self.infer_subtype(question)
        terms = self.search_terms(question, company_codes, scope_alias if not explicit_company_codes else None)
        business_question = any(x in question for x in ('핵심 사업', '사업 변화', '사업의 변화', '주요 사업'))
        if business_question:
            terms = ['사업의 내용', '사업의 개요', '주요 제품', '사업부문', '주요사업']
            for intent in intents:
                if intent['name'] == 'periodic':
                    intent['terms'] = terms
        if not terms and not company_codes:
            raise ValueError("검색어를 추출할 수 없습니다.")

        hits: list[Hit] = []
        job_hits: list[tuple[tuple[str, str, int | None], list[Hit]]] = []
        seen_chunks: set[tuple[str, int]] = set()
        targets: list[str | None] = company_codes or [None]
        per_company = max(4, limit // max(1, len(targets)) + 2)
        for intent in intents:
            intent_terms = list(dict.fromkeys(terms + intent["terms"]))[:20]
            expression = self.fts_expression(intent_terms)
            if not expression:
                expression = self.fts_expression([self.company_by_code[c][2] for c in company_codes])
            if not expression:
                continue
            # In "contracts signed in 2025 that were later terminated", 2025
            # qualifies the signing. A termination may occur in a later corpus year.
            if intent["name"] == "contract_termination" and "이후" in question:
                job_years: list[int | None] = [None]
            else:
                job_years = years if years else [None]
            for code in targets:
                for job_year in job_years:
                    job_groups = intent["groups"]
                    job_subtypes = list(intent["subtypes"])
                    job_base_month = None
                    if job_groups == ["periodic"]:
                        if periodic_subtype:
                            job_subtypes = [periodic_subtype]
                        job_base_month = base_month
                    found = self._search_once(
                        expression,
                        code,
                        job_groups,
                        [job_year] if job_year else [],
                        job_subtypes,
                        job_base_month,
                        per_company * 25,
                    )
                    # The year in "contracts signed in 2025 that were later
                    # terminated" qualifies the original signing date, not the
                    # termination filing date.  Exclude linked contracts signed
                    # in a different year before they can enter the answer context.
                    if intent["name"] == "contract_termination" and "이후" in question and years:
                        found = [
                            hit for hit in found
                            if self.contract_original_year(hit.doc_id) in set(years)
                        ]
                    unique_found: list[Hit] = []
                    for hit in found:
                        key = (hit.doc_id, hit.chunk_no)
                        if key not in seen_chunks:
                            hits.append(hit)
                            seen_chunks.add(key)
                        unique_found.append(hit)
                    job_hits.append(((code or "*", intent["name"], job_year), unique_found))

        # Only a genuinely unclassified question may relax document type. A
        # structured numeric intent must return an explicit no-evidence result
        # instead of silently borrowing an unrelated filing type.
        if not hits and groups and all(intent["name"] == "general" for intent in intents):
            expression = self.fts_expression(terms)
            for code in targets:
                fallback = self._search_once(expression, code, [], years, [], None, per_company * 25)
                for hit in fallback:
                    key = (hit.doc_id, hit.chunk_no)
                    if key not in seen_chunks:
                        hits.append(hit)
                        seen_chunks.add(key)

        hits = self.keep_latest_corrections(hits)
        allowed_chunks = {(hit.doc_id, hit.chunk_no) for hit in hits}
        job_hits = [
            (job, [hit for hit in candidates if (hit.doc_id, hit.chunk_no) in allowed_chunks])
            for job, candidates in job_hits
        ]
        job_status = []
        for (code, intent_name, job_year), candidates in job_hits:
            company_name = self.company_by_code[code][2] if code != "*" else "전체"
            receipts = list(dict.fromkeys(hit.rcept_no for hit in candidates))[:3]
            job_status.append(
                {
                    "company": company_name,
                    "intent": intent_name,
                    "year": job_year,
                    "found": bool(candidates),
                    "receipts": receipts,
                }
            )

        financial_terms = {
            "매출액", "영업이익", "당기순이익", "자산", "자산총계", "부채", "부채총계", "자본", "자본총계"
        }
        if business_question:
            for hit in hits:
                head = hit.text[:600]
                if any(x in head for x in ('사업의 내용', '사업의 개요', '주요 제품', '주요제품')):
                    hit.score -= 60
                if any(x in hit.text for x in ('최대주주', '주주에 관한 사항', '임원의 보수')):
                    hit.score += 80
        asks_financial_statement = bool(financial_terms.intersection(terms)) and groups == ["periodic"]
        if asks_financial_statement:
            income_metric = bool({"매출액", "영업이익", "당기순이익"}.intersection(terms))
            balance_metric = bool({"자산", "자산총계", "부채", "부채총계", "자본", "자본총계"}.intersection(terms))
            asks_separate = "별도" in question
            asks_consolidated = "연결" in question and not asks_separate
            requested_metrics = [metric for metric in financial_terms if metric in question]
            for hit in hits:
                text = hit.text
                if annual_facts(hit.__dict__, question):
                    hit.score -= 60.0
                income_markers = (
                    ("별도 손익계산서", "별도손익계산서", "별도 포괄손익계산서", "별도포괄손익계산서")
                    if asks_separate
                    else ("연결 손익계산서", "연결손익계산서", "연결 포괄손익계산서", "연결포괄손익계산서")
                )
                income_positions = [text.find(marker) for marker in income_markers if marker in text]
                balance_positions = [text.find(marker) for marker in ("연결 재무상태표", "연결재무상태표") if marker in text]
                if income_metric and income_positions:
                    hit.score -= 34.0 if min(income_positions) <= 700 else 22.0
                if balance_metric and not asks_separate and balance_positions:
                    hit.score -= 34.0 if min(balance_positions) <= 700 else 22.0
                if "요약 연결 재무정보" in text or "요약연결재무정보" in text:
                    hit.score -= 9.0
                elif "요약재무정보" in text and not asks_separate:
                    hit.score -= 5.0
                has_summary = "요약 연결 재무정보" in text or "요약연결재무정보" in text or "요약재무정보" in text
                if asks_consolidated and income_metric and not income_positions and not has_summary:
                    hit.score += 28.0
                if asks_consolidated and balance_metric and not balance_positions and not has_summary:
                    hit.score += 28.0
                if requested_metrics and not all(metric in text for metric in requested_metrics):
                    hit.score += 45.0
                if (
                    "최대주주" in text or "임원의 보수" in text or "관계기업의 최근 결산기" in text
                    or text.startswith("[제목] VII. 주주에 관한 사항")
                    or text.startswith("[제목] VIII. 임원 및 직원 등에 관한 사항")
                ):
                    hit.score += 35.0
        if exact_dates:
            for hit in hits:
                if hit.rcept_dt in exact_dates:
                    hit.score -= 12.0

        for _job, candidates in job_hits:
            candidates.sort(key=lambda h: (h.score, -int(h.rcept_dt), h.chunk_no))

        # Keep diverse companies, document groups and documents. This matters for
        # sector-wide comparisons where global BM25 would otherwise be dominated
        # by one company with many similar disclosures.
        hits.sort(key=lambda h: (h.score, -int(h.rcept_dt)))
        selected: list[Hit] = []
        per_doc: dict[str, int] = {}
        used_keys: set[tuple[str, str]] = set()

        per_doc_cap = 1 if len(company_codes) > 1 else 2

        def add_hit(hit: Hit) -> bool:
            if per_doc.get(hit.doc_id, 0) >= per_doc_cap:
                return False
            selected.append(hit)
            per_doc[hit.doc_id] = per_doc.get(hit.doc_id, 0) + 1
            used_keys.add((hit.corp_code, hit.doc_group))
            return True

        # First pass: fill one evidence slot for every company × intent × year job.
        # Empty jobs remain explicit in the scope header rather than being invented.
        for _job, candidates in job_hits:
            if len(selected) >= limit:
                break
            candidate = next((h for h in candidates if per_doc.get(h.doc_id, 0) < per_doc_cap), None)
            if candidate and candidate not in selected:
                add_hit(candidate)
        for hit in hits:
            if len(selected) >= limit:
                break
            if hit in selected:
                continue
            add_hit(hit)

        return {
            "question": question,
            "company_codes": company_codes,
            "explicit_company_codes": explicit_company_codes,
            "scope_label": scope_label,
            "groups": groups,
            "years": years,
            "exact_dates": sorted(exact_dates),
            "subtype": periodic_subtype,
            "base_month": base_month,
            "terms": terms,
            "intents": [intent["name"] for intent in intents],
            "jobs": job_status,
            "target_companies": [self.company_by_code[c][2] for c in company_codes],
            "target_entities": [
                {"corp_name": self.company_by_code[c][2], "listed_name": self.company_by_code[c][3]}
                for c in company_codes
            ],
            "hits": [asdict(h) for h in selected],
        }

    @staticmethod
    def format_context(result: dict, max_chars: int = 28000) -> str:
        blocks: list[str] = []
        size = 0
        target_companies = result.get("target_companies", [])
        jobs = result.get("jobs", [])
        if len(target_companies) > 1 or len(jobs) > 1:
            matched = list(dict.fromkeys(hit["corp_name"] for hit in result["hits"]))
            missing = [name for name in target_companies if name not in matched]
            scope = result.get("scope_label") or "명시 기업 비교"
            scope_block = (
                f"[검색 범위] {scope} | 대상기업={', '.join(target_companies)} | "
                f"근거검색기업={', '.join(matched) or '없음'} | "
                f"직접근거미검색기업={', '.join(missing) or '없음'}\n"
                "직접 근거가 검색되지 않은 기업은 수치가 0이라는 뜻이 아니며, 제공 근거에서 확인되지 않았다고 구분해야 합니다."
            )
            blocks.append(scope_block)
            size += len(scope_block) + 2
            job_lines = []
            for job in jobs:
                period = str(job["year"]) if job["year"] else "전체 제공기간"
                status = "근거 있음" if job["found"] else "직접 근거 없음"
                receipts = ",".join(job["receipts"]) or "-"
                job_lines.append(
                    f"- {job['company']} | {job['intent']} | {period} | {status} | 접수번호={receipts}"
                )
            job_block = "[검색 작업별 근거 상태]\n" + "\n".join(job_lines)
            blocks.append(job_block)
            size += len(job_block) + 2
        for i, hit in enumerate(result["hits"], 1):
            period = ""
            if hit["base_year"]:
                period = f" | 기준기간={hit['base_year']}.{int(hit['base_month'] or 0):02d}"
            header = (
                f"[근거 {i}] 회사={hit['corp_name']} | 공시={hit['report_nm']} | "
                f"접수일={hit['rcept_dt']} | 접수번호={hit['rcept_no']} | "
                f"정정={'예' if hit['is_correction'] else '아니오'}{period} | "
                f"원문={hit['source_file']}"
            )
            note = fact_note(annual_facts(hit, result['question']))
            block = header + "\n" + note + "\n" + hit["text"]
            if blocks and size + len(block) > max_chars:
                break
            blocks.append(block)
            size += len(block) + 2
        return "\n\n".join(blocks)
