"""Re-encode images already stored before services/images.py existed (spec §71.4).

Event covers become bounded JPEGs and alliance icons bounded square PNGs, the
same result a fresh upload gets. Dry run by default. Inside the app container:

    docker compose exec app python reprocess_images.py            # report only
    docker compose exec app python reprocess_images.py --apply    # rewrite

A stored image that cannot be decoded is reported and left untouched. An image
that is already normal is skipped, so a second run changes nothing. Take an
ops/backup.sh backup before --apply.
"""
import asyncio
import sys

from sqlalchemy import select

from models import AsyncSessionLocal
from models.db import Event, Tenant
from services.images import EVENT_COVER, TENANT_ICON, ImageProfile, ImageRejected, process_data_uri

TARGETS = (
    (Event, "cover_image_data", EVENT_COVER, "event"),
    (Tenant, "icon_image_data", TENANT_ICON, "alliance"),
)


def reprocess_value(value: str, profile: ImageProfile) -> str | None:
    """The normalised URI, or None when the image is unreadable. Raises nothing."""
    try:
        return process_data_uri(value, profile)
    except ImageRejected as exc:
        print(f"    unreadable: {exc}")
        return None


async def main(apply: bool) -> int:
    saved = changed = unreadable = 0
    async with AsyncSessionLocal() as session:
        for model, column, profile, noun in TARGETS:
            rows = (await session.execute(
                select(model).where(getattr(model, column).is_not(None), getattr(model, column) != "")
            )).scalars().all()
            for row in rows:
                before = getattr(row, column)
                print(f"  {noun} {row.id} {row.name!r}")
                after = reprocess_value(before, profile)
                if after is None:
                    unreadable += 1
                    continue
                if after == before:
                    continue
                changed += 1
                saved += len(before) - len(after)
                print(f"    {len(before):,} -> {len(after):,} characters")
                if apply:
                    setattr(row, column, after)
        if apply:
            await session.commit()
    verb = "Rewrote" if apply else "Would rewrite"
    print(f"{verb} {changed} images, {saved:,} characters saved, {unreadable} unreadable.")
    if not apply:
        print("Dry run. Re-run with --apply to write the changes.")
    return 1 if unreadable else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--apply" in sys.argv)))
