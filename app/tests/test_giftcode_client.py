"""Spec §79.4, §79.5: signing, the outcome table and transport handling. No network."""
import hashlib
from urllib.parse import parse_qs

import httpx
import pytest

from services import giftcode_client as gc
from services.giftcode_client import GiftcodeClient, classify, sign


def test_sign_is_md5_of_sorted_fields_then_key():
    payload = {"time": "1760000000", "fid": "12345678", "kid": "138", "cdk": "ABC123"}
    expected = hashlib.md5(b"cdk=ABC123&fid=12345678&kid=138&time=1760000000secretkey").hexdigest()
    assert sign(payload, "secretkey") == expected


@pytest.mark.parametrize("data, key", [
    ({"msg": "SUCCESS", "err_code": 20000}, "SUCCESS"),
    ({"msg": "SAME TYPE EXCHANGE.", "err_code": 40011}, "SUCCESS"),
    ({"msg": "RECEIVED.", "err_code": 40008}, "RECEIVED"),
    ({"msg": "TIME ERROR.", "err_code": 40007}, "TIME ERROR"),
    ({"msg": "CDK NOT FOUND.", "err_code": 40014}, "CDK NOT FOUND"),
    ({"msg": "USED.", "err_code": 40005}, "USED"),
    ({"msg": "TIMEOUT RETRY.", "err_code": 40004}, "TIMEOUT RETRY"),
    ({"msg": "TOO FREQUENT.", "err_code": 40019}, "TOO FREQUENT"),
    ({"msg": "USER INFO ERROR.", "err_code": 40020}, "USER INFO ERROR"),
    ({"msg": "Role does not exist", "err_code": 40001}, "ROLE NOT EXIST"),
    ({"msg": "STOVE_LV ERROR.", "err_code": 40006}, "STOVE_LV ERROR"),
    ({"msg": "RECHARGE_MONEY ERROR.", "err_code": 40017}, "RECHARGE_MONEY ERROR"),
    ({"msg": "RECHARGE_MONEY_VIP ERROR.", "err_code": 40018}, "RECHARGE_MONEY_VIP ERROR"),
    ({"msg": "Sign Error", "err_code": 1}, "SIGN ERROR"),
    ({"msg": "NOT LOGIN", "err_code": 40000}, "NOT LOGIN"),
])
def test_every_known_answer_classifies(data, key):
    assert classify(data).key == key


def test_the_error_code_must_match_the_message():
    assert classify({"msg": "RECEIVED.", "err_code": 99999}).key == "UNKNOWN"


@pytest.mark.parametrize("data", [{"msg": "something new", "err_code": 1}, {}, [], "text", None])
def test_anything_else_is_unknown(data):
    assert classify(data).key == "UNKNOWN"


def _client(handler, sleeps=None):
    async def sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GiftcodeClient(http, sign_key="k", base_url="https://example.test/", sleep=sleep, clock=lambda: 1760000000)


async def test_a_request_is_signed_and_honest():
    seen = {}

    def handler(request):
        seen["body"] = parse_qs(request.content.decode())
        seen["headers"] = request.headers
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"msg": "SUCCESS", "err_code": 20000})

    result = await _client(handler).redeem("12345678", 138, "ABC123")
    assert result.key == "SUCCESS"
    assert seen["url"] == "https://example.test/api/gift_code"
    body = {k: v[0] for k, v in seen["body"].items()}
    assert body["sign"] == sign({k: v for k, v in body.items() if k != "sign"}, "k")
    assert (body["fid"], body["kid"], body["cdk"], body["time"]) == ("12345678", "138", "ABC123", "1760000000")
    assert seen["headers"]["user-agent"] == gc.DEFAULT_USER_AGENT
    assert "sec-ch-ua" not in seen["headers"]


async def test_429_is_retried_then_succeeds():
    answers = iter([httpx.Response(429), httpx.Response(200, json={"msg": "RECEIVED.", "err_code": 40008})])
    sleeps = []
    result = await _client(lambda r: next(answers), sleeps).redeem("12345678", 138, "ABC123")
    assert result.key == "RECEIVED"
    assert sleeps == [2.0]


async def test_persistent_503_ends_as_transport_after_three_attempts():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    result = await _client(handler).redeem("12345678", 138, "ABC123")
    assert (result.key, len(calls)) == ("TRANSPORT", 3)


async def test_a_timeout_is_a_transport_failure_not_a_crash():
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    assert (await _client(handler).redeem("12345678", 138, "ABC123")).key == "TRANSPORT"


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refusal_is_not_retried(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status)

    result = await _client(handler).redeem("12345678", 138, "ABC123")
    assert (result.key, len(calls)) == ("BLOCKED", 1)


async def test_a_400_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400)

    assert (await _client(handler).redeem("12345678", 138, "ABC123")).key == "TRANSPORT"
    assert len(calls) == 1


async def test_invalid_json_is_unknown():
    assert (await _client(lambda r: httpx.Response(200, text="<html>")).redeem("12345678", 138, "X1Y2Z3")).key == "UNKNOWN"


def test_feature_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv("KS_GIFTCODE_SIGN_KEY", raising=False)
    assert gc.is_configured() is False
    monkeypatch.setenv("KS_GIFTCODE_SIGN_KEY", "x")
    assert gc.is_configured() is True
