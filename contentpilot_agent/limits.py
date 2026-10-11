"""Serialize per-owner admission on both PostgreSQL and SQLite."""

import time

from fastapi import HTTPException
from sqlalchemy import func, select, update

from .database import User


def lock_owner(session, owner_id):
    # A harmless UPDATE obtains a row lock on PG, a write lock on SQLite.
    # Call before any task reads to avoid count-then-insert races.
    if not session.execute(
        update(User).where(User.id == owner_id, User.active.is_(True)).values(active=True)
    ).rowcount:
        raise HTTPException(401, "账号已停用，请重新登录")


def enforce_limits(session, model, owner_id, active_limit, daily_limit, enabled, *, new=True):
    if not enabled:
        raise HTTPException(503, "管理员已暂停新增 AI 任务，请稍后重试")
    active = session.scalar(
        select(func.count())
        .select_from(model)
        .where(model.owner_id == owner_id, model.status.in_(["queued", "running"]))
    )
    if active >= active_limit:
        raise HTTPException(429, f"待执行任务已达上限（{active_limit} 个）")
    if new:
        daily = session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.owner_id == owner_id, model.created_at >= time.time() - 86400)
        )
        if daily >= daily_limit:
            raise HTTPException(429, f"最近 24 小时任务已达上限（{daily_limit} 个）")
