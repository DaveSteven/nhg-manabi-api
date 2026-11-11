from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.admin_users import create_admin_users_router
from app.database import Base, get_db
from app.main import verify_password
from app.models import AdminUser, Attempt, Exam, ExamAttempt, User, UserSession


def make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def make_client(db: Session, admin: AdminUser) -> TestClient:
    app = FastAPI()
    app.include_router(create_admin_users_router(lambda: admin))
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def add_admin(db: Session, admin_id: str = "admin-1", username: str = "root") -> AdminUser:
    admin = AdminUser(
        id=admin_id,
        username=username,
        display_name=username,
        password_hash="x",
    )
    db.add(admin)
    db.commit()
    return admin


def add_user(
    db: Session,
    user_id: int,
    username: str | None = None,
    created_at: datetime | None = None,
    current_level: str = "N5",
) -> User:
    user = User(
        id=user_id,
        username=username or f"user-{user_id}",
        display_name=f"User {user_id}",
        password_hash="x",
        created_at=created_at or datetime(2026, 11, 1, 12),
        current_level=current_level,
    )
    db.add(user)
    db.commit()
    return user


def test_list_pagination_and_keyword() -> None:
    db = make_db()
    admin = add_admin(db)
    add_user(db, "2", "alice")
    add_user(db, "3", "bob", created_at=datetime(2026, 11, 3, 12))
    add_user(db, "4", "carol")
    client = make_client(db, admin)

    res = client.get("/api/admin/users")
    assert res.status_code == 200
    body = res.json()
    # users 表只剩学员（D12 拆表），列表无任何排除逻辑
    assert body["total"] == 3
    assert [item["id"] for item in body["list"]] == ["2", "3", "4"]
    assert set(body["list"][0]) == {
        "id",
        "username",
        "display_name",
        "current_level",
        "is_admin",
        "created_at",
        "last_active_at",
    }

    # 未知查询参数被忽略
    for params in ({"include_admins": "true"}, {"role": "admin"}):
        res = client.get("/api/admin/users", params=params)
        assert res.json()["total"] == 3
        assert [item["id"] for item in res.json()["list"]] == ["2", "3", "4"]

    res = client.get("/api/admin/users", params={"keyword": "carol"})
    assert res.json()["total"] == 1
    assert [item["id"] for item in res.json()["list"]] == ["4"]

    res = client.get(
        "/api/admin/users", params={"sortField": "created_at", "sortOrder": "descend"}
    )
    assert [item["id"] for item in res.json()["list"]][0] == "3"

    res = client.get("/api/admin/users", params={"offset": 1, "limit": 1})
    assert [item["id"] for item in res.json()["list"]] == ["3"]
    assert res.json()["total"] == 3


def test_create_updates_and_duplicate_username() -> None:
    db = make_db()
    admin = add_admin(db)
    client = make_client(db, admin)

    res = client.post(
        "/api/admin/users",
        json={
            "username": "alice",
            "display_name": "Alice",
            "password": "secret-pass",
            "current_level": "n2",
            "is_admin": False,
        },
    )
    assert res.status_code == 200
    created = res.json()
    assert created["current_level"] == "N2"
    # 学员接口创建的恒为学员，无法注入 is_admin
    assert created["is_admin"] is False

    res = client.post(
        "/api/admin/users",
        json={"username": "alice", "display_name": "Dup", "password": "secret-pass"},
    )
    assert res.status_code == 409

    res = client.put(
        f"/api/admin/users/{created['id']}",
        json={"display_name": "Alice II", "current_level": "N1", "password": "new-pass"},
    )
    assert res.status_code == 200
    user = db.query(User).filter(User.id == created["id"]).one()
    assert user.display_name == "Alice II"
    assert user.current_level == "N1"
    assert verify_password("new-pass", user.password_hash)

    res = client.put(f"/api/admin/users/{created['id']}", json={"current_level": "N0"})
    assert res.status_code == 400


def test_admin_accounts_are_not_manageable() -> None:
    db = make_db()
    admin = add_admin(db)
    other_admin = add_admin(db, "admin-2", "ops")
    learner = add_user(db, "2")
    client = make_client(db, admin)

    # 管理员在 admin_users 独立表（D12），不属于用户管理范围：
    # 按 UUID 查看、修改、删除、统计都视为不存在
    for target in (admin.id, other_admin.id):
        assert client.get(f"/api/admin/users/{target}").status_code == 404
        assert client.put(f"/api/admin/users/{target}", json={"display_name": "x"}).status_code == 404
        assert client.delete(f"/api/admin/users/{target}").status_code == 404
        assert client.get(f"/api/admin/users/{target}/stats/overview").status_code == 404

    # 创建学员时传 is_admin 字段被忽略，无法借此创建管理员
    res = client.post(
        "/api/admin/users",
        json={"username": "eve", "display_name": "Eve", "password": "secret-pass", "is_admin": True},
    )
    assert res.status_code == 200
    assert res.json()["is_admin"] is False
    assert db.query(AdminUser).filter(AdminUser.username == "eve").one_or_none() is None


