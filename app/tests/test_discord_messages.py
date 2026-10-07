"""Spec §82/§83: the message calls that use ids. Unknown Message (deleted) is told apart from Unknown Channel."""
import httpx
import pytest

from services import discord_api


def _client_returning(monkeypatch, responses: list[httpx.Response]):
    calls: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    real = httpx.AsyncClient
    monkeypatch.setattr(discord_api.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    return calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(seconds):
        return None
    monkeypatch.setattr(discord_api.asyncio, "sleep", instant)


class TestPost:
    async def test_returns_the_message_id(self, monkeypatch):
        calls = _client_returning(monkeypatch, [httpx.Response(200, json={"id": "987"})])
        assert await discord_api.post_channel_message("t", "c1", "hi") == ("987", "")
        assert calls[0].method == "POST" and calls[0].url.path.endswith("/channels/c1/messages")

    async def test_no_mentions_sends_an_empty_parse_list(self, monkeypatch):
        calls = _client_returning(monkeypatch, [httpx.Response(200, json={"id": "1"})])
        await discord_api.post_channel_message("t", "c1", "@everyone", no_mentions=True)
        assert b'"allowed_mentions"' in calls[0].content and b'"parse":[]' in calls[0].content.replace(b" ", b"")

    async def test_403_names_the_permissions(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(403, json={"code": 50013})])
        message_id, error = await discord_api.post_channel_message("t", "c1", "hi")
        assert message_id == "" and error.startswith("403")

    async def test_a_reply_without_an_id_is_an_error(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(200, json={})])
        message_id, error = await discord_api.post_channel_message("t", "c1", "hi")
        assert message_id == "" and "message id" in error

    async def test_waits_out_a_429(self, monkeypatch):
        calls = _client_returning(monkeypatch, [httpx.Response(429, json={"retry_after": 1}), httpx.Response(200, json={"id": "5"})])
        assert (await discord_api.post_channel_message("t", "c1", "hi"))[0] == "5" and len(calls) == 2


class TestEdit:
    async def test_ok(self, monkeypatch):
        calls = _client_returning(monkeypatch, [httpx.Response(200, json={"id": "9"})])
        assert await discord_api.edit_channel_message("t", "c1", "9", "new") == (True, "")
        assert calls[0].method == "PATCH" and calls[0].url.path.endswith("/channels/c1/messages/9")

    async def test_a_deleted_message_is_reported_as_gone(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(404, json={"code": 10008, "message": "Unknown Message"})])
        assert await discord_api.edit_channel_message("t", "c1", "9", "new") == (False, discord_api.MESSAGE_GONE)

    async def test_a_missing_channel_is_not_reported_as_gone(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(404, json={"code": 10003, "message": "Unknown Channel"})])
        ok, error = await discord_api.edit_channel_message("t", "c1", "9", "new")
        assert not ok and error != discord_api.MESSAGE_GONE and error.startswith("404")

    async def test_a_non_json_404_is_not_gone(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(404, text="<html>")])
        ok, error = await discord_api.edit_channel_message("t", "c1", "9", "new")
        assert not ok and error != discord_api.MESSAGE_GONE


class TestDelete:
    async def test_ok(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(204)])
        assert await discord_api.delete_channel_message("t", "c1", "9") == (True, "")

    async def test_an_already_deleted_message_counts_as_deleted(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(404, json={"code": 10008})])
        assert await discord_api.delete_channel_message("t", "c1", "9") == (True, "")

    async def test_403_is_an_error(self, monkeypatch):
        _client_returning(monkeypatch, [httpx.Response(403, json={"code": 50013})])
        ok, error = await discord_api.delete_channel_message("t", "c1", "9")
        assert not ok and "Manage Messages" in error

    async def test_network_failure_is_an_error(self, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("down")
        real = httpx.AsyncClient
        monkeypatch.setattr(discord_api.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(boom)))
        ok, error = await discord_api.delete_channel_message("t", "c1", "9")
        assert not ok and error.startswith("Network error")
