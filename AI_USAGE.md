# AI usage
- Tool: OpenAI Codex, used to review the supplied challenge brief and assist with implementation, including SQL summary aggregation and regression tests.
- Key prompt themes: build a balanced tenant-ledger slice in the challenge folder using FastAPI, SQLite, and pytest; keep work local first.
- Human-selected scope: tenant isolation, partial imports, idempotency, reversals, as-of balances, and monthly summaries.
- Decisions: amounts use integer minor units; transfers use paired postings; supported currencies are USD, EUR, and GBP.
- Rejected for this time-box: FX conversion, role-based staff access, exports, rate limiting, fraud detection, and PostgreSQL.
- The implementation and tests were reviewed against the challenge requirements; adjust any design choice you cannot explain before submission.
