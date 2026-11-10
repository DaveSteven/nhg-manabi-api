from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Callable

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import get_db
from .models import Attempt, ExamAttempt, PracticeAttempt, User
from .schemas import (
    AdminSummaryOut,
    AdminSummaryTotalsOut,
    AdminTrendPointOut,
)

ACTIVITY_TREND_DAYS = 30


def _active_cutoff(today: date) -> datetime:
    return datetime(today.year, today.month, today.day) - timedelta(days=6)


def build_summary(db: Session, today: date | None = None) -> dict:
    """后台 dashboard 全局汇总：用户规模、7 日活跃/新增、30 日注册与做题趋势。"""
    today = today or datetime.utcnow().date()
    week_ago = _active_cutoff(today)
    month_ago = today - timedelta(days=ACTIVITY_TREND_DAYS - 1)

    total_users = db.scalar(select(func.count(User.id))) or 0
    total_admins = db.scalar(
        select(func.count(User.id)).where(User.is_admin.is_(True))
    ) or 0
    active_users_7d = db.scalar(
        select(func.count(User.id)).where(
            User.last_active_at.is_not(None), User.last_active_at >= week_ago
        )
    ) or 0
    new_users_7d = db.scalar(
        select(func.count(User.id)).where(User.created_at >= week_ago)
    ) or 0

    answered_total = 0
    answered_7d = 0
    activity_by_day: dict[date, int] = defaultdict(int)
    for table in (Attempt, PracticeAttempt, ExamAttempt):
        answered_total += db.scalar(select(func.coalesce(func.sum(table.total), 0))) or 0
        answered_7d += (
            db.scalar(
                select(func.coalesce(func.sum(table.total), 0)).where(
                    table.created_at >= week_ago
                )
            )
            or 0
        )
        rows = db.execute(
            select(table.created_at, table.total).where(table.created_at >= month_ago)
        ).all()
        for created_at, total in rows:
            activity_by_day[created_at.date()] += total

    registration_by_day: dict[date, int] = defaultdict(int)
    rows = db.execute(
        select(User.created_at).where(User.created_at >= month_ago)
    ).all()
    for (created_at,) in rows:
        registration_by_day[created_at.date()] += 1

    def daily_points(counts: dict[date, int]) -> list[AdminTrendPointOut]:
        points = []
        cursor = month_ago
        while cursor <= today:
            points.append(
                AdminTrendPointOut(period=cursor.isoformat(), count=counts.get(cursor, 0))
            )
            cursor += timedelta(days=1)
        return points

    return AdminSummaryOut(
        totals=AdminSummaryTotalsOut(
            users=total_users,
            admins=total_admins,
            active_users_7d=active_users_7d,
            new_users_7d=new_users_7d,
            questions_answered=answered_total,
            questions_answered_7d=answered_7d,
        ),
        registration_trend=daily_points(registration_by_day),
        activity_trend=daily_points(activity_by_day),
    ).model_dump()


def create_admin_stats_router(admin_dependency: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/admin/stats", tags=["admin-stats"])

    @router.get("/summary")
    def admin_stats_summary(
        db: Session = Depends(get_db),
        admin: User = Depends(admin_dependency),
    ) -> dict:
        _ = admin
        return build_summary(db)

    return router
