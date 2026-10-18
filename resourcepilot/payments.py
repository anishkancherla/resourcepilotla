"""The paid hall match: Stripe test mode and testnet FET, over Fetch's Agent Payment Protocol.

Campus housing facts and official links are free. Once Verification has re-opened the halls a
school lists, the student may pay ResourcePilot to have the Planner match every hall to their
criteria. The payment buys that match, never housing: ResourcePilot never applies for, reserves,
or pays for a room.

Only the Orchestrator takes payments. It is the seller in ``AgentPaymentProtocol``: it sends one
``RequestPayment`` per offer, and answers each ``CommitPayment`` with ``CompletePayment`` only
after checking the payment itself, never trusting what the buyer says it paid:

- Stripe: the Checkout Session must be the one created for this workflow, paid, in test mode,
  in USD, and for exactly the configured price.
- FET: the transaction is read from the Fetch stable testnet. It must have succeeded and hold a
  bank transfer of at least the configured price, in ``atestfet``, from the buyer's wallet to
  the Orchestrator's own wallet.

There is no live mode. A Stripe key that is not a test key stops the Orchestrator from starting,
and the FET network is fixed to the testnet, so no real money can move.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from uagents_core.contrib.protocols.payment import Funds, RequestPayment

from resourcepilot.clock import utc_now
from resourcepilot.http import MAX_RESPONSE_BYTES, Http, ProviderError, api_timeout

STRIPE_API = "https://api.stripe.com"
STRIPE = "Stripe"
TESTNET = "Fetch testnet"
FET_NETWORK = "stable-testnet"
FET_DENOM = "atestfet"
ATTO_PER_FET = 10**18
MSG_SEND = "/cosmos.bank.v1beta1.MsgSend"
SERVICE = "housing_match"
PRODUCT_NAME = "ResourcePilot hall match"
# Stripe requires a Checkout Session to stay open at least 30 minutes.
PAY_WINDOW_SECONDS = 30 * 60
MIN_PRICE_CENTS, MAX_PRICE_CENTS = 50, 2000
MAX_PRICE_FET = Decimal("100")
TESTNET_TIMEOUT_SECONDS = 15.0
TRUE_WORDS = frozenset({"1", "true", "yes", "on"})

STRIPE_SECRET = re.compile(r"sk_test_[A-Za-z0-9]{10,250}")
STRIPE_PUBLISHABLE = re.compile(r"pk_test_[A-Za-z0-9]{10,250}")
CHECKOUT_ID = re.compile(r"cs_test_[A-Za-z0-9]{10,250}")
TX_HASH = re.compile(r"[A-Fa-f0-9]{64}")
FET_WALLET = re.compile(r"fetch1[02-9ac-hj-np-z]{38,58}")


@dataclass(frozen=True)
class PaymentConfig:
    price_cents: int = 100
    price_fet: str = "1"
    stripe_secret: str = field(default="", repr=False)
    stripe_publishable: str = ""
    accept_fet: bool = False

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret and self.stripe_publishable)

    @property
    def enabled(self) -> bool:
        return self.stripe_enabled or self.accept_fet

    @property
    def price_atto(self) -> int:
        return int(Decimal(self.price_fet) * ATTO_PER_FET)

    def price_labels(self) -> list[str]:
        """How the student can pay, in the order the payment request offers it."""
        labels = []
        if self.stripe_enabled:
            labels.append(f"${self.price_cents / 100:.2f} by card (Stripe test mode)")
        if self.accept_fet:
            labels.append(f"{self.price_fet} FET on the Fetch testnet")
        return labels


def load_config(env: Mapping[str, str] | None = None) -> PaymentConfig:
    """Read the payment settings. Errors name the variable, never its value.

    Raises ``ValueError`` for a live Stripe key, so the Orchestrator refuses to start with one.
    """
    values = os.environ if env is None else env
    secret = str(values.get("STRIPE_SECRET_KEY", "") or "").strip()
    publishable = str(values.get("STRIPE_PUBLISHABLE_KEY", "") or "").strip()
    if secret and not STRIPE_SECRET.fullmatch(secret):
        raise ValueError(
            "STRIPE_SECRET_KEY must be a Stripe test-mode secret key (sk_test_...); "
            "ResourcePilot never takes real payments"
        )
    if publishable and not STRIPE_PUBLISHABLE.fullmatch(publishable):
        raise ValueError(
            "STRIPE_PUBLISHABLE_KEY must be a Stripe test-mode publishable key (pk_test_...)"
        )
    if bool(secret) != bool(publishable):
        raise ValueError("Set both STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY, or neither")
    try:
        cents = int(str(values.get("HOUSING_MATCH_PRICE_CENTS", "") or "100"))
    except ValueError:
        raise ValueError("HOUSING_MATCH_PRICE_CENTS must be a whole number of cents") from None
    if not MIN_PRICE_CENTS <= cents <= MAX_PRICE_CENTS:
        raise ValueError(
            f"HOUSING_MATCH_PRICE_CENTS must be between {MIN_PRICE_CENTS} and {MAX_PRICE_CENTS}"
        )
    raw_fet = str(values.get("HOUSING_MATCH_PRICE_FET", "") or "1").strip()
    try:
        fet = Decimal(raw_fet)
    except InvalidOperation:
        raise ValueError("HOUSING_MATCH_PRICE_FET must be a number of FET") from None
    if not (Decimal(0) < fet <= MAX_PRICE_FET) or fet != fet.quantize(Decimal("0.000001")):
        raise ValueError(
            f"HOUSING_MATCH_PRICE_FET must be above 0 and at most {MAX_PRICE_FET}, "
            "with up to six decimals"
        )
    accept_fet = str(values.get("ACCEPT_TESTNET_FET", "") or "").strip().lower() in TRUE_WORDS
    return PaymentConfig(
        price_cents=cents,
        price_fet=format(fet.normalize(), "f"),
        stripe_secret=secret,
        stripe_publishable=publishable,
        accept_fet=accept_fet,
    )


def halls_to_match(payload: Mapping[str, Any]) -> int:
    """How many halls Verification kept; the match is offered only when there is one."""
    return len(dict(payload.get("housing") or {}).get("options") or [])


# ---------------------------------------------------------------------------
# Stripe (test mode only)
# ---------------------------------------------------------------------------


def _stripe_error(status: int) -> ProviderError:
    if status in {401, 403}:
        return ProviderError(f"{STRIPE} rejected the API key; check STRIPE_SECRET_KEY")
    if status == 404:
        return ProviderError(f"{STRIPE} could not find that checkout")
    if status == 429:
        return ProviderError(f"{STRIPE} is rate limiting requests; try again shortly")
    return ProviderError(f"{STRIPE} returned an error ({status})")


async def _stripe(
    http: Http,
    config: PaymentConfig,
    method: str,
    path: str,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One Stripe request with card-safe errors: never the key or a raw response body."""
    try:
        response = await http.client.request(
            method,
            STRIPE_API + path,
            data=data,
            headers={"Authorization": f"Bearer {config.stripe_secret}"},
            timeout=api_timeout(),
            follow_redirects=False,
        )
    except httpx.TimeoutException:
        raise ProviderError(f"{STRIPE} did not respond in time") from None
    except httpx.HTTPError:
        raise ProviderError(f"{STRIPE} could not be reached") from None
    if response.status_code >= 300:
        raise _stripe_error(response.status_code)
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ProviderError(f"{STRIPE} returned more data than ResourcePilot accepts")
    try:
        body = response.json()
    except ValueError:
        raise ProviderError(f"{STRIPE} returned an unexpected response") from None
    if not isinstance(body, dict):
        raise ProviderError(f"{STRIPE} returned an unexpected response")
    return body


