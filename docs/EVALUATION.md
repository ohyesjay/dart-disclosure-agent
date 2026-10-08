# Evaluation and lessons

## Public synthetic demo checks

On 2026-10-08, all three public synthetic checks passed: revenue comparison, operating-margin calculation, and abstention when the requested year was unavailable. Local FastAPI checks also passed for the five-string response contract, question echo, source metadata, health endpoint, and rejection of missing required query parameters. These checks used fictional filings and mock LLM mode; no live HCX call was made.

## Original development checks

On 2026-10-08, the following checks were rerun against the reviewed local submission source and its original index. The index is not part of this public repository.

| Command | Observed result | What it checks |
|---|---:|---|
| `python scripts/evaluate.py` | 13/13 | Selected retrieval receipts, scope, and context expectations |
| `python scripts/evaluate_coverage.py` | 96/96 | 70 company-report cases and 26 event/ownership cases |
| `python scripts/evaluate_sectors.py` | 4/4 | Selected sector scope and company coverage |
| `python scripts/evaluate.py --cases eval/advanced_cases.json` | 9/9 | Selected complex retrieval cases |
| `python scripts/test_answer_logic.py` | 8/8 | Selected financial calculations and contract-year filtering |

The first four rows total 122 retrieval checks. Passing them does not establish correct answers to arbitrary financial questions. In particular, retrieval tests can pass when a relevant filing is present but the generated answer reads the wrong table, period, or metric.

The API evaluator checks response fields, strings, question echo, selected expected values, citation-related text, and timeout. Only 2 of its 13 basic cases specify `answer_must_contain` values. Citation-related text is not a claim-by-claim evidence check. Historical API pass rates and cached timings should not be presented as an independently measured financial accuracy rate or cold-generation latency.

## Review of 30 responses

Following submission, 30 questions were recovered from evaluation request logs and replayed against the server. An AI-assisted review, documented on 2026-09-12, compared responses and retrieved context with source filings. It examined company attribution, report periods, financial metrics, units, calculations, and citation support.

The review produced a question-by-question discrepancy report. The report and earlier Codex work records exist separately from the original source repository. Private evaluation questions, raw logs, and complete replay responses are not republished in this edition.

The review was a project postmortem, not official judging or independent human-only validation. This public summary does not newly re-verify all 30 responses. Replayed answers may have come from the application's `(question_id, question)` cache, so replay speed is not evidence of model-generation speed.

## Main failure patterns

| Failure | Why it matters | Next improvement |
|---|---|---|
| Company or related-party mix-up | A plausible figure may belong to a different company | Require a resolved entity and reject unsupported names |
| Annual, cumulative, and quarter values mixed | Comparisons become financially invalid | Carry an explicit reporting-period key through extraction and calculation |
| Consolidated/separate or metric confusion | A correct-looking number answers a different question | Validate statement basis and metric before accepting facts |
| Unit conversion and arithmetic errors | Small transcription errors can materially change results | Normalize units and compute supported results outside the LLM |
| Missing or mismatched citations | A source list can exist without supporting the claims | Validate citation IDs and their supporting evidence spans |
| Unsupported event inference | A contract filing does not prove a later termination | Link event chains and distinguish missing evidence from non-occurrence |

During the 2026-10-08 source review, the retained code also reproduced three boundary issues: substring entity matching, consolidated-summary facts accepted for a separate-statement question, and annual facts accepted for a fourth-quarter question. The public edition documents these issues rather than claiming that the postmortem has already resolved them.

## Practical lesson

An operational API, relevant retrieved documents, and passing development cases were useful milestones. Financial answer quality still required separate checks against source tables and genuinely different questions. A future iteration should establish the evaluation set and entity/period/basis validation before expanding answer coverage.
