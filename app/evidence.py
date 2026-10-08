"""Conservative extraction of explicitly labelled periodic statement columns."""
import re
from decimal import Decimal


def annual_facts(hit, question):
    if hit['doc_group'] != 'periodic' or hit['doc_subtype'] not in {'annual', 'half', 'quarter'}:
        return []
    text = hit['text']
    requested_years = sorted({int(year) for year in re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d)', question)})
    if hit['doc_subtype'] == 'annual' and '요약연결재무정보' in text:
        summary = text.split('나. 요약 별도재무정보', 1)[0]
        unit_match = re.search(r'단위\s*:\s*(백만원|천원|억원|원)', summary)
        header = next((line for line in summary.splitlines() if line.strip().startswith('구분 | 제')), None)
        if unit_match and header:
            column_count = len(header.split('|')) - 1
            summary_facts = []
            metrics = {'매출액': r'매출액', '영업이익': r'영업(?:이익|손익|손실)(?:\(손실\))?'}
            years_to_read = requested_years or [int(hit['base_year'])]
            for metric, pattern in metrics.items():
                if metric not in question:
                    continue
                line = next((row for row in summary.splitlines()
                             if re.fullmatch(pattern + r'\s*\|.*', row.strip())), None)
                if not line:
                    continue
                cells = [cell.strip() for cell in line.split('|')]
                if len(cells) != column_count + 1:
                    continue
                for year in years_to_read:
                    offset = int(hit['base_year']) - year
                    if offset < 0 or offset >= column_count:
                        continue
                    raw = cells[offset + 1]
                    if not re.fullmatch(r'-?[\d,]+(?:\.\d+)?|\([\d,]+(?:\.\d+)?\)', raw):
                        continue
                    value = Decimal(raw.replace(',', '').replace('(', '-').replace(')', ''))
                    multiplier = {'원': 1, '천원': 1000, '백만원': 1000000, '억원': 100000000}[unit_match.group(1)]
                    summary_facts.append({
                        'company': hit['corp_name'], 'year': year, 'metric': metric,
                        'value': str(value), 'unit': unit_match.group(1), 'won': str(value * multiplier),
                        'receipt': hit['rcept_no'], 'period_label': f'{year}년 사업보고서',
                    })
            if summary_facts:
                return summary_facts
    statement = re.search(r'연결\s*(?:포괄)?손익계산서', text)
    if not statement:
        return []
    text = text[statement.start():]
    if '별도' in question:
        return []
    periods = dict(re.findall(
        r'제\s*(\d+)\s*기(?:\s*(?:반기|\d+분기))?\s+((?:19|20)\d{2})[.]01[.]01\s*부터',
        text,
    ))
    unit = re.search(r'단위\s*:\s*(백만원|천원|억원|원)', text)
    if not unit:
        return []
    lines = text.splitlines()
    period_label = f"{hit['base_year']}년 사업보고서"
    columns = []
    if hit['doc_subtype'] == 'annual':
        header = next((line for line in lines if '|' in line and
                       all(re.fullmatch(r'제\s*\d+\s*기', cell.strip()) for cell in line.split('|'))), None)
        if not header:
            return []
        years = [periods.get(re.search(r'\d+', cell).group()) for cell in header.split('|')]
        if str(hit['base_year']) not in years:
            return []
        column = years.index(str(hit['base_year'])) + 1
        expected_cells = len(years) + 1
        columns = [(column, period_label)]
    else:
        header_index = next((i for i, line in enumerate(lines) if '|' in line and
                             all(re.search(r'제\s*\d+\s*기', cell.strip()) for cell in line.split('|'))), None)
        if header_index is None or header_index + 1 >= len(lines):
            return []
        header_cells = lines[header_index].split('|')
        subheader = [cell.strip() for cell in lines[header_index + 1].split('|')]
        if len(subheader) != len(header_cells) * 2 or not all(cell in {'3개월', '누적'} for cell in subheader):
            return []
        years = [periods.get(re.search(r'\d+', cell).group()) for cell in header_cells]
        if str(hit['base_year']) not in years:
            return []
        group = years.index(str(hit['base_year']))
        wants_three_months = any(word in question for word in ('2분기', '단일분기', '당분기', '3개월'))
        wants_cumulative = any(word in question for word in ('반기 누적', '누적'))
        expected_cells = len(subheader) + 1
        if hit.get('base_month') == 6:
            labels = (
                f"{hit['base_year']}년 2분기(3개월)",
                f"{hit['base_year']}년 반기 누적",
            )
        elif hit.get('base_month') == 9:
            labels = (
                f"{hit['base_year']}년 3분기(3개월)",
                f"{hit['base_year']}년 3분기 누적",
            )
        else:
            labels = (f"{hit['base_year']}년 단일분기", f"{hit['base_year']}년 누적")
        requested_columns = (0, 1) if wants_three_months and wants_cumulative else (
            (0,) if wants_three_months else (1,)
        )
        columns = [(group * 2 + offset + 1, labels[offset]) for offset in requested_columns]
    metrics = {
        '매출액': r'매출액',
        '영업이익': r'영업(?:이익|손익|손실)(?:\(손실\))?',
    }
    facts = []
    for metric, pattern in metrics.items():
        if metric not in question:
            continue
        for line in text.splitlines():
            cells = [cell.strip() for cell in line.split('|')]
            if len(cells) != expected_cells or not re.fullmatch(pattern + r'(?:\s*\(주[^)]*\))?', cells[0]):
                continue
            for column, selected_period in columns:
                raw = cells[column]
                if not re.fullmatch(r'-?[\d,]+(?:\.\d+)?|\([\d,]+(?:\.\d+)?\)', raw):
                    continue
                value = Decimal(raw.replace(',', '').replace('(', '-').replace(')', ''))
                multiplier = {'원': 1, '천원': 1000, '백만원': 1000000, '억원': 100000000}[unit.group(1)]
                facts.append({'company': hit['corp_name'], 'year': hit['base_year'],
                              'metric': metric, 'value': str(value), 'unit': unit.group(1),
                              'won': str(value * multiplier), 'receipt': hit['rcept_no'],
                              'period_label': selected_period})
    return facts


def fact_note(facts):
    return '\n'.join(
        f"[연도 열 검증] {f['company']} | {f['year']}년 연결 {f['metric']}={f['value']} {f['unit']} "
        f"(원 환산={f['won']}). 표의 연도와 열을 대응하여 추출했으며 괄호는 음수입니다."
        for f in facts
    )


def comparison_note(facts, question):
    if not any(x in question for x in ('차이', '격차율')):
        return ''
    unique = {}
    for fact in facts:
        unique.setdefault((fact['company'], fact['year'], fact['metric']), fact)
    rows = list(unique.values())
    if len(rows) != 2 or (rows[0]['year'], rows[0]['metric']) != (rows[1]['year'], rows[1]['metric']):
        return ''
    a, b = rows
    av, bv = Decimal(a['won']), Decimal(b['won'])
    difference = av - bv
    note = f"[검산] {a['company']} - {b['company']} = {difference} 원."
    if bv > 0:
        ratio = (difference / bv * 100).quantize(Decimal('0.01'))
        note += f" {b['company']}를 분모로 한 격차율=({difference}/{bv})×100={ratio}%."
    return note


def _number(value):
    number = Decimal(str(value))
    if number == number.to_integral_value():
        return f"{int(number):,}"
    return f"{number:,f}".rstrip('0').rstrip('.')


def deterministic_financial_answer(question, facts, target_companies):
    """Build closed annual-statement answers without asking the LLM to copy digits.

    The LLM is still used for narrative disclosure questions.  Annual financial
    comparisons are safer to render from the year/column/unit facts extracted
    above because one dropped zero changes the substance of the answer.
    """
    requested_metrics = [metric for metric in ('매출액', '영업이익') if metric in question]
    if not requested_metrics or not facts:
        return ''
    unique = {}
    for fact in facts:
        if fact['metric'] in requested_metrics:
            unique.setdefault((fact['company'], fact['year'], fact['metric'], fact['period_label']), fact)

    if set(requested_metrics) == {'매출액', '영업이익'} and '영업이익률' in question:
        margin_rows = []
        for company in target_companies:
            company_facts = [fact for key, fact in unique.items() if key[0] == company]
            revenue = [fact for fact in company_facts if fact['metric'] == '매출액']
            profit = [fact for fact in company_facts if fact['metric'] == '영업이익']
            if len(revenue) != 1 or len(profit) != 1 or revenue[0]['period_label'] != profit[0]['period_label']:
                return ''
            revenue_won, profit_won = Decimal(revenue[0]['won']), Decimal(profit[0]['won'])
            if revenue_won == 0:
                return ''
            margin = (profit_won / revenue_won * 100).quantize(Decimal('0.01'))
            margin_rows.append((company, revenue[0], profit[0], margin))
        margin_rows.sort(key=lambda row: row[3], reverse=True)
        lines = ["공시 원문에서 같은 연결범위·기간의 매출액과 영업이익을 확인해 계산했습니다.", "",
                 "| 순위 | 기업 | 연결 매출액 | 연결 영업이익 | 영업이익률 | 근거 |",
                 "|---:|---|---:|---:|---:|---|"]
        for rank, (company, revenue, profit, margin) in enumerate(margin_rows, 1):
            refs = sorted({revenue['evidence_index'], profit['evidence_index']})
            lines.append(
                f"| {rank} | {company} | {_number(revenue['won'])}원 | {_number(profit['won'])}원 | "
                f"{_number(margin)}% | {'; '.join(f'[근거 {ref}]' for ref in refs)} |"
            )
        lines.append("")
        lines.append("영업이익률 = 연결 영업이익 ÷ 연결 매출액 × 100으로 계산했습니다.")
        return "\n".join(lines)

    if len(requested_metrics) != 1:
        return ''
    metric = requested_metrics[0]

    if len(target_companies) == 1:
        rows = [fact for key, fact in unique.items() if key[0] == target_companies[0] and key[2] == metric]
        if len(rows) == 2:
            rows.sort(key=lambda fact: (fact['year'], '누적' in fact['period_label']))
            first, second = rows
            first_value, second_value = Decimal(first['won']), Decimal(second['won'])
            difference = second_value - first_value
            lines = [f"{first['company']}의 연결 {metric}을 공시 원문 표의 기간별 열로 검증했습니다.", "",
                     f"| 기간 | 연결 {metric} | 공시 원문 값 | 근거 |", "|---|---:|---:|---|"]
            for row in rows:
                lines.append(
                    f"| {row['period_label']} | {_number(row['won'])}원 | "
                    f"{_number(row['value'])}{row['unit']} | [근거 {row['evidence_index']}] |"
                )
            lines.append("")
            lines.append(
                f"{second['period_label']} - {first['period_label']} = {_number(difference)}원입니다."
            )
            if any(word in question for word in ('증가율', '증감률')):
                if first_value == 0:
                    lines.append("이전 기간 값이 0이므로 증가율은 계산할 수 없습니다.")
                else:
                    rate = (difference / abs(first_value) * 100).quantize(Decimal('0.01'))
                    lines.append(
                        f"증가율은 ({_number(difference)} / {_number(abs(first_value))}) × 100 = {_number(rate)}%입니다."
                    )
            return "\n".join(lines)

    rows = []
    for company in target_companies:
        candidates = [fact for key, fact in unique.items() if key[0] == company]
        if len(candidates) != 1:
            return ''
        rows.append(candidates[0])
    if not rows:
        return ''

    periods = {row.get('period_label', f"{row['year']}년") for row in rows}
    if len(periods) != 1:
        return ''
    period = next(iter(periods))
    lines = [f"{period} 연결 {metric}을 공시 원문 표에서 해당 기간 열로 검증한 결과입니다.", ""]
    lines.extend(("| 기업 | 연결 " + metric + " | 공시 원문 값 | 근거 |", "|---|---:|---:|---|"))
    for row in rows:
        won = Decimal(row['won'])
        suffix = " (영업손실)" if metric == '영업이익' and won < 0 else ''
        lines.append(
            f"| {row['company']} | {_number(won)}원{suffix} | "
            f"{_number(row['value'])}{row['unit']} | [근거 {row['evidence_index']}] |"
        )

    if len(rows) == 2 and any(word in question for word in ('차이', '격차율')):
        first, second = rows
        first_value, second_value = Decimal(first['won']), Decimal(second['won'])
        difference = first_value - second_value
        direction = "더 큽니다" if difference >= 0 else "더 작습니다"
        lines.append("")
        lines.append(
            f"{first['company']}의 {metric}은 {second['company']}보다 "
            f"{_number(abs(difference))}원 {direction} "
            f"({_number(first_value)}원 - {_number(second_value)}원)."
        )
        if '격차율' in question:
            if second_value == 0:
                lines.append(f"{second['company']} 값이 0이므로 이를 분모로 한 격차율은 계산할 수 없습니다.")
            else:
                ratio = (difference / abs(second_value) * 100).quantize(Decimal('0.01'))
                lines.append(
                    f"{second['company']}를 분모로 한 격차율은 "
                    f"({_number(difference)} / {_number(abs(second_value))}) × 100 = {_number(ratio)}%입니다."
                )
    return "\n".join(lines)
