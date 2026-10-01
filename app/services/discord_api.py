import asyncio
import logging
from datetime import datetime, timezone

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
    image: str | None = None,
) -> tuple[str, str]:
    """
    Creates a Discord Scheduled Event.
    Returns (discord_event_id, error_message).
    discord_event_id is empty string on failure.

    image (spec §33), when given, is the event's cover image as the full
    data URI EventDefinition.cover_image_data already stores ("data:image/
    png;base64,...") — exactly the format Discord's own API expects for
    this field, so it's passed straight through with no re-encoding.
    Omitted entirely rather than sent as null/empty when there's no image,
    matching how this payload already omits fields Discord doesn't need.
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
    if image:
        payload["image"] = image

    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/scheduled-events"

    async with httpx.AsyncClient() as client:
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.post(url, headers=_auth_headers(token), json=payload)
            except httpx.RequestError as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.warning(f"create_discord_event: network error in guild {guild_id} after {MAX_RETRIES} attempts — {e}")
                return "", f"Network error: {e}"

            if response.status_code in (200, 201):
                data = response.json()
                discord_id = str(data.get("id", ""))
                logger.info(f"create_discord_event: created {discord_id} in guild {guild_id} ({name!r})")
                return discord_id, ""

            if response.status_code == 401:
                logger.warning(f"create_discord_event: 401 in guild {guild_id} — token invalid or bot removed")
                return "", "401 Unauthorized — token invalid or bot removed"

            if response.status_code == 403:
                logger.warning(f"create_discord_event: 403 in guild {guild_id} — bot missing MANAGE_EVENTS")
                return "", "403 Forbidden — bot missing MANAGE_EVENTS permission"

            if response.status_code == 400:
                detail = response.json().get("message", "Bad request")
                logger.warning(f"create_discord_event: 400 in guild {guild_id} — {detail}")
                return "", f"400 {detail}"

            if response.status_code == 429:
                retry_after = response.json().get("retry_after", 2 ** attempt)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(float(retry_after))
                    continue
                logger.warning(f"create_discord_event: 429 rate limited in guild {guild_id} after {MAX_RETRIES} attempts")
                return "", "429 Rate limited — retry later"

            if response.status_code >= 500:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2)
                    continue
                logger.warning(f"create_discord_event: {response.status_code} Discord server error in guild {guild_id}")
                return "", f"{response.status_code} Discord server error"

            logger.warning(f"create_discord_event: unexpected HTTP {response.status_code} in guild {guild_id}")
            return "", f"Unexpected HTTP {response.status_code}"

    logger.warning(f"create_discord_event: max retries exceeded in guild {guild_id}")
    return "", "Max retries exceeded"


async def update_discord_event(
    token:      str,
    guild_id:   str,
    discord_event_id: str,
    name:        str,
    description: str,
    location:    str,
    image:       str | None = None,
    start:       datetime | None = None,
    end:         datetime | None = None,
) -> tuple[bool, str]:
    """
    Updates a Discord Scheduled Event's name/description/location to match
    the current event definition. Returns (success, error_message).

    start/end (spec §66.4a, moving or editing one occurrence) are included
    only when given, so every existing caller keeps its old behavior.

    Shares create_discord_event's retry/backoff and error-code handling —
    this used to be reimplemented ad hoc at each call site with a bare
    single-attempt httpx.patch and no retry on 429/5xx.

    image (spec §33) is only included when explicitly passed — omitted
    means "leave whatever image Discord already has," not "remove it,"
    since discord_sync.py's callers only ever push the fields they've
    actually diffed against Discord's own state, and this app doesn't
    currently diff cover images (see spec §33's Out of Scope).
    """
    payload = {
        "name":            name,
        "description":     description or "",
        "entity_metadata": {"location": location or "Community Server"},
    }
    if image:
        payload["image"] = image
    if start is not None:
        payload["scheduled_start_time"] = start.astimezone(timezone.utc).isoformat()
    if end is not None:
        payload["scheduled_end_time"] = end.astimezone(timezone.utc).isoformat()
    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/scheduled-events/{discord_event_id}"

    async with httpx.AsyncClient() as client:
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.patch(url, headers=_auth_headers(token), json=payload)
            except httpx.RequestError as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.warning(f"update_discord_event: network error for {discord_event_id} in guild {guild_id} after {MAX_RETRIES} attempts — {e}")
                return False, f"Network error: {e}"

            if response.status_code in (200, 201):
                logger.info(f"update_discord_event: updated {discord_event_id} in guild {guild_id}")
                return True, ""

            if response.status_code == 401:
                logger.warning(f"update_discord_event: 401 for {discord_event_id} in guild {guild_id} — token invalid or bot removed")
                return False, "401 Unauthorized — token invalid or bot removed"

            if response.status_code == 403:
                logger.warning(f"update_discord_event: 403 for {discord_event_id} in guild {guild_id} — bot missing MANAGE_EVENTS")
                return False, "403 Forbidden — bot missing MANAGE_EVENTS permission"

            if response.status_code == 404:
                logger.warning(f"update_discord_event: 404 for {discord_event_id} in guild {guild_id} — may already be cancelled")
                return False, "404 Event not found — may already be cancelled"

            if response.status_code == 400:
                detail = response.json().get("message", "Bad request")
                logger.warning(f"update_discord_event: 400 for {discord_event_id} in guild {guild_id} — {detail}")
                return False, f"400 {detail}"

            if response.status_code == 429:
                retry_after = response.json().get("retry_after", 2 ** attempt)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(float(retry_after))
                    continue
                logger.warning(f"update_discord_event: 429 rate limited for {discord_event_id} in guild {guild_id} after {MAX_RETRIES} attempts")
                return False, "429 Rate limited — retry later"

            if response.status_code >= 500:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2)
                    continue
                logger.warning(f"update_discord_event: {response.status_code} Discord server error for {discord_event_id} in guild {guild_id}")
                return False, f"{response.status_code} Discord server error"

            logger.warning(f"update_discord_event: unexpected HTTP {response.status_code} for {discord_event_id} in guild {guild_id}")
            return False, f"Unexpected HTTP {response.status_code}"

    logger.warning(f"update_discord_event: max retries exceeded for {discord_event_id} in guild {guild_id}")
    return False, "Max retries exceeded"


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
            logger.warning(f"cancel_discord_event: network error for {discord_event_id} in guild {guild_id} — {e}")
            return False, f"Network error: {e}"

    if response.status_code in (200, 204):
        logger.info(f"cancel_discord_event: cancelled {discord_event_id} in guild {guild_id}")
        return True, ""
    if response.status_code == 404:
        logger.warning(f"cancel_discord_event: 404 for {discord_event_id} in guild {guild_id} — may already be cancelled")
        return False, "404 Event not found — may already be cancelled"
    if response.status_code == 401:
        logger.warning(f"cancel_discord_event: 401 for {discord_event_id} in guild {guild_id}")
        return False, "401 Unauthorized"
    if response.status_code == 403:
        logger.warning(f"cancel_discord_event: 403 for {discord_event_id} in guild {guild_id}")
        return False, "403 Forbidden"
    logger.warning(f"cancel_discord_event: unexpected HTTP {response.status_code} for {discord_event_id} in guild {guild_id}")
    return False, f"HTTP {response.status_code}"


async def get_guild_events(token: str, guild_id: str) -> list[dict]:
    """Fetches all upcoming scheduled events for the guild."""
    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/scheduled-events"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=_auth_headers(token))
            if response.status_code == 200:
                return response.json()
            logger.warning(f"get_guild_events: HTTP {response.status_code} for guild {guild_id}")
        except httpx.RequestError as e:
            # Previously swallowed with a bare `pass` — a network failure
            # here silently looked identical to "this guild genuinely has
            # no scheduled events," which is what let auto_post.py treat a
            # transient Discord outage as license to (re-)create events
            # Discord already had, with nothing in the logs to explain why.
            logger.warning(f"get_guild_events: network error for guild {guild_id} — {e}")
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
            logger.warning(f"get_guild_channels: network error for guild {guild_id} — {e}")
            return [], f"Network error: {e}"

    if response.status_code == 200:
        channels = [c for c in response.json() if c.get("type") == 0]
        channels.sort(key=lambda c: c.get("position", 0))
        return [{"id": c["id"], "name": c["name"]} for c in channels], ""
    if response.status_code == 401:
        logger.warning(f"get_guild_channels: 401 for guild {guild_id}")
        return [], "401 Unauthorized — token invalid or bot removed"
    if response.status_code == 403:
        logger.warning(f"get_guild_channels: 403 for guild {guild_id}")
        return [], "403 Forbidden — bot missing access to channels"
    logger.warning(f"get_guild_channels: unexpected HTTP {response.status_code} for guild {guild_id}")
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
            logger.warning(f"get_guild_roles: network error for guild {guild_id} — {e}")
            return [], f"Network error: {e}"

    if response.status_code == 200:
        roles = response.json()
        return [{"id": r["id"], "name": r["name"], "color": r.get("color", 0)} for r in roles], ""
    if response.status_code == 401:
        logger.warning(f"get_guild_roles: 401 for guild {guild_id}")
        return [], "401 Unauthorized — token invalid or bot removed"
    if response.status_code == 403:
        logger.warning(f"get_guild_roles: 403 for guild {guild_id}")
        return [], "403 Forbidden — bot missing access to roles"
    logger.warning(f"get_guild_roles: unexpected HTTP {response.status_code} for guild {guild_id}")
    return [], f"HTTP {response.status_code}"


async def get_guild_info(token: str, guild_id: str) -> tuple[dict | None, str]:
    """
    Fetches the guild's own display name — distinct from our `Tenant.name`
    (the alliance's name in our system), which several tenants can share
    a Discord server without sharing (see MOD/NSR's shared guild_id).
    Returns (info, error_message). info is None on failure.
    """
    url = f"{DISCORD_API_BASE}/guilds/{guild_id}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=_auth_headers(token))
        except httpx.RequestError as e:
            logger.warning(f"get_guild_info: network error for guild {guild_id} — {e}")
            return None, f"Network error: {e}"

    if response.status_code == 200:
        g = response.json()
        return {"id": g["id"], "name": g["name"]}, ""
    if response.status_code == 401:
        logger.warning(f"get_guild_info: 401 for guild {guild_id}")
        return None, "401 Unauthorized — token invalid or bot removed"
    if response.status_code == 403:
        logger.warning(f"get_guild_info: 403 for guild {guild_id}")
        return None, "403 Forbidden — bot missing access to this guild"
    logger.warning(f"get_guild_info: unexpected HTTP {response.status_code} for guild {guild_id}")
    return None, f"HTTP {response.status_code}"


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
                logger.warning(f"send_channel_message: network error for channel {channel_id} after {MAX_RETRIES} attempts — {e}")
                return False, f"Network error: {e}"

            if response.status_code in (200, 201):
                return True, ""
            if response.status_code == 401:
                logger.warning(f"send_channel_message: 401 for channel {channel_id}")
                return False, "401 Unauthorized"
            if response.status_code == 403:
                logger.warning(f"send_channel_message: 403 for channel {channel_id} — bot missing Send Messages")
                return False, "403 Missing permissions — bot needs Send Messages permission in the channel"
            if response.status_code == 404:
                logger.warning(f"send_channel_message: 404 for channel {channel_id} — check notification_channel_id")
                return False, "404 Channel not found — check notification_channel_id"
            if response.status_code == 429:
                retry_after = response.json().get("retry_after", 2 ** attempt)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(float(retry_after))
                    continue
                logger.warning(f"send_channel_message: 429 rate limited for channel {channel_id} after {MAX_RETRIES} attempts")
                return False, "429 Rate limited"
            logger.warning(f"send_channel_message: unexpected HTTP {response.status_code} for channel {channel_id}")
            return False, f"HTTP {response.status_code}"
    logger.warning(f"send_channel_message: max retries exceeded for channel {channel_id}")
    return False, "Max retries exceeded"
