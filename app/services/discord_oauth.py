"""Discord OAuth2 — "Login with Discord". Separate from services/discord_api.py
on purpose: that file is a bot-token-authenticated REST client (creating
events, posting messages); this is the app-level OAuth2 flow (client_id/
client_secret, authorization codes), a genuinely different Discord auth
mechanism, not the same concern split arbitrarily.
"""
import os

import httpx

DISCORD_OAUTH_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_OAUTH_TOKEN_URL     = "https://discord.com/api/oauth2/token"
DISCORD_API_BASE            = "https://discord.com/api/v10"

CLIENT_ID     = os.environ.get("DISCORD_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DISCORD_OAUTH_CLIENT_SECRET", "")
REDIRECT_URI  = os.environ.get("DISCORD_OAUTH_REDIRECT_URI", "")


def build_authorize_url(state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
        "prompt":        "none",
    }
    return f"{DISCORD_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> tuple[str, str]:
    """Returns (access_token, error). access_token is empty on failure."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                DISCORD_OAUTH_TOKEN_URL,
                data={
                    "client_id":     CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as e:
            return "", str(e)

    if resp.status_code != 200:
        return "", f"{resp.status_code} {resp.text}"
    return resp.json().get("access_token", ""), ""


async def get_discord_identity(access_token: str) -> tuple[dict, str]:
    """Returns ({"id": ..., "username": ...}, error). Empty dict on failure."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{DISCORD_API_BASE}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as e:
            return {}, str(e)

    if resp.status_code != 200:
        return {}, f"{resp.status_code} {resp.text}"
    data = resp.json()
    return {"id": data.get("id", ""), "username": data.get("username", "")}, ""