def checkout_form(config: PaymentConfig, *, workflow_id: str, school: str) -> dict[str, str]:
    """The Checkout Session body: one line item at the configured price, bound to the workflow."""
    return {
        "ui_mode": "embedded",
        "mode": "payment",
        "redirect_on_completion": "never",
        "payment_method_types[0]": "card",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(config.price_cents),
        "line_items[0][price_data][product_data][name]": PRODUCT_NAME,
        "line_items[0][price_data][product_data][description]": (
            f"Hall-by-hall housing match at {school}"[:200]
        ),
        "client_reference_id": workflow_id,
        "metadata[workflow_id]": workflow_id,
        "metadata[service]": SERVICE,
        "expires_at": str(int(utc_now().timestamp()) + PAY_WINDOW_SECONDS),
    }


async def create_checkout(
    http: Http, config: PaymentConfig, *, workflow_id: str, school: str
) -> dict[str, str]:
    """Create an embedded Checkout Session; return what ASI:One needs to show it."""
    body = await _stripe(
        http,
        config,
        "POST",
        "/v1/checkout/sessions",
        checkout_form(config, workflow_id=workflow_id, school=school),
    )
    session_id = str(body.get("id") or "")
    secret = str(body.get("client_secret") or "")
    if not CHECKOUT_ID.fullmatch(session_id) or not secret or body.get("livemode") is not False:
        raise ProviderError(f"{STRIPE} returned an unexpected checkout")
    return {
        "ui_mode": "embedded",
        "publishable_key": config.stripe_publishable,
        "client_secret": secret,
        "checkout_session_id": session_id,
        "id": session_id,
        "currency": "usd",
        "amount_cents": str(config.price_cents),
    }


