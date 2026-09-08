"""Create one non-production prepaid wallet with an immutable test-credit record.

This helper is intentionally not a payment integration. Use it only against a
development project after authenticating as a trusted operator. Production
wallet credits must originate from a verified payment-provider webhook.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
from pathlib import Path
import sys
import uuid

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from common.billing import customer_wallet_document_id


NANOS_PER_USD = Decimal("1000000000")
WALLETS_COLLECTION = os.getenv(
    "FIRESTORE_CUSTOMER_WALLETS_COLLECTION",
    "customer_wallets_v3",
).strip() or "customer_wallets_v3"
TRANSACTIONS_COLLECTION = os.getenv(
    "FIRESTORE_WALLET_TRANSACTIONS_COLLECTION",
    "wallet_transactions_v3",
).strip() or "wallet_transactions_v3"



def main() -> None:
    args = _parse_args()
    project_id = args.project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project_id:
        raise SystemExit("Pass --project-id or set GOOGLE_CLOUD_PROJECT.")
    credit_nanos = _usd_to_nanos(args.credit_usd)
    if credit_nanos <= 0:
        raise SystemExit("--credit-usd must be greater than zero.")

    try:
        from google.cloud import firestore
    except ImportError as exc:
        raise SystemExit(
            "google-cloud-firestore is required. Install services/agent_persistence_worker_v3/requirements.txt first."
        ) from exc

    client = firestore.Client(project=project_id)
    wallet_id = customer_wallet_document_id(args.uid)
    wallet_ref = client.collection(WALLETS_COLLECTION).document(wallet_id)
    transaction_id = f"manual_test_credit-{uuid.uuid4().hex}"
    transaction_ref = client.collection(TRANSACTIONS_COLLECTION).document(transaction_id)
    created_at = datetime.now(timezone.utc)

    @firestore.transactional
    def create_wallet(transaction):
        wallet_snapshot = transaction.get(wallet_ref)
        if wallet_snapshot.exists:
            raise RuntimeError(
                f"Wallet {wallet_id} already exists; this script never changes an existing wallet."
            )
        transaction.create(
            wallet_ref,
            {
                "schema_version": 1,
                "billing_subject_id": args.uid,
                "owner_uid": args.uid,
                "currency": "USD",
                "status": "active",
                "available_credit_nanos": credit_nanos,
                "reserved_credit_nanos": 0,
                "settled_usage_nanos": 0,
                "lifetime_credited_nanos": credit_nanos,
                "created_at": created_at,
                "updated_at": created_at,
            },
        )
        transaction.create(
            transaction_ref,
            {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "transaction_type": "manual_test_credit",
                "status": "posted",
                "billing_subject_id": args.uid,
                "owner_uid": args.uid,
                "wallet_document_id": wallet_id,
                "currency": "USD",
                "amount_nanos": credit_nanos,
                "created_at": created_at,
                "reason": args.reason,
                "external_reference": "non-production-manual-credit",
            },
        )

    create_wallet(client.transaction())
    print(
        f"Created wallet {wallet_id} for uid {args.uid} with {credit_nanos} USD nanos "
        f"and transaction {transaction_id}."
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", help="Non-production Google Cloud project id.")
    parser.add_argument("--uid", required=True, help="Firebase Auth UID that owns the wallet.")
    parser.add_argument("--credit-usd", required=True, help="Initial test credit in USD, for example 10.00.")
    parser.add_argument("--reason", default="local billing smoke test")
    parser.add_argument(
        "--non-production",
        action="store_true",
        help="Required acknowledgement that this creates unverified test credit.",
    )
    args = parser.parse_args()
    if not args.non_production:
        parser.error("--non-production is required; do not use this helper to credit production wallets.")
    return args


def _usd_to_nanos(value: str) -> int:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise SystemExit("--credit-usd must be a valid decimal USD amount.") from exc
    if not parsed.is_finite():
        raise SystemExit("--credit-usd must be finite.")
    return int((parsed * NANOS_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


if __name__ == "__main__":
    main()
