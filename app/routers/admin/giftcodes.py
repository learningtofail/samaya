"""Gift code redemption runs (spec §79.6). Leaders start a run for the alliances they
can write to; a tick job advances it (services/giftcode_engine.py). Player IDs appear
only in these authenticated responses, scoped to the alliances the caller can see."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Kingdom, Player, RedemptionResult, RedemptionRun, Tenant, User, UserTenant
from services import giftcode_engine as engine
from services.audit import log_change
from services.giftcode_client import FRIENDLY, is_configured

from .deps import check_write_access, get_current_tenants, get_current_user

router = APIRouter()

PROBLEM_LIMIT = 200


class RunIn(BaseModel):
    code: str = Field(max_length=100)
    tenant_ids: list[int] = Field(min_length=1, max_length=50)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _run_dict(run: RedemptionRun, status_counts: dict[str, int]) -> dict:
    return {
        "id": run.id, "code": run.code, "status": run.status, "stop_reason": run.stop_reason,
        "created_at": _iso(run.created_at), "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "counts": engine.summarize(status_counts),
    }


async def _status_counts(db: AsyncSession, run_ids: list[int], tenant_ids: list[int]) -> dict[int, dict[str, int]]:
    rows = (await db.execute(
        select(RedemptionResult.run_id, RedemptionResult.status, func.count())
        .where(RedemptionResult.run_id.in_(run_ids), RedemptionResult.tenant_id.in_(tenant_ids))
        .group_by(RedemptionResult.run_id, RedemptionResult.status)
    )).all()
    out: dict[int, dict[str, int]] = {rid: {} for rid in run_ids}
    for run_id, status, n in rows:
        out[run_id][status] = n
    return out


@router.get("/api/giftcode-config")
async def giftcode_config(user: User = Depends(get_current_user)):
    return {"enabled": is_configured(), "delay_seconds": engine.DELAY_SECONDS}


@router.post("/api/giftcode-runs", status_code=201)
async def start_run(payload: RunIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not is_configured():
        raise HTTPException(status_code=503, detail="Gift code redemption is not configured on this server")
    tenant_ids = list(dict.fromkeys(payload.tenant_ids))
    tenants = (await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))).scalars().all()
    if len(tenants) != len(tenant_ids):
        raise HTTPException(status_code=404, detail="Unknown alliance")
    await check_write_access(db, user, tenant_ids)
    if len({t.kingdom_id for t in tenants}) != 1:
        raise HTTPException(status_code=422, detail="Pick alliances from one Kingdom")
    kingdom = await db.get(Kingdom, tenants[0].kingdom_id)
    now = datetime.now(timezone.utc)
    try:
        run = await engine.create_run(db, kingdom=kingdom, tenant_ids=tenant_ids, raw_code=payload.code,
                                      user_id=user.id, now=now)
    except engine.RunRefused as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    counts = {t.id: 0 for t in tenants}
    for tenant_id, n in (await db.execute(
        select(RedemptionResult.tenant_id, func.count()).where(RedemptionResult.run_id == run.id).group_by(RedemptionResult.tenant_id)
    )).all():
        counts[tenant_id] = n
    for tenant in tenants:
        await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="redemption_runs", row_id=run.id,
                         action="create", after={"code": run.code, "players": counts[tenant.id]})
    await db.commit()
    return _run_dict(run, await _only(db, run.id))


async def _only(db: AsyncSession, run_id: int) -> dict[str, int]:
    rows = (await db.execute(
        select(RedemptionResult.status, func.count()).where(RedemptionResult.run_id == run_id).group_by(RedemptionResult.status)
    )).all()
    return {status: n for status, n in rows}


@router.get("/api/giftcode-runs")
async def list_runs(
    limit: int = 20, tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db),
):
    tenant_ids = [t.id for t in tenants]
    run_ids = (await db.execute(
        select(RedemptionResult.run_id).where(RedemptionResult.tenant_id.in_(tenant_ids)).distinct()
    )).scalars().all()
    if not run_ids:
        return []
    runs = (await db.execute(
        select(RedemptionRun).where(RedemptionRun.id.in_(run_ids)).order_by(RedemptionRun.id.desc()).limit(max(1, min(limit, 50)))
    )).scalars().all()
    counts = await _status_counts(db, [r.id for r in runs], tenant_ids)
    return [_run_dict(r, counts[r.id]) for r in runs]


@router.get("/api/giftcode-runs/{run_id}")
async def get_run(
    run_id: int, tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db),
):
    by_id = {t.id: t for t in tenants}
    run = await db.get(RedemptionRun, run_id)
    counts = (await _status_counts(db, [run_id], list(by_id)))[run_id] if run else {}
    if run is None or not counts:
        raise HTTPException(status_code=404, detail="No such run")
    rows = (await db.execute(
        select(RedemptionResult, Player)
        .outerjoin(Player, Player.id == RedemptionResult.player_id)
        .where(RedemptionResult.run_id == run_id, RedemptionResult.tenant_id.in_(list(by_id)))
        .order_by(RedemptionResult.id)
    )).all()
    wrong_kingdom, problems = [], []
    for result, player in rows:
        group = engine.outcome_group(result.status)
        entry = {"fid": result.fid, "kid": result.kid, "name": player.name if player else None,
                 "alliance": by_id[result.tenant_id].slug, "status": result.status,
                 "message": result.message or FRIENDLY.get(result.status, result.status)}
        if group == "wrong_kingdom":
            wrong_kingdom.append(entry)
        elif group in ("requirement", "other", "code") and len(problems) < PROBLEM_LIMIT:
            problems.append(entry)
    return {**_run_dict(run, counts), "wrong_kingdom": wrong_kingdom, "problems": problems}


@router.post("/api/giftcode-runs/{run_id}/cancel")
async def cancel_run(run_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    run = await db.get(RedemptionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run")
    tenant_ids = list((await db.execute(
        select(RedemptionResult.tenant_id).where(RedemptionResult.run_id == run_id).distinct())).scalars().all())
    if not user.is_superadmin:
        writable = (await db.execute(
            select(UserTenant.tenant_id).where(UserTenant.user_id == user.id, UserTenant.tenant_id.in_(tenant_ids), UserTenant.role != "viewer")
        )).scalars().all()
        if not writable:
            raise HTTPException(status_code=403, detail="You cannot cancel a run for these alliances")
    if run.status not in engine.ACTIVE_RUN:
        raise HTTPException(status_code=409, detail="That run has already finished")
    await engine.cancel_run(db, run, datetime.now(timezone.utc))
    for tenant_id in tenant_ids:
        await log_change(db, user_id=user.id, tenant_id=tenant_id, table_name="redemption_runs", row_id=run.id,
                         action="update", before={"status": "active"}, after={"status": "cancelled"})
    await db.commit()
    return _run_dict(run, await _only(db, run.id))
