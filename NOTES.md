# Notes
- Chosen: tenant-scoped immutable ledger, row-level imports, reversals, balances, and monthly summaries.
- Tenant API keys are random, returned once, and stored as SHA-256 hashes.
- Amounts are decimal strings at the API boundary and signed integer minor units in SQLite.
- Supported currencies: USD, EUR, GBP; summaries remain separated by currency and do not convert FX.
- A transfer writes linked debit and credit entries in one database transaction.
- Corrections append a reversal with a reason; original postings are never edited or deleted.
- Unique tenant/external-id constraints make import retries idempotent under concurrent requests.
- SQLite is a single-writer local challenge database; use PostgreSQL for higher write concurrency.
- Skipped: role-specific staff access, FX conversion, exports, rate limiting, fraud flags, and deployment.
