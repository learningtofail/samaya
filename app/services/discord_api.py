import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
INTER_CALL_DELAY = 0.5  # seconds between API calls
MAX_RETRIES = 3


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


async def create_discord_event(
    token: str,
    guild_id: str,
    name: str,
    start: datetime,
    end: datetime,
    description: str,
    location: str,
) -> tuple[str, str]:
    """
    Creates a Discord Scheduled Event.
    Returns (discord_event_id, error_message).
    discord_event_id is empty string on failure.
    """
    payload = {
        "name": name,
        "scheduled_start_time": start.astimezone(timezone.utc).isoformat(),
        "scheduled_end_time": end.astimezone(timezone.utc).isoformat(),
        "description": description or "",
        "entity_type": 3,  # EXTERNAL
        "entity_metadata": {"location": location or "Community Server"},
        "privacy_level": 2,  # GUILD_ONLY
    }

    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/scheduled-events"

    async with httpx.AsyncClient() as client:
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.post(url, headers=_auth_headers(token), json=payload)
            except httpx.RequestError as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return "", f"Network error: {e}"

            if response.status_code in (200, 201):
                data = response.json()
                return str(data.get("id", "")), ""

            if response.status_code == 401:
                return "", "401 Unauthorized — token invalid or bot removed"

            if response.status_code == 403:
                return "", "403 Forbidden — bot missing MANAGE_EVENTS permission"

            if response.status_code == 400:
                detail = response.json().get("message", "Bad request")
                return "", f"400 {detail}"

            if response.status_code == 429:
                retry_after = response.json().get("retry_after", 2 ** attempt)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(float(retry_after))
                    continue
                return "", "429 Rate limited — retry later"

            if response.status_code >= 500:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2)
                    continue
                return "", f"{response.status_code} Discord server error"

            return "", f"Unexpected HTTP {response.status_code}"

    return "", "Max retries exceeded"


async def cancel_discord_event(
    token: str,
    guild_id: str,
    discord_event_id: str,
) -> tuple[bool, str]:
    """
    Deletes a Discord Scheduled Event.
    Returns (success, error_message).
    """
    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/scheduled-events/{discord_event_id}"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.delete(url, headers=_auth_headers(token))
        except httpx.RequestError as e:
            return False, f"Network error: {e}"

    if response.status_code in (200, 204):
        return True, ""
    if response.status_code == 404:
        return False, "404 Event not found — may already be cancelled"
    if response.status_code == 401:
        return False, "401 Unauthorized"
    if response.status_code == 403:
        return False, "403 Forbidden"
    return False, f"HTTP {response.status_code}"


async def get_guild_events(token: str, guild_id: str) -> list[dict]:
    """Fetches all upcoming scheduled events for the guild."""
    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/scheduled-events"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=_auth_headers(token))
            if response.status_code == 200:
                return response.json()
        except httpx.RequestError:
            pass
    return []


async def get_guild_channels(token: str, guild_id: str) -> tuple[list[dict], str]:
    """
    Fetches text channels (type=0) for the guild, sorted by position.
    Returns (channels, error_message). channels is [] on failure.
    """
    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/channels"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=_auth_headers(token))
        except httpx.RequestError as e:
            return [], f"Network error: {e}"

    if response.status_code == 200:
        channels = [c for c in response.json() if c.get("type") == 0]
        channels.sort(key=lambda c: c.get("position", 0))
        return [{"id": c["id"], "name": c["name"]} for c in channels], ""
    if response.status_code == 401:
        return [], "401 Unauthorized — token invalid or bot removed"
    if response.status_code == 403:
        return [], "403 Forbidden — bot missing access to channels"
    return [], f"HTTP {response.status_code}"


async def get_guild_roles(token: str, guild_id: str) -> tuple[list[dict], str]:
    """
    Fetches roles for the guild.
    Returns (roles, error_message). roles is [] on failure.
    """
    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/roles"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=_auth_headers(token))
        except httpx.RequestError as e:
            return [], f"Network error: {e}"

    if response.status_code == 200:
        roles = response.json()
        return [{"id": r["id"], "name": r["name"], "color": r.get("color", 0)} for r in roles], ""
    if response.status_code == 401:
        return [], "401 Unauthorized — token invalid or bot removed"
    if response.status_code == 403:
        return [], "403 Forbidden — bot missing access to roles"
    return [], f"HTTP {response.status_code}"


async def verify_token(token: str) -> tuple[bool, str]:
    """Calls GET /users/@me to confirm the token is valid."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{DISCORD_API_BASE}/users/@me",
                headers=_auth_headers(token)
            )
            if response.status_code == 200:
                username = response.json().get("username", "unknown")
                return True, username
            return False, f"HTTP {response.status_code}"
        except httpx.RequestError as e:
            return False, str(e)


async def send_channel_message(
    token:      str,
    channel_id: str,
    content:    str,
) -> tuple[bool, str]:
    """
    Sends a message to a Discord text channel.
    Returns (success, error_message).
    """
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    async with httpx.AsyncClient() as client:
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.post(
                    url,
                    headers=_auth_headers(token),
                    json={"content": content},
                )
            except httpx.RequestError as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return False, f"Network error: {e}"

            if response.status_code in (200, 201):
                return True, ""
            if response.status_code == 401:
                return False, "401 Unauthorized"
            if response.status_code == 403:
                return False, "403 Missing permissions — bot needs Send Messages permission in the channel"
            if response.status_code == 404:
                return False, "404 Channel not found — check notification_channel_id"
            if response.status_code == 429:
                retry_after = response.json().get("retry_after", 2 ** attempt)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(float(retry_after))
                    continue
                return False, "429 Rate limited"
            return False, f"HTTP {response.status_code}"
    return False, "Max retries exceeded"