def test_delete_guards_and_success() -> None:
    db = make_db()
    admin = add_admin(db)
    client = make_client(db, admin)

    res = client.delete("/api/admin/users/999")
    assert res.status_code == 404

    learner = add_user(db, "2")
    exam = Exam(external_id="exam-1", level="N3", title="N3")
    db.add(exam)
    db.flush()
    db.add(
        Attempt(
            user_id=learner.id,
            mode="book",
            level="N3",
            subject="meaning",
            subject_label="词汇",
            title="P",
            correct=1,
            total=2,
        )
    )
    db.commit()

    # 有学习记录不可删
    res = client.delete(f"/api/admin/users/{learner.id}")
    assert res.status_code == 409

    db.query(Attempt).delete()
    db.add(
        ExamAttempt(
            user_id=learner.id,
            exam_id=exam.id,
            level="N3",
            title="N3",
            correct=1,
            total=2,
        )
    )
    db.commit()
    res = client.delete(f"/api/admin/users/{learner.id}")
    assert res.status_code == 409

    db.query(ExamAttempt).delete()
    db.commit()
    db.add(UserSession(user_id=learner.id, token="tok", expires_at=datetime(2027, 1, 1)))
    db.commit()

    res = client.delete(f"/api/admin/users/{learner.id}")
    assert res.status_code == 200
    assert db.get(User, learner.id) is None
    assert db.query(UserSession).filter_by(user_id=learner.id).count() == 0

    res = client.delete(f"/api/admin/users/{learner.id}")
    assert res.status_code == 404


def test_admin_stats_endpoints_reuse_student_aggregation() -> None:
    db = make_db()
    admin = add_admin(db)
    learner = add_user(db, "2", current_level="N3")
    exam = Exam(external_id="exam-1", level="N3", title="N3")
    db.add(exam)
    db.flush()
    db.add_all(
        [
            Attempt(
                user_id=learner.id,
                mode="book",
                level="N3",
                subject="meaning",
                subject_label="词汇",
                title="P",
                correct=8,
                total=10,
                created_at=datetime(2026, 11, 9, 12),
            ),
            ExamAttempt(
                user_id=learner.id,
                exam_id=exam.id,
                level="N3",
                title="N3",
                correct=15,
                total=20,
                created_at=datetime(2026, 11, 10, 12),
            ),
            Attempt(
                user_id=learner.id,
                mode="book",
                level="N2",
                subject="grammar",
                subject_label="语法",
                title="P2",
                correct=5,
                total=15,
                created_at=datetime(2026, 11, 10, 13),
            ),
        ]
    )
    db.commit()
    client = make_client(db, admin)

    res = client.get(f"/api/admin/users/{learner.id}/stats/overview")
    assert res.status_code == 200
    body = res.json()
    # admin 端点默认不过滤级别：N3 的 10+20 加 N2 的 15
    assert body["totals"]["questions_answered"] == 45
    assert body["totals"]["questions_correct"] == 28
    assert body["totals"]["exam_attempts"] == 1
    assert body["streak"]["last_active_date"] == "2026-11-10"
    assert any(day["answered"] == 10 for day in body["activity"])

    res = client.get(
        f"/api/admin/users/{learner.id}/stats/trend",
        params={"granularity": "day", "days": 7},
    )
    assert res.status_code == 200
    points = res.json()["points"]
    assert sum(p["answered"] for p in points) == 45

    res = client.get(f"/api/admin/users/{learner.id}/stats/overview", params={"level": "N1"})
    assert res.status_code == 200
    assert res.json()["totals"]["questions_answered"] == 0

    # 带 level 只看该级；默认（不带 level）= 全部级别
    res = client.get(f"/api/admin/users/{learner.id}/stats/overview", params={"level": "N3"})
    body = res.json()
    assert body["totals"]["questions_answered"] == 30
    assert {item["level"] for item in body["breakdown_by_level"]} == {"N3"}

    res = client.get("/api/admin/users/999/stats/overview")
    assert res.status_code == 404

    res = client.get(f"/api/admin/users/{learner.id}")
    assert res.status_code == 200
    assert res.json()["username"] == "user-2"
    res = client.get("/api/admin/users/999")
    assert res.status_code == 404