def stripe_problem(
    session: Mapping[str, Any], config: PaymentConfig, *, workflow_id: str
) -> str | None:
    """Why this Checkout Session does not pay for this workflow's match, or None when it does."""
    metadata = dict(session.get("metadata") or {})
    if session.get("livemode") is not False:
        return "That checkout is not a Stripe test-mode payment"
    if (
        metadata.get("workflow_id") != workflow_id
        or session.get("client_reference_id") != workflow_id
    ):
        return "That checkout belongs to a different request"
    if session.get("payment_status") != "paid":
        return "Stripe has not marked that checkout as paid yet"
    if (
        str(session.get("currency") or "").lower() != "usd"
        or session.get("amount_total") != config.price_cents
    ):
        return "That checkout was not for the hall match price"
    return None


async def verify_stripe(
    http: Http, config: PaymentConfig, *, session_id: str, workflow_id: str
) -> str | None:
    """Read the Checkout Session from Stripe and check it. Returns the problem, or None."""
    if not CHECKOUT_ID.fullmatch(session_id):
        return "That is not a Stripe test-mode checkout"
    session = await _stripe(http, config, "GET", f"/v1/checkout/sessions/{session_id}")
    if session.get("id") != session_id:
        return "Stripe returned a different checkout"
    return stripe_problem(session, config, workflow_id=workflow_id)


# ---------------------------------------------------------------------------
# FET (Fetch stable testnet only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transfer:
    sender: str
    recipient: str
    denom: str
    amount: int


@dataclass(frozen=True)
class LedgerTx:
    succeeded: bool
    transfers: tuple[Transfer, ...]


def read_testnet_tx(tx_hash: str) -> LedgerTx | None:
    """Read one transaction's bank transfers from the Fetch stable testnet, or None if unknown.

    Blocking; callers run it in a worker thread with a time limit.
    """
    from cosmpy.aerial.client import LedgerClient, NetworkConfig
    from cosmpy.aerial.exceptions import NotFoundError
    from cosmpy.protos.cosmos.bank.v1beta1.tx_pb2 import MsgSend
    from cosmpy.protos.cosmos.tx.v1beta1.service_pb2 import GetTxRequest

    ledger = LedgerClient(NetworkConfig.fetchai_stable_testnet())
    try:
        response = ledger.txs.GetTx(GetTxRequest(hash=tx_hash))
    except NotFoundError:
        return None
    except Exception as exc:
        if "not found" in str(exc).lower():
            return None
        raise
    transfers = []
    for message in response.tx.body.messages:
        if message.type_url != MSG_SEND:
            continue
        send = MsgSend.FromString(message.value)
        for coin in send.amount:
            if coin.amount.isdigit():
                transfers.append(
                    Transfer(send.from_address, send.to_address, coin.denom, int(coin.amount))
                )
    return LedgerTx(succeeded=response.tx_response.code == 0, transfers=tuple(transfers))


