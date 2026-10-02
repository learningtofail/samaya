"""Spec §67.7: create, update, cancel and send all wait out a 429 (capped) and
stop after the last attempt; a non-JSON 429 body no longer raises."""
import httpx
import pytest

from services import discord_api


@pytest.fixture
def waits(monkeypatch):
    recorded: list[float] = []

    async def fake_sleep(seconds):
        recorded.append(seconds)
    monkeypatch.setattr(discord_api.asyncio, "sleep", fake_sleep)
    return recorded


def _client_returning(monkeypatch, responses: list[httpx.Response]):
    calls: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    real = httpx.AsyncClient
    monkeypatch.setattr(discord_api.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    return calls


def _429(**kw):
    return httpx.Response(429, **kw)


class TestCancel:
    async def test_retries_after_the_wait_discord_asks_for(self, monkeypatch, waits):
        calls = _client_returning(monkeypatch, [_429(json={"retry_after": 1.5}), httpx.Response(204)])
        ok, error = await discord_api.cancel_discord_event("t", "g", "e")
        assert (ok, error) == (True, "") and len(calls) == 2 and waits == [1.5]

    async def test_a_non_json_429_uses_the_retry_after_header(self, monkeypatch, waits):
        _client_returning(monkeypatch, [_429(text="<html>rate limited</html>", headers={"Retry-After": "3"}), httpx.Response(204)])
        ok, _ = await discord_api.cancel_discord_event("t", "g", "e")
        assert ok and waits == [3.0]

    async def test_a_non_json_429_without_a_header_backs_off(self, monkeypatch, waits):
        _client_returning(monkeypatch, [_429(text="slow down"), httpx.Response(204)])
        ok, _ = await discord_api.cancel_discord_event("t", "g", "e")
        assert ok and waits == [1.0]

    async def test_each_wait_is_capped(self, monkeypatch, waits):
        _client_returning(monkeypatch, [_429(json={"retry_after": 600}), httpx.Response(204)])
        await discord_api.cancel_discord_event("t", "g", "e")
        assert waits == [discord_api.MAX_RETRY_WAIT]

    async def test_it_gives_up_after_the_last_attempt(self, monkeypatch, waits):
        calls = _client_returning(monkeypatch, [_429(json={"retry_after": 1})])
        ok, error = await discord_api.cancel_discord_event("t", "g", "e")
        assert not ok and error.startswith("429") and len(calls) == discord_api.MAX_RETRIES
        assert len(waits) == discord_api.MAX_RETRIES - 1

    async def test_a_404_still_means_already_gone(self, monkeypatch, waits):
        _client_returning(monkeypatch, [httpx.Response(404)])
        ok, error = await discord_api.cancel_discord_event("t", "g", "e")
        assert not ok and error.startswith("404") and waits == []

    async def test_a_server_error_is_retried(self, monkeypatch, waits):
        calls = _client_returning(monkeypatch, [httpx.Response(502), httpx.Response(204)])
        ok, _ = await discord_api.cancel_discord_event("t", "g", "e")
        assert ok and len(calls) == 2


class TestSendAndCreate:
    async def test_send_survives_a_non_json_429(self, monkeypatch, waits):
        calls = _client_returning(monkeypatch, [_429(text="nope", headers={"Retry-After": "2"}), httpx.Response(200, json={})])
        ok, _ = await discord_api.send_channel_message("t", "c", "hi")
        assert ok and len(calls) == 2 and waits == [2.0]

    async def test_create_waits_and_retries(self, monkeypatch, waits):
        from datetime import datetime, timezone
        _client_returning(monkeypatch, [_429(json={"retry_after": 2}), httpx.Response(200, json={"id": "77"})])
        start = datetime(2026, 10, 9, 19, tzinfo=timezone.utc)
        discord_id, error = await discord_api.create_discord_event(
            token="t", guild_id="g", name="n", start=start, end=start.replace(hour=21), description="", location="")
        assert (discord_id, error) == ("77", "") and waits == [2.0]


async def test_the_cutover_script_paces_its_deletes():
    from cutover_delete_old_discord_events import INTER_CALL_DELAY
    assert INTER_CALL_DELAY >= 0.5


class TestBadRequestDetail:
    BODY = {"code": 50035, "message": "Invalid Form Body", "errors": {
        "description": {"_errors": [{"code": "BASE_TYPE_MAX_LENGTH", "message": "Must be 1000 or fewer in length."}]},
        "entity_metadata": {"location": {"_errors": [{"code": "X", "message": "Required"}]}},
    }}

    async def test_create_names_the_rejected_fields(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(400, json=self.BODY)])
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        event_id, error = await discord_api.create_discord_event("t", "g", "n", now, now, "d", "l")
        assert event_id == ""
        assert error == ("400 Invalid Form Body (description: Must be 1000 or fewer in length.; "
                         "entity_metadata.location: Required)")

    async def test_a_non_json_400_still_returns_an_error(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(400, text="<html>nope</html>")])
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        _, error = await discord_api.create_discord_event("t", "g", "n", now, now, "d", "l")
        assert error == "400 Bad request"

    async def test_description_and_location_are_cut_to_discords_limits(self, monkeypatch):
        calls = _client_returning(monkeypatch, [httpx.Response(200, json={"id": "9"})])
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        await discord_api.create_discord_event("t", "g", "n", now, now, "x" * 1500, "y" * 300)
        import json
        sent = json.loads(calls[0].content)
        assert len(sent["description"]) == discord_api.EVENT_DESCRIPTION_MAX and sent["description"].endswith("…")
        assert len(sent["entity_metadata"]["location"]) == discord_api.EVENT_LOCATION_MAX
