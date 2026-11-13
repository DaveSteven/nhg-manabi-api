from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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
    ExamAttemptAnswer,
    ExamQuestion,
    Option,
    PracticeAttempt,
    Question,
    User,
)
from app.main import ensure_schema
from app.stats import build_efficiency, build_exam_performance, build_overview, build_trend

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


def _add_exam_with_questions(db: Session, external_id: str = "exam-t13") -> Exam:
    exam = Exam(external_id=external_id, level="N2", title="2019年12月 N2 真题")
    db.add(exam)
    db.flush()
    # question_type 覆盖三大块：1=词汇 25=语法 35=读解 45=听解
    db.add_all(
        [
            ExamQuestion(
                external_id=f"{external_id}-q{t}",
                exam_id=exam.id,
                layer_id=1,
                order_index=i,
                level="N2",
                question_type=t,
            )
            for i, t in enumerate([1, 25, 35, 45])
        ]
    )
    db.flush()
    return exam


def _add_exam_attempt(
    db: Session,
    exam: Exam,
    user_id: str,
    created_at: datetime,
    is_correct_by_type: dict[int, int],
    level: str | None = None,
) -> ExamAttempt:
    attempt = ExamAttempt(
        user_id=user_id,
        exam_id=exam.id,
        level=level or exam.level,
        title=exam.title,
        correct=sum(is_correct_by_type.values()),
        total=len(is_correct_by_type),
        created_at=created_at,
    )
    db.add(attempt)
    db.flush()
    questions = {
        q.question_type: q for q in db.scalars(
            select(ExamQuestion).where(ExamQuestion.exam_id == exam.id)
        )
    }
    db.add_all(
        [
            ExamAttemptAnswer(
                attempt_id=attempt.id,
                user_id=user_id,
                question_id=questions[q_type].id,
                correct_option_id=1,
                is_correct=is_correct,
            )
            for q_type, is_correct in is_correct_by_type.items()
        ]
    )
    db.flush()
    return attempt


def test_exam_performance_curve_and_sections() -> None:
    db = make_db()
    user = add_user(db, "t13")
    user.current_level = "N2"
    exam = _add_exam_with_questions(db)
    # 同一套真题重考两次：第一次 2/4，第二次 3/4（重考计入多次）
    _add_exam_attempt(
        db, exam, user.id, datetime(2026, 11, 11, 10), {1: 1, 25: 0, 35: 1, 45: 0}
    )
    _add_exam_attempt(
        db, exam, user.id, datetime(2026, 11, 12, 10), {1: 1, 25: 1, 35: 1, 45: 0}
    )
    db.commit()

    result = build_exam_performance(db, user)

    # 曲线：时间升序，score 为百分制
    assert [(p["score"], p["correct"], p["total"]) for p in result["points"]] == [
        (50.0, 2, 4),
        (75.0, 3, 4),
    ]
    assert result["points"][1]["attempt_id"] == result["points"][0]["attempt_id"] + 1
    assert result["points"][0]["created_at"] == "2026-11-11T10:00:00"

    # 三大块：言語知識合并 vocab+grammar；total 之和 = 总作答数（8）
    sections = {s["section"]: s for s in result["by_section"]}
    assert sections["language"]["correct"] == 3
    assert sections["language"]["total"] == 4
    assert sections["language"]["accuracy"] == 75.0
    parts = {p["subject"]: p for p in sections["language"]["parts"]}
    assert parts["vocabulary"]["accuracy"] == 100.0
    assert parts["grammar"]["accuracy"] == 50.0
    assert sections["reading"]["accuracy"] == 100.0
    assert sections["listening"]["accuracy"] == 0.0
    assert sum(s["total"] for s in result["by_section"]) == 8

    # 目标分占位
    assert result["goal"] == {"enabled": False, "goal_pct": None}


def test_exam_performance_empty_user_and_level_filter() -> None:
    db = make_db()
    user = add_user(db, "t13b")
    user.current_level = "N2"
    exam = _add_exam_with_questions(db)
    # N3 attempt 不在默认级别范围内
    _add_exam_attempt(
        db, exam, user.id, datetime(2026, 11, 11, 10), {1: 1, 25: 0, 35: 1, 45: 1},
        level="N3",
    )
    db.commit()

    # 空用户全 0
    empty = add_user(db, "t13c")
    empty.current_level = "N2"
    empty_result = build_exam_performance(db, empty)
    assert empty_result["points"] == []
    assert empty_result["by_section"] == [
        {
            "section": "language",
            "label": "言語知識",
            "correct": 0,
            "total": 0,
            "accuracy": None,
            "parts": [
                {"subject": "vocabulary", "label": "词汇", "correct": 0, "total": 0, "accuracy": None},
                {"subject": "grammar", "label": "语法", "correct": 0, "total": 0, "accuracy": None},
            ],
        },
        {"section": "reading", "label": "读解", "correct": 0, "total": 0, "accuracy": None,
         "parts": [{"subject": "reading", "label": "阅读", "correct": 0, "total": 0, "accuracy": None}]},
        {"section": "listening", "label": "听解", "correct": 0, "total": 0, "accuracy": None,
         "parts": [{"subject": "listening", "label": "听解", "correct": 0, "total": 0, "accuracy": None}]},
    ]

    # level 过滤：默认 current_level=N2 排除 N3 attempt；显式 level=N3 命中
    assert build_exam_performance(db, user)["points"] == []
    assert len(build_exam_performance(db, user, level="N3")["points"]) == 1


