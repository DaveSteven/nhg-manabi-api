from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main as app_main
from app.database import Base
from app.models import (
    Attempt,
    AttemptAnswer,
    Book,
    Exam,
    ExamAttempt,
    Option,
    PracticeAttempt,
    Question,
    User,
)
from app.main import ensure_schema
from app.stats import build_overview, build_trend

ANSWER_TABLES = ("attempt_answers", "exam_attempt_answers", "practice_attempt_answers")


def make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def add_user(db: Session, user_id: str = "1") -> User:
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


def _table_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").all()}


def test_created_at_migration_idempotent_and_legacy_rows_stay_null(monkeypatch) -> None:
    """AC3：旧库启动补 created_at 列，重复执行幂等；存量行保持 NULL 不回填。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # 模拟旧库：三张 answer 表尚无 created_at 列，且已有存量作答行
    with engine.begin() as conn:
        for table in ANSWER_TABLES:
            conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN created_at")
        for table in ANSWER_TABLES:
            conn.exec_driver_sql(
                f"INSERT INTO {table} "
                "(attempt_id, user_id, question_id, correct_option_id, is_correct) "
                "VALUES (1, 'legacy-user', 1, 1, 1)"
            )

    monkeypatch.setattr(app_main, "engine", engine)
    with Session(engine) as db:
        ensure_schema(db)  # 第一次：补列
        ensure_schema(db)  # 第二次：幂等

    with engine.connect() as conn:
        for table in ANSWER_TABLES:
            assert "created_at" in _table_columns(conn, table)
            row = conn.exec_driver_sql(f"SELECT created_at FROM {table}").all()
            assert row[0][0] is None  # 存量行保持 NULL，不回填


def test_new_answer_writes_created_at_near_attempt_time() -> None:
    """AC3：新作答由 ORM default 写入逐条真实时间，与 attempt 创建时间接近。"""
    db = make_db()
    user = add_user(db)
    book = Book(
        external_id=1,
        name="Book",
        level="N3",
        subject="vocabulary",
        subject_label="词汇",
    )
    db.add(book)
    db.flush()
    question = Question(
        external_id=1,
        book_id=book.id,
        level="N3",
        subject="vocabulary",
        stem="",
        question_text="?",
        answer_text="A",
    )
    question.options = [
        Option(text="A", is_correct=1, source_order=1),
        Option(text="B", is_correct=0, source_order=2),
    ]
    db.add(question)
    db.flush()

    before = datetime.utcnow()
    attempt = Attempt(
        user_id=user.id,
        mode="book",
        level="N3",
        subject="vocabulary",
        subject_label="词汇",
        title="Practice",
        correct=1,
        total=1,
    )
    attempt.answers = [
        AttemptAnswer(
            user_id=user.id,
            question_id=question.id,
            correct_option_id=question.options[0].id,
            is_correct=1,
        )
    ]
    db.add(attempt)
    db.commit()
    after = datetime.utcnow()

    answer = attempt.answers[0]
    assert answer.created_at is not None
    assert before - timedelta(seconds=1) <= answer.created_at <= after + timedelta(seconds=1)


@pytest.fixture()
def personal_client():
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app as main_app, hash_password
    from app.models import (
        AttemptAnswer,
        ExamAttemptAnswer,
        PracticeAttemptAnswer,
    )

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="1", username="david", display_name="David", password_hash=hash_password("pw")))
    db.add(User(id="2", username="amy", display_name="Amy", password_hash=hash_password("pw")))
    # 教材题 david 2 次（对 1 错 1）；amy 1 次（错）
    db.add_all(
        [
            AttemptAnswer(attempt_id=1, user_id="1", question_id=100, correct_option_id=1, is_correct=1),
            AttemptAnswer(attempt_id=1, user_id="1", question_id=100, correct_option_id=2, is_correct=0),
            AttemptAnswer(attempt_id=2, user_id="2", question_id=100, correct_option_id=2, is_correct=0),
        ]
    )
    # 真题：模考正式提交 1 次对 + 专项练习 2 次（对 1 错 1），合并口径
    db.add_all(
        [
            ExamAttemptAnswer(attempt_id=1, user_id="1", question_id=200, correct_option_id=3, is_correct=1),
            PracticeAttemptAnswer(attempt_id=1, user_id="1", question_id=200, correct_option_id=3, is_correct=1),
            PracticeAttemptAnswer(attempt_id=1, user_id="1", question_id=200, correct_option_id=4, is_correct=0),
        ]
    )
    db.commit()

    main_app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(main_app), db
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        db.close()


def _personal_login(client: TestClient, username: str) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_question_personal_aggregates_textbook_and_exam_sources(personal_client) -> None:
    """AC1：多来源作答与明细一致；只返回作答过的题；其他用户数据隔离。"""
    client, _ = personal_client
    headers = _personal_login(client, "david")

    res = client.get(
        "/api/stats/question-personal", params={"source": "question", "ids": "100,999"},
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json() == {
        "items": {"100": {"my_attempts": 2, "my_correct": 1, "my_accuracy": 50.0}}
    }

    res = client.get(
        "/api/stats/question-personal",
        params={"source": "exam_question", "ids": "200"},
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json() == {
        "items": {"200": {"my_attempts": 3, "my_correct": 2, "my_accuracy": 66.7}}
    }


def test_question_personal_other_user_and_unanswered_isolated(personal_client) -> None:
    """AC1/AC4：他人作答不串数据；未作答 id 不出现在响应。"""
    client, _ = personal_client
    headers = _personal_login(client, "amy")

    res = client.get(
        "/api/stats/question-personal",
        params={"source": "question", "ids": "100"},
        headers=headers,
    )
    assert res.json() == {"items": {"100": {"my_attempts": 1, "my_correct": 0, "my_accuracy": 0.0}}}

    res = client.get(
        "/api/stats/question-personal",
        params={"source": "exam_question", "ids": "200"},
        headers=headers,
    )
    assert res.json() == {"items": {}}


def test_question_personal_ids_validation(personal_client) -> None:
    """AC4：ids 上限 200 超限 422；非法与空 ids 422。"""
    client, _ = personal_client
    headers = _personal_login(client, "david")

    over = client.get(
        "/api/stats/question-personal",
        params={"source": "question", "ids": ",".join(str(i) for i in range(201))},
        headers=headers,
    )
    assert over.status_code == 422

    exact = client.get(
        "/api/stats/question-personal",
        params={"source": "question", "ids": ",".join(str(i) for i in range(200))},
        headers=headers,
    )
    assert exact.status_code == 200

    bad = client.get(
        "/api/stats/question-personal",
        params={"source": "question", "ids": "1,abc"},
        headers=headers,
    )
    assert bad.status_code == 422

    missing = client.get(
        "/api/stats/question-personal",
        params={"source": "question"},
        headers=headers,
    )
    assert missing.status_code == 422
