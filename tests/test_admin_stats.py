from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.admin_stats import build_summary
from app.database import Base
from app.models import Attempt, Exam, ExamAttempt, User


def make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


TODAY = date(2026, 11, 10)


def add_user(db: Session, user_id: int, created_at: datetime, is_admin: bool = False) -> User:
    user = User(
        id=user_id,
        username=f"user-{user_id}",
        display_name=f"User {user_id}",
        password_hash="x",
        is_admin=is_admin,
        created_at=created_at,
    )
    db.add(user)
    db.commit()
    return user


def test_summary_counts_and_trends() -> None:
    db = make_db()
    # 管理员 + 本周新用户 + 老用户
    add_user(db, 1, datetime(2026, 1, 1), is_admin=True)
    add_user(db, 2, datetime(2026, 11, 8))  # 本周注册
    add_user(db, 3, datetime(2026, 5, 1))
    exam = Exam(external_id="exam-1", level="N3", title="N3")
    db.add(exam)
    db.flush()
    db.add_all(
        [
            # 7 天内：用户 2/3 各一批
            Attempt(
                user_id=2, mode="book", level="N3", subject="meaning", subject_label="词汇",
                title="P", correct=8, total=10, created_at=datetime(2026, 11, 8, 12),
            ),
            Attempt(
                user_id=3, mode="book", level="N3", subject="grammar", subject_label="语法",
                title="P", correct=6, total=20, created_at=datetime(2026, 11, 9, 12),
            ),
            # 30 天内但 7 天外
            ExamAttempt(
                user_id=3, exam_id=exam.id, level="N3", title="N3",
                correct=10, total=30, created_at=datetime(2026, 10, 20, 12),
            ),
            # 30 天外，不计入趋势
            Attempt(
                user_id=3, mode="book", level="N3", subject="meaning", subject_label="词汇",
                title="P", correct=1, total=5, created_at=datetime(2026, 8, 1, 12),
            ),
        ]
    )
    # 用户 2 今日活跃；用户 3 十天前活跃
    db.query(User).filter(User.id == 2).update(
        {"last_active_at": datetime(2026, 11, 10, 9)}, synchronize_session=False
    )
    db.query(User).filter(User.id == 3).update(
        {"last_active_at": datetime(2026, 10, 31, 9)}, synchronize_session=False
    )
    db.commit()

    # 管理员（用户 1）再记一笔做题，不应计入任何统计
    db.add(
        Attempt(
            user_id=1, mode="book", level="N3", subject="meaning", subject_label="词汇",
            title="admin", correct=9, total=99, created_at=datetime(2026, 11, 9, 12),
        )
    )
    db.commit()

    summary = build_summary(db, today=TODAY)
    totals = summary["totals"]
    # 统计只面向 C 端用户，管理员被排除
    assert totals["users"] == 2
    assert totals["admins"] == 1
    assert totals["active_users_7d"] == 1  # 只有用户 2
    assert totals["new_users_7d"] == 1  # 只有用户 2
    assert totals["questions_answered"] == 10 + 20 + 30 + 5  # 含 30 天外的，不含管理员的 99
    assert totals["questions_answered_7d"] == 30

    registration = {p["period"]: p["count"] for p in summary["registration_trend"]}
    assert registration["2026-11-08"] == 1
    assert sum(registration.values()) == 1

    activity = {p["period"]: p["count"] for p in summary["activity_trend"]}
    assert activity["2026-11-08"] == 10
    assert activity["2026-11-09"] == 20  # 不含管理员的 99
    assert activity["2026-10-20"] == 30
    assert activity.get("2026-08-01", 0) == 0  # 30 天窗口外，无此键
    assert sum(activity.values()) == 60

    # 趋势窗口 = 30 天（含今天往前 29 天）
    assert len(summary["registration_trend"]) == 30


def test_login_touches_last_active_at() -> None:
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app as main_app, hash_password
    from app.security import verify_password

    db = make_db()
    password_hash = hash_password("secret")
    add_user(db, 1, datetime(2026, 1, 1))
    db.query(User).filter(User.id == 1).update(
        {"password_hash": password_hash, "username": "alice"}, synchronize_session=False
    )
    db.commit()
    assert db.get(User, 1).last_active_at is None

    main_app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(main_app)
        res = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "secret"},
        )
        assert res.status_code == 200
    finally:
        main_app.dependency_overrides.pop(get_db, None)

    assert db.get(User, 1).last_active_at is not None
    assert verify_password("secret", db.get(User, 1).password_hash)
