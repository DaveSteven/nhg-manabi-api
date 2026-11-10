from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .models import (
    Attempt,
    ExamAttempt,
    ExamAttemptAnswer,
    ExamQuestion,
    PracticeAttempt,
    User,
)


SUBJECT_LABELS = {
    "vocabulary": "词汇",
    "grammar": "语法",
    "reading": "阅读",
    "listening": "听解",
}
SUBJECT_ORDER = tuple(SUBJECT_LABELS)


@dataclass(frozen=True)
class StatRow:
    created_at: datetime
    level: str
    correct: int
    total: int
    kind: str
    subject: str | None = None


def _normalize_subject(value: str | None) -> str | None:
    if value in {"meaning", "kanji", "vocabulary"}:
        return "vocabulary"
    if value in SUBJECT_LABELS:
        return value
    return None


def _exam_subject(question_type: int | None) -> str | None:
    value = question_type or 0
    if value >= 41:
        return "listening"
    if value >= 31:
        return "reading"
    if value >= 21:
        return "grammar"
    if value > 0:
        return "vocabulary"
    return None


def _accuracy(correct: int, total: int) -> float | None:
    return round(correct / total * 100, 1) if total else None


def _collect_rows(db: Session, user_id: str, level: str | None) -> list[StatRow]:
    rows: list[StatRow] = []

    attempt_stmt = select(Attempt).where(Attempt.user_id == user_id)
    practice_stmt = select(PracticeAttempt).where(PracticeAttempt.user_id == user_id)
    exam_stmt = select(ExamAttempt).where(ExamAttempt.user_id == user_id)
    if level:
        attempt_stmt = attempt_stmt.where(Attempt.level == level)
        practice_stmt = practice_stmt.where(PracticeAttempt.level == level)
        exam_stmt = exam_stmt.where(ExamAttempt.level == level)

    rows.extend(
        StatRow(
            created_at=item.created_at,
            level=item.level,
            correct=item.correct,
            total=item.total,
            kind="practice",
            subject=_normalize_subject(item.subject),
        )
        for item in db.scalars(attempt_stmt)
    )
    rows.extend(
        StatRow(
            created_at=item.created_at,
            level=item.level,
            correct=item.correct,
            total=item.total,
            kind="listening" if item.category == "listening" else "practice",
            subject=_normalize_subject(item.category),
        )
        for item in db.scalars(practice_stmt)
    )
    rows.extend(
        StatRow(
            created_at=item.created_at,
            level=item.level,
            correct=item.correct,
            total=item.total,
            kind="exam",
        )
        for item in db.scalars(exam_stmt)
    )
    return rows


def _exam_subject_totals(
    db: Session, user_id: str, level: str | None
) -> dict[str, list[int]]:
    stmt = (
        select(ExamQuestion.question_type, ExamAttemptAnswer.is_correct)
        .join(ExamAttemptAnswer, ExamAttemptAnswer.question_id == ExamQuestion.id)
        .join(ExamAttempt, ExamAttempt.id == ExamAttemptAnswer.attempt_id)
        .where(ExamAttempt.user_id == user_id)
    )
    if level:
        stmt = stmt.where(ExamAttempt.level == level)

    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for question_type, is_correct in db.execute(stmt):
        subject = _exam_subject(question_type)
        if subject:
            totals[subject][0] += int(bool(is_correct))
            totals[subject][1] += 1
    return totals


def _streak(active_dates: set[date], today: date) -> dict:
    if not active_dates:
        return {"current": 0, "longest": 0, "last_active_date": None}

    ordered = sorted(active_dates)
    longest = 1
    run = 1
    for previous, current in zip(ordered, ordered[1:]):
        if current == previous + timedelta(days=1):
            run += 1
            longest = max(longest, run)
        else:
            run = 1

    last_active = ordered[-1]
    current = 0
    if last_active >= today - timedelta(days=1):
        cursor = last_active
        while cursor in active_dates:
            current += 1
            cursor -= timedelta(days=1)
    return {
        "current": current,
        "longest": longest,
        "last_active_date": last_active.isoformat(),
    }


