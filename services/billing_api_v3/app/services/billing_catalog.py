"""Load and validate the server-owned Stripe billing catalog."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

import yaml

from services.billing_api_v3.app.core.config import get_settings


_PACKAGE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_STRIPE_PRICE_ID_PATTERN = re.compile(r"^price_[A-Za-z0-9]+$")
USD_NANOS_PER_CENT = 10_000_000


class BillingCatalogError(ValueError):
    """Raised when the server-owned billing catalog is unsafe or invalid."""


@dataclass(frozen=True)
class TopupPackage:
    package_id: str
    display_name: str
    stripe_price_id: str
    amount_cents: int
    credit_nanos: int


@dataclass(frozen=True)
class MonthlyServiceFeePlan:
    stripe_price_id: str
    amount_cents: int
    fee_nanos: int
    interval: str


@dataclass(frozen=True)
class BillingCatalog:
    schema_version: int
    environment: str
    currency: str
    topup_packages: dict[str, TopupPackage]
    monthly_service_fee: MonthlyServiceFeePlan

    def get_topup_package(self, package_id: str) -> TopupPackage:
        try:
            return self.topup_packages[package_id]
        except KeyError as exc:
            raise BillingCatalogError("Unknown top-up package.") from exc


def _required_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BillingCatalogError(f"{field_name} must be a mapping.")
    return value


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BillingCatalogError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BillingCatalogError(f"{field_name} must be a positive integer.")
    return value


def _validate_stripe_price_id(value: Any, *, field_name: str) -> str:
    price_id = _required_string(value, field_name=field_name)
    if not _STRIPE_PRICE_ID_PATTERN.fullmatch(price_id):
        raise BillingCatalogError(f"{field_name} must be a Stripe Price id.")
    return price_id


def _validate_amount_mapping(
    *,
    amount_cents: int,
    amount_nanos: int,
    cents_field_name: str,
    nanos_field_name: str,
) -> None:
    if amount_nanos != amount_cents * USD_NANOS_PER_CENT:
        raise BillingCatalogError(
            f"{nanos_field_name} must equal {cents_field_name} multiplied by "
            "USD_NANOS_PER_CENT."
        )


def load_billing_catalog(path: Path) -> BillingCatalog:
    """Load a test or production catalog without ever accepting client prices."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BillingCatalogError(f"Unable to read billing catalog at {path}.") from exc
    except yaml.YAMLError as exc:
        raise BillingCatalogError("Billing catalog is not valid YAML.") from exc

    catalog = _required_mapping(raw, field_name="catalog")
    schema_version = _positive_int(catalog.get("schema_version"), field_name="schema_version")
    if schema_version != 1:
        raise BillingCatalogError("Unsupported billing catalog schema_version.")

    environment = _required_string(catalog.get("environment"), field_name="environment")
    if environment not in {"test", "production"}:
        raise BillingCatalogError("environment must be test or production.")

    currency = _required_string(catalog.get("currency"), field_name="currency").upper()
    if currency != "USD":
        raise BillingCatalogError("Only USD billing catalog entries are supported.")

    raw_packages = _required_mapping(catalog.get("topup_packages"), field_name="topup_packages")
    if not raw_packages:
        raise BillingCatalogError("topup_packages must not be empty.")

    packages: dict[str, TopupPackage] = {}
    seen_price_ids: set[str] = set()
    for package_id, raw_package in raw_packages.items():
        if not isinstance(package_id, str) or not _PACKAGE_ID_PATTERN.fullmatch(package_id):
            raise BillingCatalogError("Top-up package identifiers are invalid.")
        package = _required_mapping(raw_package, field_name=f"topup_packages.{package_id}")
        stripe_price_id = _validate_stripe_price_id(
            package.get("stripe_price_id"),
            field_name=f"topup_packages.{package_id}.stripe_price_id",
        )
        if stripe_price_id in seen_price_ids:
            raise BillingCatalogError("Each top-up package must have a unique Stripe Price id.")
        seen_price_ids.add(stripe_price_id)
        amount_cents = _positive_int(
            package.get("amount_cents"),
            field_name=f"topup_packages.{package_id}.amount_cents",
        )
        credit_nanos = _positive_int(
            package.get("credit_nanos"),
            field_name=f"topup_packages.{package_id}.credit_nanos",
        )
        _validate_amount_mapping(
            amount_cents=amount_cents,
            amount_nanos=credit_nanos,
            cents_field_name=f"topup_packages.{package_id}.amount_cents",
            nanos_field_name=f"topup_packages.{package_id}.credit_nanos",
        )
        packages[package_id] = TopupPackage(
            package_id=package_id,
            display_name=_required_string(
                package.get("display_name"),
                field_name=f"topup_packages.{package_id}.display_name",
            ),
            stripe_price_id=stripe_price_id,
            amount_cents=amount_cents,
            credit_nanos=credit_nanos,
        )

    raw_fee = _required_mapping(catalog.get("monthly_service_fee"), field_name="monthly_service_fee")
    fee_price_id = _validate_stripe_price_id(
        raw_fee.get("stripe_price_id"),
        field_name="monthly_service_fee.stripe_price_id",
    )
    if fee_price_id in seen_price_ids:
        raise BillingCatalogError("The monthly service fee must use a separate Stripe Price id.")
    fee_amount_cents = _positive_int(
        raw_fee.get("amount_cents"),
        field_name="monthly_service_fee.amount_cents",
    )
    fee_nanos = _positive_int(
        raw_fee.get("fee_nanos"),
        field_name="monthly_service_fee.fee_nanos",
    )
    _validate_amount_mapping(
        amount_cents=fee_amount_cents,
        amount_nanos=fee_nanos,
        cents_field_name="monthly_service_fee.amount_cents",
        nanos_field_name="monthly_service_fee.fee_nanos",
    )
    interval = _required_string(raw_fee.get("interval"), field_name="monthly_service_fee.interval")
    if interval != "month":
        raise BillingCatalogError("monthly_service_fee.interval must be month.")

    return BillingCatalog(
        schema_version=schema_version,
        environment=environment,
        currency=currency,
        topup_packages=packages,
        monthly_service_fee=MonthlyServiceFeePlan(
            stripe_price_id=fee_price_id,
            amount_cents=fee_amount_cents,
            fee_nanos=fee_nanos,
            interval=interval,
        ),
    )


@lru_cache(maxsize=1)
def get_billing_catalog() -> BillingCatalog:
    return load_billing_catalog(get_settings().catalog_path)
