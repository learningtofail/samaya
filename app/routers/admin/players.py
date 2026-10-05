"""The player registry (spec §80.3): the one list of player IDs every player-facing
feature reads. Leaders write it, scoped to alliances they can access. Nothing here
is public, and the game gives Samaya no way to verify who owns an ID."""
import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Kingdom, Player, RedemptionResult, Tenant, User
from services.audit import log_change
from services.player_import import MAX_KID, NAME_MAX, TooManyLines, clean_name, parse_player_lines

from .deps import check_write_access, get_current_tenants, get_current_user, require_not_viewer

router = APIRouter()

ALLIANCE_CAP = 2000
NOTE_MAX = 40


class BulkPlayersIn(BaseModel):
    text: str = Field(max_length=100_000)


class PlayerPatch(BaseModel):
    kid: Optional[int] = Field(default=None, ge=1, le=MAX_KID)  # explicit null clears the override
    name: Optional[str] = Field(default=None, max_length=NAME_MAX)
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX)
    tenant_slug: Optional[str] = None  # moves the player to another alliance


def _player_dict(p: Player, tenant: Tenant, game_number: int | None) -> dict:
    return {
        "id": p.id, "fid": p.fid, "kid": p.kid, "effective_kid": p.kid or game_number,
        "name": p.name, "note": p.note, "tenant_slug": tenant.slug, "tenant_name": tenant.name,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


async def _scoped_player(db: AsyncSession, player_id: int, tenant: Tenant) -> Player:
    player = await db.get(Player, player_id)
    if player is None or player.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="No such player in this alliance")
    return player


@router.get("/api/players")
async def list_players(tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db)):
    by_id = {t.id: t for t in tenants}
    kingdoms = {t.kingdom_id: (await db.get(Kingdom, t.kingdom_id)).game_number for t in tenants}
    players = (await db.execute(select(Player).where(Player.tenant_id.in_(by_id)).order_by(Player.tenant_id, Player.id))).scalars().all()
    rows = [_player_dict(p, by_id[p.tenant_id], kingdoms[by_id[p.tenant_id].kingdom_id]) for p in players]
    return {"players": rows, "missing_kingdom": sum(1 for r in rows if r["effective_kid"] is None),
            "game_numbers": {t.slug: kingdoms[t.kingdom_id] for t in tenants}}


@router.post("/api/players/bulk")
async def add_players(
    payload: BulkPlayersIn, tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    try:
        parsed = parse_player_lines(payload.text)
    except TooManyLines as e:
        raise HTTPException(status_code=422, detail=str(e))

    fids = [e.fid for e in parsed.entries]
    existing = {p.fid: p for p in (await db.execute(
        select(Player).where(Player.kingdom_id == tenant.kingdom_id, Player.fid.in_(fids))
    )).scalars().all()} if fids else {}
    total = (await db.execute(select(func.count()).select_from(Player).where(Player.tenant_id == tenant.id))).scalar_one()

    added = updated = duplicates = 0
    elsewhere: list[str] = []
    rejected = [{"line": n, "reason": r} for n, r in parsed.rejected]
    for entry in parsed.entries:
        current = existing.get(entry.fid)
        if current is not None:
            if current.tenant_id != tenant.id:
                elsewhere.append(entry.fid)
                continue
            changed = False
            if entry.kid is not None and entry.kid != current.kid:
                current.kid, changed = entry.kid, True
            if entry.name is not None and entry.name != current.name:
                current.name, changed = entry.name, True
            if changed:
                current.updated_at = func.now()
                updated += 1
            else:
                duplicates += 1
            continue
        if total + added >= ALLIANCE_CAP:
            rejected.append({"line": 0, "reason": f"This alliance is at the limit of {ALLIANCE_CAP} players"})
            break
        db.add(Player(kingdom_id=tenant.kingdom_id, tenant_id=tenant.id, fid=entry.fid, kid=entry.kid,
                      name=entry.name, created_by=user.id))
        added += 1

    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="players", row_id=None, action="create",
                     after={"added": added, "updated": updated, "duplicates": duplicates,
                            "in_other_alliance": len(elsewhere), "rejected": len(rejected)})
    await db.commit()
    return {"added": added, "updated": updated, "duplicates": duplicates + parsed.repeated,
            "in_other_alliance": elsewhere, "rejected": rejected}


@router.get("/api/players/export")
async def export_players(
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """CSV of one alliance's players (fid, kid, name). Audited with a count, never the IDs."""
    kingdom = await db.get(Kingdom, tenant.kingdom_id)
    players = (await db.execute(select(Player).where(Player.tenant_id == tenant.id).order_by(Player.id))).scalars().all()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["fid", "kid", "name", "alliance"])
    for p in players:
        name = p.name or ""
        if name[:1] in ("=", "+", "-", "@"):
            name = "'" + name  # keep a spreadsheet from running a typed name as a formula
        writer.writerow([p.fid, p.kid or kingdom.game_number or "", name, tenant.slug])
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="player_exports", row_id=None, action="create",
                     after={"count": len(players)})
    await db.commit()
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="players-{tenant.slug}.csv"', "Cache-Control": "no-store"})


@router.patch("/api/players/{player_id}")
async def update_player(
    player_id: int, payload: PlayerPatch, tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    player = await _scoped_player(db, player_id, tenant)
    sent = payload.model_fields_set
    changed: dict = {}
    if "kid" in sent:
        player.kid, changed["kid"] = payload.kid, payload.kid
    if "name" in sent:
        player.name = clean_name(payload.name or "") or None
        changed["name"] = bool(player.name)
    if "note" in sent:
        player.note = clean_name(payload.note or "") or None
        changed["note"] = bool(player.note)
    destination = tenant
    if payload.tenant_slug and payload.tenant_slug != tenant.slug:
        destination = (await db.execute(select(Tenant).where(Tenant.slug == payload.tenant_slug))).scalar_one_or_none()
        if destination is None or destination.kingdom_id != tenant.kingdom_id:
            raise HTTPException(status_code=404, detail="No such alliance in this Kingdom")
        await check_write_access(db, user, [destination.id])
        player.tenant_id = destination.id
        changed["moved_to"] = destination.slug
    player.updated_at = func.now()
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="players", row_id=player.id, action="update",
                     before={"alliance": tenant.slug}, after=changed)
    await db.commit()
    await db.refresh(player)
    kingdom = await db.get(Kingdom, destination.kingdom_id)
    return _player_dict(player, destination, kingdom.game_number)


@router.delete("/api/players/{player_id}", status_code=204)
async def delete_player(
    player_id: int, tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    player = await _scoped_player(db, player_id, tenant)
    await db.execute(delete(RedemptionResult).where(RedemptionResult.player_id == player.id))
    await db.delete(player)
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="players", row_id=player_id, action="delete",
                     before={"alliance": tenant.slug})
    await db.commit()
