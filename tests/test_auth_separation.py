import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app, hash_password
from app.models import AdminUser, User


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    # 学员在 users 表，管理员在 admin_users 表（D12 拆分）
    db.add(User(id="1", username="david", display_name="David", password_hash=hash_password("pw")))
    db.add(
        AdminUser(
            id="admin-1",
            username="root",
            display_name="Root",
            password_hash=hash_password("pw"),
        )
    )
    db.commit()
    main_app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(main_app), db
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        db.close()


def login(client: TestClient, path: str, username: str) -> tuple[int, str | None]:
    res = client.post(path, json={"username": username, "password": "pw"})
    return res.status_code, res.json().get("token") if res.status_code == 200 else None


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_admin_cannot_sign_in_to_learning_app(client) -> None:
    c, _ = client
    # root 只在 admin_users 表，学习端用户名不存在与密码错误返回一致的 401
    status, _ = login(c, "/api/auth/login", "root")
    assert status == 401


def test_learner_cannot_sign_in_to_admin(client) -> None:
    c, _ = client
    status, _ = login(c, "/api/admin/auth/login", "david")
    assert status == 401


def test_tokens_are_scoped_to_their_side(client) -> None:
    c, _ = client
    _, app_token = login(c, "/api/auth/login", "david")
    _, admin_token = login(c, "/api/admin/auth/login", "root")

    assert c.get("/api/me", headers=bearer(app_token)).json()["username"] == "david"
    assert c.get("/api/admin/me", headers=bearer(admin_token)).json()["username"] == "root"

    # 后台 token 不能访问学习端，学习端 token 不能访问后台
    assert c.get("/api/me", headers=bearer(admin_token)).status_code == 401
    assert c.get("/api/attempts", headers=bearer(admin_token)).status_code == 401
    assert c.get("/api/admin/me", headers=bearer(app_token)).status_code == 401
    assert c.get("/api/admin/users", headers=bearer(app_token)).status_code == 401


def test_learning_endpoints_require_login(client) -> None:
    c, _ = client
    # 不再回落到默认用户，也不再接受 X-User-Id 冒充
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/attempts").status_code == 401
    assert c.get("/api/me", headers={"X-User-Id": "1"}).status_code == 401
    assert c.get("/api/attempts", headers={"X-User-Id": "1"}).status_code == 401


def test_admin_logout_revokes_admin_session(client) -> None:
    c, _ = client
    # 登出端点是共享的，admin token 也要能被删（双表查找）
    _, admin_token = login(c, "/api/admin/auth/login", "root")
    assert c.get("/api/admin/me", headers=bearer(admin_token)).status_code == 200
    assert c.post("/api/auth/logout", headers=bearer(admin_token)).status_code == 200
    assert c.get("/api/admin/me", headers=bearer(admin_token)).status_code == 401


def test_admin_login_response_has_no_learner_fields(client) -> None:
    c, _ = client
    res = login(c, "/api/admin/auth/login", "root")
    assert res[0] == 200
    body = c.get("/api/admin/me", headers=bearer(res[1]))
    assert body.status_code == 200
    # D12：admin 契约不再含 is_admin/current_level
    assert set(body.json()) == {"id", "username", "display_name"}
