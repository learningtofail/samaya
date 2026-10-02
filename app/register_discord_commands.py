"""Registers Samaya's slash commands with Discord (spec §70).

Run inside the container after any change to services/discord_commands.COMMANDS:

    docker compose run --rm app python register_discord_commands.py          # dry run
    docker compose run --rm app python register_discord_commands.py --apply  # send

This overwrites the application's whole global command list with COMMANDS, so
a command created by hand in the developer portal is removed. The dry run
prints what would be sent and changes nothing.
"""
import argparse
import json
import os
import sys

import httpx

from services.discord_api import DISCORD_API_BASE
from services.discord_commands import COMMANDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="send the commands to Discord (default: dry run)")
    args = parser.parse_args(argv)

    app_id = os.environ.get("DISCORD_OAUTH_CLIENT_ID", "")
    token = os.environ.get("PLATFORM_BOT_TOKEN", "")
    print(json.dumps(COMMANDS, indent=2))
    if not args.apply:
        print(f"\nDry run: {len(COMMANDS)} commands. Nothing sent. Add --apply to register them.")
        return 0
    if not app_id or not token:
        print("DISCORD_OAUTH_CLIENT_ID and PLATFORM_BOT_TOKEN must both be set.", file=sys.stderr)
        return 1
    response = httpx.put(
        f"{DISCORD_API_BASE}/applications/{app_id}/commands",
        headers={"Authorization": f"Bot {token}"}, json=COMMANDS, timeout=30,
    )
    if response.status_code != 200:
        print(f"Discord answered {response.status_code}: {response.text}", file=sys.stderr)
        return 1
    print(f"\nRegistered {len(response.json())} commands.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
