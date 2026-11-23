# Tenant Ledger
Install and run: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && uvicorn app.main:app --reload`
Tests: `pytest`
Open `/docs`; create a tenant at `/api/v1/tenants` to receive an `X-Tenant-Key`.
SQLite is the default; set `DATABASE_URL` to override it.