def build_overview(
    db: Session,
    user: User,
    level: str | None = None,
    today: date | None = None,
    all_levels: bool = False,
) -> dict:
    # 学生端默认只看当前级别；admin 端传 all_levels=True 查看全部级别。
    effective_level = None if all_levels else (level or user.current_level)
    rows = _collect_rows(db, user.id, effective_level)
    today = today or datetime.utcnow().date()

    questions_correct = sum(row.correct for row in rows)
    questions_answered = sum(row.total for row in rows)
    active_dates = {row.created_at.date() for row in rows}

    activity_totals: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        activity_totals[row.created_at.date()][0] += row.total
        activity_totals[row.created_at.date()][1] += row.correct
    activity_start = today - timedelta(days=364)
    activity = []
    cursor = activity_start
    while cursor <= today:
        answered, correct = activity_totals[cursor]
        activity.append(
            {"date": cursor.isoformat(), "answered": answered, "correct": correct}
        )
        cursor += timedelta(days=1)

    level_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    subject_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        level_totals[row.level][0] += row.correct
        level_totals[row.level][1] += row.total
        if row.subject:
            subject_totals[row.subject][0] += row.correct
            subject_totals[row.subject][1] += row.total

    for subject, values in _exam_subject_totals(db, user.id, effective_level).items():
        subject_totals[subject][0] += values[0]
        subject_totals[subject][1] += values[1]

    return {
        "totals": {
            "practice_attempts": sum(row.kind == "practice" for row in rows),
            "exam_attempts": sum(row.kind == "exam" for row in rows),
            "listening_attempts": sum(row.kind == "listening" for row in rows),
            "questions_answered": questions_answered,
            "questions_correct": questions_correct,
            "overall_accuracy": _accuracy(questions_correct, questions_answered),
        },
        "streak": _streak(active_dates, today),
        "activity": activity,
        "breakdown_by_level": [
            {
                "level": item_level,
                "correct": values[0],
                "total": values[1],
                "accuracy": _accuracy(values[0], values[1]),
            }
            for item_level, values in sorted(level_totals.items(), reverse=True)
        ],
        "breakdown_by_subject": [
            {
                "subject": subject,
                "label": SUBJECT_LABELS[subject],
                "correct": subject_totals[subject][0],
                "total": subject_totals[subject][1],
                "accuracy": _accuracy(*subject_totals[subject]),
            }
            for subject in SUBJECT_ORDER
        ],
    }


def _period_start(value: date, granularity: str) -> date:
    if granularity == "week":
        return value - timedelta(days=value.weekday())
    if granularity == "month":
        return value.replace(day=1)
    return value


def build_trend(
    db: Session,
    user: User,
    *,
    granularity: str,
    days: int,
    level: str | None = None,
    today: date | None = None,
    all_levels: bool = False,
) -> dict:
    effective_level = None if all_levels else (level or user.current_level)
    today = today or datetime.utcnow().date()
    cutoff = today - timedelta(days=days - 1)
    grouped: dict[date, list[int]] = defaultdict(lambda: [0, 0, 0])
    for row in _collect_rows(db, user.id, effective_level):
        row_date = row.created_at.date()
        if cutoff <= row_date <= today:
            period = _period_start(row_date, granularity)
            grouped[period][0] += row.total
            grouped[period][1] += row.correct
            grouped[period][2] += 1
    return {
        "points": [
            {
                "period": period.isoformat(),
                "answered": values[0],
                "correct": values[1],
                "accuracy": _accuracy(values[1], values[0]),
                "attempts": values[2],
            }
            for period, values in sorted(grouped.items())
        ]
    }


def create_stats_router(current_user_dependency: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/stats", tags=["stats"])

    @router.get("/overview")
    def stats_overview(
        level: str | None = None,
        db: Session = Depends(get_db),
        user: User = Depends(current_user_dependency),
    ) -> dict:
        return build_overview(db, user, level)

    @router.get("/trend")
    def stats_trend(
        metric: Literal["accuracy", "volume"] = "accuracy",
        granularity: Literal["day", "week", "month"] = "day",
        days: int = Query(90, ge=1, le=3660),
        level: str | None = None,
        db: Session = Depends(get_db),
        user: User = Depends(current_user_dependency),
    ) -> dict:
        # Both metrics share the same points; metric controls presentation on the client.
        _ = metric
        return build_trend(
            db,
            user,
            granularity=granularity,
            days=days,
            level=level,
        )

    return router
