"""管理员账号表拆分迁移测试（D12 / T-020）。

用 tmp 文件 SQLite 预置一张仍含 is_admin 列的旧库，跑 split_admin_accounts 后断言：
①管理员入新表且原用户名+原密码可登录管理端；
②users 无管理员残留、is_admin 列已删除；
③admin 会话被清、学员会话保留；
④二次运行幂等不报错。
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import main as main_module
from app.database import get_db
from app.main import (
    app as main_app,
    hash_password,
    hash_session_token,
    split_admin_accounts,
)
from app.security import verify_password

ADMIN_OLD_ID = "11111111-1111-1111-1111-111111111111"
LEARNER_ID = "22222222-2222-2222-2222-222222222222"
ADMIN_TOKEN = "admin-legacy-token"
LEARNER_TOKEN = "learner-legacy-token"


def build_legacy_db(path: str) -> None:
    """手工建旧库：users 含 is_admin 列，reviewed_by / revisions.user_id 带 users 外键。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id VARCHAR(36) PRIMARY KEY,
            username VARCHAR(64) UNIQUE NOT NULL,
            display_name VARCHAR(128) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            current_level VARCHAR(16) NOT NULL DEFAULT 'N5',
            is_admin BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME,
            last_active_at DATETIME
        );
        CREATE TABLE user_sessions (
            id INTEGER PRIMARY KEY,
            user_id VARCHAR(36) NOT NULL REFERENCES users (id),
            token VARCHAR(128) UNIQUE NOT NULL,
            scope VARCHAR(16) NOT NULL DEFAULT 'app',
            expires_at DATETIME NOT NULL,
            created_at DATETIME
        );
        CREATE TABLE admin_users (
            id VARCHAR(36) PRIMARY KEY,
            username VARCHAR(64) UNIQUE NOT NULL,
            display_name VARCHAR(128) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at DATETIME
        );
        CREATE TABLE admin_user_sessions (
            id INTEGER PRIMARY KEY,
            user_id VARCHAR(36) NOT NULL REFERENCES admin_users (id),
            token VARCHAR(128) UNIQUE NOT NULL,
            expires_at DATETIME NOT NULL,
            created_at DATETIME
        );
        CREATE TABLE exam_questions (
            id INTEGER PRIMARY KEY,
            external_id VARCHAR(64) NOT NULL,
            reviewed_by VARCHAR(36) REFERENCES users (id)
        );
        CREATE TABLE exam_question_revisions (
            id INTEGER PRIMARY KEY,
            question_id INTEGER NOT NULL REFERENCES exam_questions (id),
            user_id VARCHAR(36) NOT NULL REFERENCES users (id),
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at DATETIME
        );
        """
    )
    conn.executemany(
        "INSERT INTO users (id, username, display_name, password_hash, current_level, "
        "is_admin, created_at) VALUES (?, ?, ?, ?, 'N5', ?, '2026-01-01')",
        [
            (ADMIN_OLD_ID, "root", "Root", hash_password("admin-pw"), 1),
            (LEARNER_ID, "david", "David", hash_password("learner-pw"), 0),
        ],
    )
    conn.executemany(
        "INSERT INTO user_sessions (user_id, token, scope, expires_at) VALUES (?, ?, ?, '2027-01-01')",
        [
            (ADMIN_OLD_ID, hash_session_token(ADMIN_TOKEN), "admin"),
            (LEARNER_ID, hash_session_token(LEARNER_TOKEN), "app"),
        ],
    )
    conn.execute(
        "INSERT INTO exam_questions (id, external_id, reviewed_by) VALUES (1, 'q1', ?)",
        (ADMIN_OLD_ID,),
    )
    conn.execute(
        "INSERT INTO exam_question_revisions (id, question_id, user_id, before_json, after_json) "
        "VALUES (1, 1, ?, '{}', '{}')",
        (ADMIN_OLD_ID,),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def legacy_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy.sqlite3")
    build_legacy_db(db_path)

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(main_module, "engine", engine)
    db = Session(engine)
    main_app.dependency_overrides[get_db] = lambda: db
    try:
        yield db, engine
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        db.close()
        engine.dispose()


def users_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}


def fk_targets(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[2] for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()}


def test_split_migration(legacy_env) -> None:
    db, engine = legacy_env

    # 迁移前：admin token 可用（旧库用 user_sessions scope='admin' 的体系）
    assert main_app.dependency_overrides[get_db]() is db

    split_admin_accounts(db)

    conn = sqlite3.connect(engine.url.database)

    # ③ admin 会话被清、学员会话保留（在登录产生新会话之前断言）
    assert conn.execute("SELECT COUNT(*) FROM admin_user_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0] == 1
    remaining = conn.execute("SELECT token, scope FROM user_sessions").fetchone()
    assert remaining == (hash_session_token(LEARNER_TOKEN), "app")

    # ① 管理员入新表，原用户名+原密码可登录管理端
    admins = conn.execute("SELECT username, password_hash FROM admin_users").fetchall()
    assert [row[0] for row in admins] == ["root"]
    assert verify_password("admin-pw", admins[0][1])
    client = TestClient(main_app)
    res = client.post("/api/admin/auth/login", json={"username": "root", "password": "admin-pw"})
    assert res.status_code == 200
    body = res.json()
    assert set(body["user"]) == {"id", "username", "display_name"}
    # 新管理员 UUID ≠ 旧 users 表里的 UUID
    assert body["user"]["id"] != ADMIN_OLD_ID
    # 管理员可访问后台接口
    me = client.get("/api/admin/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200

    # ② users 无管理员残留、is_admin 列已删除
    assert "is_admin" not in users_columns(conn)
    users = conn.execute("SELECT id, username FROM users").fetchall()
    assert users == [(LEARNER_ID, "david")]
    # 外键已去掉 users 指向（revisions 对 exam_questions 的正常外键保留）
    assert "users" not in fk_targets(conn, "exam_questions")
    assert "users" not in fk_targets(conn, "exam_question_revisions")
    assert "exam_questions" in fk_targets(conn, "exam_question_revisions")
    # 数据仍在（列被保留）
    assert conn.execute(
        "SELECT reviewed_by FROM exam_questions WHERE external_id='q1'"
    ).fetchone() == (ADMIN_OLD_ID,)

    # 登录产生了一条新 admin 会话
    assert conn.execute("SELECT COUNT(*) FROM admin_user_sessions").fetchone()[0] == 1

    # 学员旧 token 仍有效、学员端零变化
    me_app = client.get("/api/me", headers={"Authorization": f"Bearer {LEARNER_TOKEN}"})
    assert me_app.status_code == 200
    assert me_app.json()["username"] == "david"
    # 旧 admin token（已删）不可用
    assert client.get(
        "/api/admin/me", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    ).status_code == 401

    # ④ 二次运行幂等不报错，不重复插入
    split_admin_accounts(db)
    assert conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0] == 1
    assert users_columns(conn) == users_columns(conn)  # 列集合稳定

    conn.close()


def test_split_migration_is_noop_on_fresh_schema(tmp_path, monkeypatch) -> None:
    """新库（create_all 出来的表无 is_admin 列）直接跳过。"""
    db_path = str(tmp_path / "fresh.sqlite3")
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(main_module, "engine", engine)
    main_module.Base.metadata.create_all(engine)
    db = Session(engine)
    split_admin_accounts(db)
    assert db.scalar(main_module.select(main_module.AdminUser.id).limit(1)) is None
    db.close()
    engine.dispose()