# ---------------- T-014 效率维度（统计三期，attempt 级时长） ----------------

ATTEMPT_TABLES = ("attempts", "exam_attempts", "practice_attempts")


def test_duration_seconds_migration_idempotent_and_legacy_rows_stay_null(monkeypatch) -> None:
    """S1：旧库启动补 duration_seconds 列，重复执行幂等；存量行保持 NULL = 未采集。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # 模拟旧库：三张 attempt 表尚无 duration_seconds 列，且已有存量行
    with engine.begin() as conn:
        for table in ATTEMPT_TABLES:
            conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN duration_seconds")
        conn.exec_driver_sql(
            "INSERT INTO attempts (user_id, mode, level, subject, subject_label, title, correct, total, created_at) "
            "VALUES ('legacy-user', 'book', 'N3', 'meaning', '词汇', 'Old', 1, 2, '2026-01-01 00:00:00')"
        )

    monkeypatch.setattr(app_main, "engine", engine)
    with Session(engine) as db:
        ensure_schema(db)  # 第一次：补列
        ensure_schema(db)  # 第二次：幂等

    with engine.connect() as conn:
        for table in ATTEMPT_TABLES:
            assert "duration_seconds" in _table_columns(conn, table)
        row = conn.exec_driver_sql("SELECT duration_seconds FROM attempts").all()
        assert row[0][0] is None  # 存量行保持 NULL，不回填


def test_efficiency_aggregates_attempt_level_duration() -> None:
    """S2：NULL 时长 attempt 跳过；avg=Σduration/Σtotal；三 kind 时长占比；日粒度趋势。"""
    db = make_db()
    user = add_user(db)
    exam = Exam(external_id="exam-eff", level="N3", title="N3 Exam")
    db.add(exam)
    db.flush()
    db.add_all(
        [
            Attempt(
                user_id=user.id, mode="book", level="N3", subject="meaning", subject_label="词汇",
                title="T1", correct=8, total=10, duration_seconds=100,
                created_at=datetime(2026, 11, 1, 12),
            ),
            PracticeAttempt(
                user_id=user.id, level="N3", category="grammar", title="G1",
                correct=5, total=10, duration_seconds=100,
                created_at=datetime(2026, 11, 2, 12),
            ),
            PracticeAttempt(
                user_id=user.id, level="N3", category="listening", title="L1",
                correct=5, total=5, duration_seconds=300,
                created_at=datetime(2026, 11, 3, 12),
            ),
            ExamAttempt(
                user_id=user.id, exam_id=exam.id, level="N3", title="N3 Exam",
                correct=10, total=20, duration_seconds=1200,
                created_at=datetime(2026, 11, 4, 12),
            ),
            # 未采集时长：参与 coverage eligible，但不进任何聚合
            ExamAttempt(
                user_id=user.id, exam_id=exam.id, level="N3", title="N3 Exam",
                correct=9, total=20, duration_seconds=None,
                created_at=datetime(2026, 11, 5, 12),
            ),
        ]
    )
    db.commit()

    result = build_efficiency(db, user, granularity="day", days=90, today=date(2026, 11, 30))

    assert result["coverage"] == {"eligible_attempts": 5, "with_duration": 4, "ratio": 0.8}
    assert result["avg_seconds_per_question"] == round(1700 / 45, 1)
    assert result["time_distribution"] == [
        {"kind": "practice", "label": "专项练习", "seconds": 200, "pct": round(200 / 1700 * 100, 1)},
        {"kind": "exam", "label": "模拟考试", "seconds": 1200, "pct": round(1200 / 1700 * 100, 1)},
        {"kind": "listening", "label": "听力精听", "seconds": 300, "pct": round(300 / 1700 * 100, 1)},
    ]
    by_period = {point["period"]: point for point in result["speed_trend"]}
    assert by_period["2026-11-01"] == {"period": "2026-11-01", "avg_seconds": 10.0, "answered": 10}
    assert by_period["2026-11-02"] == {"period": "2026-11-02", "avg_seconds": 10.0, "answered": 10}
    assert by_period["2026-11-03"] == {"period": "2026-11-03", "avg_seconds": 60.0, "answered": 5}
    assert by_period["2026-11-04"] == {"period": "2026-11-04", "avg_seconds": 60.0, "answered": 20}
    assert "2026-11-05" not in by_period


def test_efficiency_for_new_user_returns_empty_state() -> None:
    """S2：空用户不报错，字段为 null / 空数组。"""
    db = make_db()
    user = add_user(db)

    result = build_efficiency(db, user, granularity="day", days=90, today=date(2026, 11, 30))

    assert result == {
        "coverage": {"eligible_attempts": 0, "with_duration": 0, "ratio": None},
        "avg_seconds_per_question": None,
        "speed_trend": [],
        "time_distribution": [],
    }


def test_efficiency_endpoint_granularity_and_days_window() -> None:
    """S2：接口周粒度与 days 窗口（旧 attempt 落窗外不聚合）。"""
    db = make_db()
    user = add_user(db)
    db.add(
        Attempt(
            user_id=user.id, mode="book", level="N3", subject="meaning", subject_label="词汇",
            title="T1", correct=1, total=4, duration_seconds=80,
            created_at=datetime(2026, 1, 6, 12),  # 周一
        )
    )
    db.commit()

    # 2026-01-12（周一）为终点、days=7 → 窗口 01-06..01-12，命中；granularity=week → 周起点 01-05
    result = build_efficiency(db, user, granularity="week", days=7, today=date(2026, 1, 12))
    assert result["speed_trend"] == [
        {"period": "2026-01-05", "avg_seconds": 20.0, "answered": 4}
    ]
    # days=1 → 只含 01-12 当天，窗外
    out = build_efficiency(db, user, granularity="week", days=1, today=date(2026, 1, 12))
    assert out["coverage"] == {"eligible_attempts": 0, "with_duration": 0, "ratio": None}


def test_finalize_exam_run_writes_server_side_duration() -> None:
    """S1：模考 attempt 时长取 run 起止差（服务端可信，不依赖客户端）。"""
    from app.main import finalize_exam_run
    from app.models import ExamLayer, ExamOption, ExamRun

    db = make_db()
    user = add_user(db)
    exam = Exam(external_id="exam-run", level="N3", title="N3 Exam")
    db.add(exam)
    db.flush()
    layer = ExamLayer(external_id="layer-run", exam_id=exam.id, order_index=0)
    db.add(layer)
    db.flush()
    question = ExamQuestion(
        external_id="run-q1", exam_id=exam.id, layer_id=layer.id, order_index=0, level="N3",
    )
    question.options = [
        ExamOption(text="A", is_correct=1, source_order=1),
        ExamOption(text="B", is_correct=0, source_order=2),
    ]
    db.add(question)
    db.commit()

    run = ExamRun(user_id=user.id, exam_id=exam.id, status="in_progress")
    run.started_at = datetime.utcnow() - timedelta(seconds=600)
    db.add(run)
    db.commit()

    finalize_exam_run(db, run)

    attempt = db.get(ExamAttempt, run.attempt_id)
    assert attempt is not None
    assert attempt.duration_seconds is not None
    assert 599 <= attempt.duration_seconds <= 601


def test_submit_attempt_clamps_client_duration() -> None:
    """S1：客户端可选时长——缺失/越界（≤0 或 >4h）存 NULL，合法值原样落库。"""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app as main_app, clamp_duration_seconds, hash_password

    # 钳制函数单测
    assert clamp_duration_seconds(None) is None
    assert clamp_duration_seconds(0) is None
    assert clamp_duration_seconds(-5) is None
    assert clamp_duration_seconds(14401) is None
    assert clamp_duration_seconds(14400) == 14400
    assert clamp_duration_seconds(72) == 72

    db = make_db()
    db.add(User(id="1", username="david", display_name="David", password_hash=hash_password("pw"), current_level="N3"))
    book = Book(external_id=1, name="Book", level="N3", subject="vocabulary", subject_label="词汇")
    db.add(book)
    db.flush()
    question = Question(
        external_id=1, book_id=book.id, level="N3", subject="vocabulary",
        stem="", question_text="?", answer_text="A",
    )
    question.options = [
        Option(text="A", is_correct=1, source_order=1),
        Option(text="B", is_correct=0, source_order=2),
    ]
    db.add(question)
    db.commit()

    main_app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(main_app)
        login = client.post("/api/auth/login", json={"username": "david", "password": "pw"})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        payload = {
            "mode": "book", "level": "N3", "subject": "meaning", "subject_label": "词汇",
            "title": "T", "answers": [{"question_id": question.id, "chosen_option_id": question.options[0].id}],
        }

        # AttemptOut 不含 duration_seconds，落库值查库断言
        ok = client.post("/api/attempts", json={**payload, "duration_seconds": 72}, headers=headers)
        assert ok.status_code == 200
        ok_attempt = db.get(Attempt, ok.json()["id"])
        assert ok_attempt.duration_seconds == 72

        missing = client.post("/api/attempts", json=payload, headers=headers)
        assert missing.status_code == 200
        assert db.get(Attempt, missing.json()["id"]).duration_seconds is None

        over = client.post("/api/attempts", json={**payload, "duration_seconds": 14401}, headers=headers)
        assert over.status_code == 200
        assert db.get(Attempt, over.json()["id"]).duration_seconds is None
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        db.close()
