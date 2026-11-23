import hashlib
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .db import Base, SessionLocal, engine
from .models import AuditEvent, LedgerEntry, LedgerTransaction, Tenant
from .schemas import ImportRequest, ReversalRequest, TenantCreate, TransactionInput


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_minor(amount_minor: int) -> str:
    return f"{Decimal(amount_minor) / Decimal(100):.2f}"


def _api_key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _validation_messages(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]


def _entry_specs(data: TransactionInput) -> list[tuple[str, int]]:
    amount = int(Decimal(data.amount) * 100)
    if data.kind == "deposit":
        return [(data.client_ref, amount)]
    if data.kind in {"withdrawal", "fee"}:
        return [(data.client_ref, -amount)]
    return [
        (data.from_client_ref, -amount),
        (data.to_client_ref, amount),
    ]


def _append_transaction(
    db: Session,
    tenant_id: str,
    *,
    external_id: str,
    kind: str,
    amount_minor: int,
    currency: str,
    occurred_at: datetime,
    entries: list[tuple[str, int]],
    reversal_of_id: str | None = None,
    reason: str | None = None,
) -> LedgerTransaction:
    now = _utc_naive(datetime.now(timezone.utc))
    tx = LedgerTransaction(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        external_id=external_id,
        kind=kind,
        amount_minor=amount_minor,
        currency=currency,
        occurred_at=occurred_at,
        created_at=now,
        reversal_of_id=reversal_of_id,
        reason=reason,
    )
    db.add(tx)
    db.flush()
    db.add_all(
        LedgerEntry(
            transaction_id=tx.id,
            tenant_id=tenant_id,
            client_ref=client_ref,
            currency=currency,
            amount_minor=entry_amount,
        )
        for client_ref, entry_amount in entries
    )
    db.add(
        AuditEvent(
            tenant_id=tenant_id,
            transaction_id=tx.id,
            action="transaction_reversed" if reversal_of_id else "transaction_posted",
            reason=reason,
            created_at=now,
        )
    )
    db.commit()
    return tx


