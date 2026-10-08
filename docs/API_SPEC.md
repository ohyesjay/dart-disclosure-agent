# API

The local server exposes `GET /answer` with required string query parameters `question_id` and `question`.

```bash
curl -G http://127.0.0.1:8000/answer \
  --data-urlencode 'question_id=demo-revenue' \
  --data-urlencode 'question=가상알파의 2025년 사업보고서 연결 매출액은 얼마인가?'
```

Successful responses contain five string fields:

| Field | Meaning |
|---|---|
| `question_id` | Echoed request ID |
| `question` | Echoed question |
| `retrieved_context` | Selected filing evidence and available calculation notes |
| `think_trace` | Application-generated retrieval summary, not hidden model reasoning |
| `answer` | Answer text with source metadata |

`GET /health` returns `{"status":"ok"}`. It is a liveness endpoint; it does not verify model credentials or successful model calls.

FastAPI returns 422 for missing or invalid query parameters. Model HTTP errors are mapped to 503; unexpected internal errors are mapped to 500. Some retrieval `ValueError` conditions produce a 200 response explaining that evidence was not found.

The original application has no authentication or application-level rate limiting. Bind the public demo to localhost. Real deployment requires access controls, request limits, and independent financial-quality validation.
