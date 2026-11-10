from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Attempt, Exam, ExamAttempt, PracticeAttempt, User
from app.stats import build_overview, build_trend


def make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def add_user(db: Session, user_id: int = 1) -> User:
    user = User(
        id=user_id,
        username=f"learner-{user_id}",
        display_name="Learner",
        current_level="N3",
    )
    db.add(user)
    db.commit()
    return user


def test_overview_aggregates_three_attempt_sources_and_streak() -> None:
    db = make_db()
    user = add_user(db)
    exam = Exam(external_id="exam-1", level="N3", title="N3 Exam")
    db.add(exam)
    db.flush()
    db.add_all(
        [
            Attempt(
                user_id=user.id,
                mode="book",
                level="N3",
                subject="meaning",
                subject_label="词汇",
                title="Practice 1",
                correct=8,
                total=10,
                created_at=datetime(2026, 11, 8, 12),
            ),
            PracticeAttempt(
                user_id=user.id,
                level="N3",
                category="grammar",
                title="Grammar",
                correct=6,
                total=10,
                created_at=datetime(2026, 11, 9, 12),
            ),
            ExamAttempt(
                user_id=user.id,
                exam_id=exam.id,
                level="N3",
                title="N3 Exam",
                correct=15,
                total=20,
                created_at=datetime(2026, 11, 10, 12),
            ),
        ]
    )
    db.commit()

    result = build_overview(db, user, today=date(2026, 11, 10))

    assert result["totals"] == {
        "practice_attempts": 2,
        "exam_attempts": 1,
        "listening_attempts": 0,
        "questions_answered": 40,
        "questions_correct": 29,
        "overall_accuracy": 72.5,
    }
    assert result["streak"]["current"] == 3
    assert result["streak"]["longest"] == 3
    assert result["activity"][-1] == {
        "date": "2026-11-10",
        "answered": 20,
        "correct": 15,
    }
    assert result["breakdown_by_subject"][0]["accuracy"] == 80.0


def test_overview_for_new_user_returns_empty_state() -> None:
    db = make_db()
    user = add_user(db)

    result = build_overview(db, user, today=date(2026, 11, 10))

    assert result["totals"]["questions_answered"] == 0
    assert result["totals"]["overall_accuracy"] is None
    assert result["streak"] == {
        "current": 0,
        "longest": 0,
        "last_active_date": None,
    }
    assert len(result["activity"]) == 365
    assert all(item["accuracy"] is None for item in result["breakdown_by_subject"])


def test_trend_groups_attempts_by_week_and_month_without_overlap() -> None:
    db = make_db()
    user = add_user(db)
    for created_at, correct, total in [
        (datetime(2026, 9, 30, 8), 5, 10),
        (datetime(2026, 10, 1, 8), 8, 10),
        (datetime(2026, 10, 5, 8), 9, 10),
    ]:
        db.add(
            Attempt(
                user_id=user.id,
                mode="book",
                level="N3",
                subject="grammar",
                subject_label="语法",
                title="Practice",
                correct=correct,
                total=total,
                created_at=created_at,
            )
        )
    db.commit()

    monthly = build_trend(
        db,
        user,
        granularity="month",
        days=60,
        today=date(2026, 10, 10),
    )
    weekly = build_trend(
        db,
        user,
        granularity="week",
        days=60,
        today=date(2026, 10, 10),
    )

    assert [point["period"] for point in monthly["points"]] == [
        "2026-09-01",
        "2026-10-01",
    ]
    assert monthly["points"][1]["answered"] == 20
    assert [point["period"] for point in weekly["points"]] == [
        "2026-09-28",
        "2026-10-05",
    ]
