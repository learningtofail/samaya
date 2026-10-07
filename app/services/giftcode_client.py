"""Client for the game's gift code redemption endpoint (spec §79.4, §79.5).

The protocol facts (URL, signing rule, error codes) come from the community
script github.com/justncodes/ks-giftcode v2.0.0 and are unverified against the
live API. This is a clean reimplementation, not a copy. It sends an honest
User-Agent and does not rotate headers to avoid bot detection: if the server
blocks the client, the run stops (`BLOCKED`) and a person decides what next.

The signing key is configuration (KS_GIFTCODE_SIGN_KEY), never committed. It was
extracted from the game's web client and can change without notice. Everything
here takes an injected `httpx.AsyncClient` and sleep function, so tests never
touch the network.
"""
import asyncio
import hashlib
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

DEFAULT_BASE_URL = "https://kingshot-giftcode.centurygame.com"
DEFAULT_USER_AGENT = "Samaya/1.0 (Kingshot alliance scheduler)"

MAX_TRANSPORT_ATTEMPTS = 3
TRANSPORT_RETRY_SECONDS = 2.0

# Final outcome keys stored on RedemptionResult.status, with the text a leader reads.
FRIENDLY = {
    "SUCCESS": "Redeemed",
    "RECEIVED": "Already redeemed",
    "TIME ERROR": "Code has expired",
    "CDK NOT FOUND": "Code not found or incorrect",
    "USED": "Claim limit reached",
    "TOO FREQUENT": "Rate limited on this player ID",
    "TIMEOUT RETRY": "The server kept asking for a retry",
    "USER INFO ERROR": "Wrong kingdom for this player ID",
    "ROLE NOT EXIST": "No such player",
    "STOVE_LV ERROR": "Town Center level too low for this code",
    "RECHARGE_MONEY ERROR": "Not enough spending for this code",
    "RECHARGE_MONEY_VIP ERROR": "VIP level too low for this code",
    "SIGN ERROR": "The server rejected the request signature",
    "NOT LOGIN": "The server rejected the session",
    "BLOCKED": "The server refused this client",
    "TRANSPORT": "Could not reach the server",
    "UNKNOWN": "Unrecognised answer from the server",
}

# A bad code is bad for everyone, so these stop the whole run.
FATAL_KEYS = {"TIME ERROR": "code_expired", "CDK NOT FOUND": "code_invalid", "USED": "claim_limit"}
# Keys that suggest the API changed under us (spec §79.2 decision 8).
DRIFT_KEYS = frozenset({"SIGN ERROR", "NOT LOGIN", "UNKNOWN"})

# (msg, err_code) pairs the reference script recognises. SAME TYPE EXCHANGE is a success.
_BY_MESSAGE = {
    ("RECEIVED", 40008): "RECEIVED",
    ("SAME TYPE EXCHANGE", 40011): "SUCCESS",
    ("TIME ERROR", 40007): "TIME ERROR",
    ("CDK NOT FOUND", 40014): "CDK NOT FOUND",
    ("USED", 40005): "USED",
    ("TIMEOUT RETRY", 40004): "TIMEOUT RETRY",
    ("TOO FREQUENT", 40019): "TOO FREQUENT",
    ("USER INFO ERROR", 40020): "USER INFO ERROR",
    ("STOVE_LV ERROR", 40006): "STOVE_LV ERROR",
    ("RECHARGE_MONEY ERROR", 40017): "RECHARGE_MONEY ERROR",
    ("RECHARGE_MONEY_VIP ERROR", 40018): "RECHARGE_MONEY_VIP ERROR",
}


@dataclass(frozen=True)
class RedeemResult:
    key: str
    message: str


def is_configured() -> bool:
    """The feature is off until a signing key is set (spec §79.2 decision 7)."""
    return bool(os.environ.get("KS_GIFTCODE_SIGN_KEY"))


