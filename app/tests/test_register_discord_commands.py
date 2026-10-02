"""The registration script registers under the bot token's own application,
not the OAuth login application (spec §70)."""
import httpx
import pytest

import register_discord_commands as script


@pytest.fixture
def discord(monkeypatch):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.setdefault("calls", []).append((request.method, request.url.path))
        if request.url.path.endswith("/oauth2/applications/@me"):
            return httpx.Response(200, json={"id": "999"})
        return httpx.Response(200, json=[{}, {}, {}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(script.httpx, "get", lambda url, **kw: client.get(url, **kw))
    monkeypatch.setattr(script.httpx, "put", lambda url, **kw: client.put(url, **kw))
    monkeypatch.setenv("PLATFORM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("DISCORD_OAUTH_CLIENT_ID", "111")
    monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)
    return sent


def test_registers_under_the_bots_application(discord, capsys):
    assert script.main(["--apply"]) == 0
    assert ("PUT", "/api/v10/applications/999/commands") in discord["calls"]
    assert "application 999" in capsys.readouterr().out


def test_an_explicit_application_id_wins(discord, monkeypatch):
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "555")
    assert script.main(["--apply"]) == 0
    assert ("PUT", "/api/v10/applications/555/commands") in discord["calls"]


def test_a_dry_run_sends_nothing(discord, capsys):
    assert script.main([]) == 0
    assert "calls" not in discord and "Dry run" in capsys.readouterr().out


def test_a_rejected_token_stops_before_registering(monkeypatch, capsys):
    monkeypatch.setenv("PLATFORM_BOT_TOKEN", "bad")
    monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    monkeypatch.setattr(script.httpx, "get", lambda url, **kw: client.get(url, **kw))
    assert script.main(["--apply"]) == 1
    assert "did not accept" in capsys.readouterr().err
