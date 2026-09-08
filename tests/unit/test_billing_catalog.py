from pathlib import Path

import pytest

from services.billing_api_v3.app.services.billing_catalog import (
    BillingCatalogError,
    load_billing_catalog,
)


CATALOG_PATH = Path("config/billing.test.yaml")


def test_test_catalog_contains_only_the_approved_stripe_prices() -> None:
    catalog = load_billing_catalog(CATALOG_PATH)

    assert catalog.environment == "test"
    assert catalog.get_topup_package("credit_10_usd").stripe_price_id == (
        "price_1U3ZKOB5Es3VU3maflfGkdrX"
    )
    assert catalog.get_topup_package("credit_10_usd").credit_nanos == 10_000_000_000
    assert catalog.monthly_service_fee.stripe_price_id == "price_1U3ZYBB5Es3VU3maSP6qq6sg"
    assert catalog.monthly_service_fee.fee_nanos == 5_000_000_000


def test_catalog_rejects_a_credit_amount_that_does_not_match_its_price(tmp_path) -> None:
    invalid_catalog = tmp_path / "billing.invalid.yaml"
    invalid_catalog.write_text(
        """
schema_version: 1
environment: test
currency: USD
topup_packages:
  credit_5_usd:
    display_name: $5 token credit
    stripe_price_id: price_1U3ZHnB5Es3VU3maoEQbMKnC
    amount_cents: 500
    credit_nanos: 4000000000
monthly_service_fee:
  stripe_price_id: price_1U3ZYBB5Es3VU3maSP6qq6sg
  amount_cents: 500
  fee_nanos: 5000000000
  interval: month
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(BillingCatalogError, match="credit_nanos"):
        load_billing_catalog(invalid_catalog)


def test_catalog_rejects_an_unknown_topup_package() -> None:
    catalog = load_billing_catalog(CATALOG_PATH)

    with pytest.raises(BillingCatalogError, match="Unknown top-up package"):
        catalog.get_topup_package("client_selected_price")