# Swapped out in tests; production reads the testnet.
TX_READER: Callable[[str], LedgerTx | None] = read_testnet_tx


def fet_problem(tx: LedgerTx | None, *, buyer: str, wallet: str, price_atto: int) -> str | None:
    """Why this transaction does not pay for the match, or None when it does."""
    if tx is None:
        return f"The {TESTNET} does not show that transaction yet"
    if not tx.succeeded:
        return "That FET transaction failed on the testnet"
    paid = sum(
        transfer.amount
        for transfer in tx.transfers
        if transfer.sender == buyer and transfer.recipient == wallet and transfer.denom == FET_DENOM
    )
    if paid == 0:
        return "That transaction did not send testnet FET from your wallet to ResourcePilot"
    if paid < price_atto:
        return "That transaction sent less than the hall match price"
    return None


async def verify_fet(config: PaymentConfig, *, tx_hash: str, buyer: str, wallet: str) -> str | None:
    """Read the transaction from the testnet and check it. Returns the problem, or None."""
    if not TX_HASH.fullmatch(tx_hash):
        return "That is not a Fetch transaction hash"
    if not FET_WALLET.fullmatch(buyer):
        return "The payment did not name the paying FET wallet"
    try:
        tx = await asyncio.wait_for(
            asyncio.to_thread(TX_READER, tx_hash.upper()), TESTNET_TIMEOUT_SECONDS
        )
    except TimeoutError:
        raise ProviderError(f"The {TESTNET} did not respond in time") from None
    except Exception:
        raise ProviderError(f"The {TESTNET} could not be reached") from None
    return fet_problem(tx, buyer=buyer, wallet=wallet, price_atto=config.price_atto)


# ---------------------------------------------------------------------------
# The payment request
# ---------------------------------------------------------------------------


def payment_request(
    config: PaymentConfig,
    *,
    workflow_id: str,
    wallet: str,
    school: str,
    checkout: dict[str, str] | None,
) -> RequestPayment:
    """One offer: Stripe when a checkout was created, testnet FET when it is accepted."""
    description = f"Unlock your hall-by-hall housing match at {school}"[:200]
    funds: list[Funds] = []
    metadata: dict[str, str | dict[str, str]] = {
        "service": SERVICE,
        "workflow_id": workflow_id,
        "content": description + ". This pays ResourcePilot for the match, never your school.",
    }
    if checkout:
        funds.append(
            Funds(amount=f"{config.price_cents / 100:.2f}", currency="USD", payment_method="stripe")
        )
        metadata["stripe"] = dict(checkout)
    if config.accept_fet:
        funds.append(Funds(amount=config.price_fet, currency="FET", payment_method="fet_direct"))
        metadata["fet_network"] = FET_NETWORK
        metadata["mainnet"] = "false"
        metadata["provider_agent_wallet"] = wallet
    if not funds:
        raise ValueError("No payment method is available")
    return RequestPayment(
        accepted_funds=funds,
        recipient=wallet,
        deadline_seconds=PAY_WINDOW_SECONDS,
        reference=workflow_id,
        description=description,
        metadata=metadata,
    )


def buyer_wallet(metadata: Mapping[str, Any] | None) -> str:
    """The paying wallet ASI:One names on a FET commit."""
    values = dict(metadata or {})
    return str(values.get("buyer_fet_wallet") or values.get("buyer_fet_address") or "").strip()
