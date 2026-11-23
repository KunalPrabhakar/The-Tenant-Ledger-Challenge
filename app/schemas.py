from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP"}
TransactionKind = Literal["deposit", "withdrawal", "fee", "transfer"]


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class TransactionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=128)
    kind: TransactionKind
    amount: str = Field(min_length=1, max_length=32)
    currency: str = Field(min_length=3, max_length=3)
    occurred_at: datetime
    client_ref: str | None = Field(default=None, max_length=128)
    from_client_ref: str | None = Field(default=None, max_length=128)
    to_client_ref: str | None = Field(default=None, max_length=128)

    @field_validator("external_id", "client_ref", "from_client_ref", "to_client_ref")
    @classmethod
    def clean_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("currency")
    @classmethod
    def supported_currency(cls, value: str) -> str:
        value = value.upper()
        if value not in SUPPORTED_CURRENCIES:
            raise ValueError("currency must be USD, EUR, or GBP")
        return value

    @field_validator("amount")
    @classmethod
    def valid_positive_amount(cls, value: str) -> str:
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("amount must be a decimal string") from exc
        if not amount.is_finite() or amount <= 0:
            raise ValueError("amount must be a positive finite value")
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError("amount must have at most two decimal places")
        if amount > Decimal("999999999999.99"):
            raise ValueError("amount exceeds the supported maximum")
        return format(amount, "f")

    @field_validator("occurred_at")
    @classmethod
    def aware_past_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        value = value.astimezone(timezone.utc)
        if value > datetime.now(timezone.utc):
            raise ValueError("future transactions are not allowed")
        return value

    @model_validator(mode="after")
    def valid_client_fields(self):
        if self.kind == "transfer":
            if not self.from_client_ref or not self.to_client_ref:
                raise ValueError("transfers require from_client_ref and to_client_ref")
            if self.from_client_ref == self.to_client_ref:
                raise ValueError("transfer clients must be different")
            if self.client_ref:
                raise ValueError("transfers must not include client_ref")
        else:
            if not self.client_ref:
                raise ValueError(f"{self.kind} requires client_ref")
            if self.from_client_ref or self.to_client_ref:
                raise ValueError("from_client_ref and to_client_ref are only for transfers")
        return self


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class ReversalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("external_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value
