"""Unit tests for services/discord_oauth.py — the app-level "Login with
Discord" OAuth2 flow (audit remediation, Phase 4). test_auth.py already
covers the full HTTP invite-claim round-trip, but it monkeypatches
exchange_code_for_token()/get_discord_identity() wholesale at the router
level (see that file's own docstring on why), so this module's actual
httpx-calling internals — token-exchange failure handling, non-200
responses, malformed identity payloads — were never independently under
test. Follows the same monkeypatch-httpx.AsyncClient-directly style
test_event_cover_image.py's TestDiscordApiImageField uses.
"""
import httpx

from services.discord_oauth import (
    build_authorize_url,
    exchange_code_for_token,
    get_discord_identity,
)


class TestBuildAuthorizeUrl:

    def test_includes_state_and_configured_client_id(self, monkeypatch):
        monkeypatch.setattr("services.discord_oauth.CLIENT_ID", "my-client-id")
        monkeypatch.setattr("services.discord_oauth.REDIRECT_URI", "https://ks138.taraka.dev/auth/callback")

        url = build_authorize_url("csrf-state-123")

        assert url.startswith("https://discord.com/api/oauth2/authorize?")
        assert "client_id=my-client-id" in url
        assert "state=csrf-state-123" in url
        assert "response_type=code" in url
        assert "scope=identify" in url


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


class TestExchangeCodeForToken:

    async def test_success_returns_access_token_and_no_error(self, monkeypatch):
        async def fake_post(self, url, data=None, headers=None):
            assert data["grant_type"] == "authorization_code"
            assert data["code"] == "the-auth-code"
            return _FakeResponse(200, {"access_token": "the-access-token"})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        token, error = await exchange_code_for_token("the-auth-code")

        assert token == "the-access-token"
        assert error == ""

    async def test_non_200_response_returns_empty_token_and_error(self, monkeypatch):
        async def fake_post(self, url, data=None, headers=None):
            return _FakeResponse(400, {"error": "invalid_grant"}, text="invalid_grant")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        token, error = await exchange_code_for_token("stale-code")

        assert token == ""
        assert "400" in error
        assert "invalid_grant" in error

    async def test_network_error_returns_empty_token_and_error(self, monkeypatch):
        async def fake_post(self, url, data=None, headers=None):
            raise httpx.ConnectTimeout("connection timed out")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        token, error = await exchange_code_for_token("any-code")

        assert token == ""
        assert "connection timed out" in error

    async def test_missing_access_token_key_returns_empty_string_not_keyerror(self, monkeypatch):
        # Discord returning 200 with an unexpected body shape shouldn't
        # raise — callers only ever branch on whether error == "".
        async def fake_post(self, url, data=None, headers=None):
            return _FakeResponse(200, {"unexpected": "shape"})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        token, error = await exchange_code_for_token("any-code")

        assert token == ""
        assert error == ""


class TestGetDiscordIdentity:

    async def test_success_returns_id_and_username(self, monkeypatch):
        async def fake_get(self, url, headers=None):
            assert headers["Authorization"] == "Bearer the-access-token"
            return _FakeResponse(200, {"id": "123456789", "username": "someuser"})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        identity, error = await get_discord_identity("the-access-token")

        assert identity == {"id": "123456789", "username": "someuser"}
        assert error == ""

    async def test_non_200_response_returns_empty_dict_and_error(self, monkeypatch):
        async def fake_get(self, url, headers=None):
            return _FakeResponse(401, {"message": "401: Unauthorized"}, text="401: Unauthorized")

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        identity, error = await get_discord_identity("expired-token")

        assert identity == {}
        assert "401" in error

    async def test_network_error_returns_empty_dict_and_error(self, monkeypatch):
        async def fake_get(self, url, headers=None):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        identity, error = await get_discord_identity("any-token")

        assert identity == {}
        assert "connection refused" in error

    async def test_missing_fields_default_to_empty_string(self, monkeypatch):
        async def fake_get(self, url, headers=None):
            return _FakeResponse(200, {})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        identity, error = await get_discord_identity("any-token")

        assert identity == {"id": "", "username": ""}
        assert error == ""
