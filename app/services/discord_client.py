"""The Discord client the delivery engine uses (spec §66.4).

A FastAPI dependency so tests substitute a fake with
`app.dependency_overrides[get_discord] = lambda: fake`. The real client is
the services.discord_api module itself: the engine only needs its five
functions, so no wrapper class is added.
"""
from services import discord_api


def get_discord():
    return discord_api