def create_app(
    session_factory: sessionmaker = SessionLocal,
    database_engine=engine,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        Base.metadata.create_all(bind=database_engine)
        yield

    app = FastAPI(
        title="Tenant Ledger",
        version="1.0.0",
        description="A tenant-scoped, append-only ledger challenge API.",
        lifespan=lifespan,
    )

    def get_session():
        with session_factory() as db:
            yield db

    def require_tenant(
        api_key: Annotated[str, Header(alias="X-Tenant-Key")],
        db: Session = Depends(get_session),
    ) -> Tenant:
        tenant = db.scalar(
            select(Tenant).where(Tenant.api_key_hash == _api_key_hash(api_key))
        )
        if tenant is None:
            raise HTTPException(status_code=401, detail="invalid tenant key")
        return tenant

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/v1/tenants", status_code=201)
    def create_tenant(payload: TenantCreate, db: Session = Depends(get_session)):
        api_key = secrets.token_urlsafe(32)
        tenant = Tenant(
            id=uuid.uuid4().hex,
            name=payload.name,
            api_key_hash=_api_key_hash(api_key),
            created_at=_utc_naive(datetime.now(timezone.utc)),
        )
        db.add(tenant)
        db.commit()
        return {"tenant_id": tenant.id, "name": tenant.name, "api_key": api_key}

    @app.post("/api/v1/transactions/import")
    def import_transactions(
        payload: ImportRequest,
        tenant: Tenant = Depends(require_tenant),
        db: Session = Depends(get_session),
    ):
        results = []
        for index, row in enumerate(payload.rows):
            try:
                data = TransactionInput.model_validate(row)
            except ValidationError as exc:
                results.append(
                    {
                        "row": index,
                        "status": "rejected",
                        "errors": _validation_messages(exc),
                    }
                )
                continue

            existing = db.scalar(
                select(LedgerTransaction).where(
                    LedgerTransaction.tenant_id == tenant.id,
                    LedgerTransaction.external_id == data.external_id,
                )
            )
            if existing is not None:
                results.append(
                    {
                        "row": index,
                        "status": "duplicate",
                        "transaction_id": existing.id,
                    }
                )
                continue

            try:
                tx = _append_transaction(
                    db,
                    tenant.id,
                    external_id=data.external_id,
                    kind=data.kind,
                    amount_minor=int(Decimal(data.amount) * 100),
                    currency=data.currency,
                    occurred_at=_utc_naive(data.occurred_at),
                    entries=_entry_specs(data),
                )
                results.append(
                    {
                        "row": index,
                        "status": "created",
                        "transaction_id": tx.id,
                    }
                )
            except IntegrityError:
                db.rollback()
                existing = db.scalar(
                    select(LedgerTransaction).where(
                        LedgerTransaction.tenant_id == tenant.id,
                        LedgerTransaction.external_id == data.external_id,
                    )
                )
                if existing is None:
                    raise HTTPException(
                        status_code=409, detail="transaction could not be posted"
                    )
                results.append(
                    {
                        "row": index,
                        "status": "duplicate",
                        "transaction_id": existing.id,
                    }
                )

        counts = {
            status: sum(item["status"] == status for item in results)
            for status in ("created", "duplicate", "rejected")
        }
        return {"results": results, "counts": counts}

    @app.post("/api/v1/transactions/{transaction_id}/reverse")
    def reverse_transaction(
        transaction_id: str,
        payload: ReversalRequest,
        tenant: Tenant = Depends(require_tenant),
        db: Session = Depends(get_session),
    ):
        original = db.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.id == transaction_id,
                LedgerTransaction.tenant_id == tenant.id,
            )
        )
        if original is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        if original.reversal_of_id is not None:
            raise HTTPException(status_code=409, detail="a reversal cannot be reversed")

        existing_key = db.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.tenant_id == tenant.id,
                LedgerTransaction.external_id == payload.external_id,
            )
        )
        if existing_key is not None:
            if existing_key.reversal_of_id == original.id:
                return {
                    "status": "duplicate",
                    "transaction_id": existing_key.id,
                    "reversal_of_id": original.id,
                }
            raise HTTPException(status_code=409, detail="external_id already used")

        existing_reversal = db.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.tenant_id == tenant.id,
                LedgerTransaction.reversal_of_id == original.id,
            )
        )
        if existing_reversal is not None:
            raise HTTPException(status_code=409, detail="transaction already reversed")

        original_entries = db.scalars(
            select(LedgerEntry).where(
                LedgerEntry.tenant_id == tenant.id,
                LedgerEntry.transaction_id == original.id,
            )
        ).all()
        try:
            reversal = _append_transaction(
                db,
                tenant.id,
                external_id=payload.external_id,
                kind="reversal",
                amount_minor=original.amount_minor,
                currency=original.currency,
                occurred_at=_utc_naive(datetime.now(timezone.utc)),
                entries=[
                    (entry.client_ref, -entry.amount_minor)
                    for entry in original_entries
                ],
                reversal_of_id=original.id,
                reason=payload.reason,
            )
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="transaction already reversed or key used"
            ) from exc
        return {
            "status": "created",
            "transaction_id": reversal.id,
            "reversal_of_id": original.id,
        }

    @app.get("/api/v1/clients/{client_ref}/transactions")
    def client_transactions(
        client_ref: str,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        tenant: Tenant = Depends(require_tenant),
        db: Session = Depends(get_session),
    ):
        rows = db.execute(
            select(LedgerEntry, LedgerTransaction)
            .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
            .where(
                LedgerEntry.tenant_id == tenant.id,
                LedgerEntry.client_ref == client_ref,
            )
            .order_by(LedgerTransaction.occurred_at, LedgerTransaction.created_at)
            .limit(limit)
            .offset(offset)
        ).all()
        return {
            "client_ref": client_ref,
            "transactions": [
                {
                    "transaction_id": tx.id,
                    "external_id": tx.external_id,
                    "kind": tx.kind,
                    "amount": _format_minor(entry.amount_minor),
                    "currency": entry.currency,
                    "occurred_at": _utc_iso(tx.occurred_at),
                    "reversal_of_id": tx.reversal_of_id,
                    "reason": tx.reason,
                }
                for entry, tx in rows
            ],
        }

    @app.get("/api/v1/clients/{client_ref}/balance")
    def client_balance(
        client_ref: str,
        as_of: date | None = None,
        tenant: Tenant = Depends(require_tenant),
        db: Session = Depends(get_session),
    ):
        report_date = as_of or datetime.now(timezone.utc).date()
        cutoff = datetime.combine(report_date.fromordinal(report_date.toordinal() + 1), time.min)
        rows = db.execute(
            select(LedgerEntry.currency, func.sum(LedgerEntry.amount_minor))
            .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
            .where(
                LedgerEntry.tenant_id == tenant.id,
                LedgerEntry.client_ref == client_ref,
                LedgerTransaction.occurred_at < cutoff,
            )
            .group_by(LedgerEntry.currency)
            .order_by(LedgerEntry.currency)
        ).all()
        return {
            "client_ref": client_ref,
            "as_of": report_date.isoformat(),
            "balances": [
                {"currency": currency, "amount": _format_minor(total or 0)}
                for currency, total in rows
            ],
        }

    @app.get("/api/v1/clients/{client_ref}/summary")
    def client_monthly_summary(
        client_ref: str,
        month: str = Query(pattern=r"^\d{4}-\d{2}$"),
        tenant: Tenant = Depends(require_tenant),
        db: Session = Depends(get_session),
    ):
        try:
            year, month_number = (int(value) for value in month.split("-"))
            start = datetime(year, month_number, 1)
            next_month = datetime(year + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="month must be YYYY-MM") from exc
        rows = db.execute(
            select(LedgerEntry, LedgerTransaction)
            .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
            .where(
                LedgerEntry.tenant_id == tenant.id,
                LedgerEntry.client_ref == client_ref,
                LedgerTransaction.occurred_at >= start,
                LedgerTransaction.occurred_at < next_month,
            )
        ).all()
        totals: dict[str, dict[str, int]] = {}
        for entry, tx in rows:
            currency_totals = totals.setdefault(
                entry.currency, {"inflows": 0, "outflows": 0, "fees": 0, "net": 0}
            )
            currency_totals["net"] += entry.amount_minor
            if entry.amount_minor > 0:
                currency_totals["inflows"] += entry.amount_minor
            elif entry.amount_minor < 0:
                currency_totals["outflows"] += abs(entry.amount_minor)
            if tx.kind == "fee":
                currency_totals["fees"] += abs(entry.amount_minor)
        return {
            "client_ref": client_ref,
            "month": month,
            "by_currency": [
                {
                    "currency": currency,
                    "inflows": _format_minor(values["inflows"]),
                    "outflows": _format_minor(values["outflows"]),
                    "fees": _format_minor(values["fees"]),
                    "net": _format_minor(values["net"]),
                }
                for currency, values in sorted(totals.items())
            ],
        }

    @app.get("/api/v1/transactions/{transaction_id}/audit")
    def transaction_audit(
        transaction_id: str,
        tenant: Tenant = Depends(require_tenant),
        db: Session = Depends(get_session),
    ):
        original = db.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.id == transaction_id,
                LedgerTransaction.tenant_id == tenant.id,
            )
        )
        if original is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        related_ids = select(LedgerTransaction.id).where(
            LedgerTransaction.tenant_id == tenant.id,
            or_(
                LedgerTransaction.id == original.id,
                LedgerTransaction.reversal_of_id == original.id,
            ),
        )
        events = db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.transaction_id.in_(related_ids),
            )
            .order_by(AuditEvent.created_at, AuditEvent.id)
        ).all()
        return {
            "transaction_id": original.id,
            "events": [
                {
                    "transaction_id": event.transaction_id,
                    "action": event.action,
                    "reason": event.reason,
                    "created_at": _utc_iso(event.created_at),
                }
                for event in events
            ],
        }

    return app


app = create_app()