def sign(payload: dict[str, str], key: str) -> str:
    """Lowercase hex MD5 of the fields sorted by name and joined as `k=v&k=v`, followed by the key."""
    joined = "&".join(f"{name}={payload[name]}" for name in sorted(payload))
    return hashlib.md5(f"{joined}{key}".encode()).hexdigest()


def classify(data) -> RedeemResult:
    """Map an API answer to an outcome key. Pure, so every row of the spec's table is a test."""
    if not isinstance(data, dict):
        return RedeemResult("UNKNOWN", "The answer was not a JSON object")
    msg = str(data.get("msg", "")).strip().strip(".")
    err_code = data.get("err_code")
    if msg == "SUCCESS":
        return RedeemResult("SUCCESS", FRIENDLY["SUCCESS"])
    key = _BY_MESSAGE.get((msg, err_code))
    if key is None and err_code == 40001 and "not exist" in msg.lower():
        key = "ROLE NOT EXIST"
    if key is None and "sign error" in msg.lower():
        key = "SIGN ERROR"
    if key is None and msg == "NOT LOGIN":
        key = "NOT LOGIN"
    if key is None:
        return RedeemResult("UNKNOWN", f"{FRIENDLY['UNKNOWN']}: {msg[:120] or 'empty message'}")
    return RedeemResult(key, FRIENDLY[key])


class GiftcodeClient:
    def __init__(
        self, http: httpx.AsyncClient, *, sign_key: str, base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.time,
    ):
        self._http = http
        self._key = sign_key
        self._base = base_url.rstrip("/")
        self._user_agent = user_agent
        self._sleep = sleep
        self._clock = clock

    def _headers(self) -> dict[str, str]:
        # Origin and Referer name the site the endpoint belongs to, as a form post from its own page would.
        return {"Accept": "application/json", "User-Agent": self._user_agent,
                "Origin": self._base, "Referer": self._base + "/"}

    async def redeem(self, fid: str, kid: int, code: str) -> RedeemResult:
        """One signed redemption. Retries transport trouble (429, 502, 503, 504, timeouts) a few
        times; a 401 or 403 is not retried and returns BLOCKED."""
        payload = {"fid": str(fid), "cdk": code, "kid": str(kid), "time": str(int(self._clock()))}
        form = {"sign": sign(payload, self._key), **payload}
        last = "no attempt was made"
        for attempt in range(MAX_TRANSPORT_ATTEMPTS):
            try:
                response = await self._http.post(
                    f"{self._base}/api/gift_code", data=form, headers=self._headers(),
                    timeout=httpx.Timeout(30.0, connect=10.0))
            except httpx.HTTPError as exc:
                last = f"{exc.__class__.__name__}"
            else:
                status = response.status_code
                if status == 200:
                    try:
                        return classify(response.json())
                    except ValueError:
                        return RedeemResult("UNKNOWN", "The answer was not valid JSON")
                if status in (401, 403):
                    return RedeemResult("BLOCKED", f"{FRIENDLY['BLOCKED']} (HTTP {status})")
                last = f"HTTP {status}"
                if status not in (429, 502, 503, 504):
                    return RedeemResult("TRANSPORT", f"{FRIENDLY['TRANSPORT']} ({last})")
            if attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                await self._sleep(TRANSPORT_RETRY_SECONDS * (attempt + 1))
        return RedeemResult("TRANSPORT", f"{FRIENDLY['TRANSPORT']} ({last})")


def build_client(http: httpx.AsyncClient) -> GiftcodeClient:
    """A client from the environment. Call only when is_configured()."""
    return GiftcodeClient(
        http, sign_key=os.environ["KS_GIFTCODE_SIGN_KEY"],
        base_url=os.environ.get("KS_GIFTCODE_BASE_URL") or DEFAULT_BASE_URL,
        user_agent=os.environ.get("KS_GIFTCODE_USER_AGENT") or DEFAULT_USER_AGENT,
    )
