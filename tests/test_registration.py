"""T-028 自助注册测试：覆盖 features A18 的 AC-01 ~ AC-08。

- AC-01 注册创建未激活账号并发验证邮件
- AC-02 未激活登录同「不存在」口径；激活后正常登录使用功能
- AC-03 链接一次性 / 过期明确失败；可重发
- AC-04 同 IP 注册超限、重发超频被拒
- AC-05 用户名/邮箱/密码字段级错误
- AC-06 忘记密码 → 重置 → 旧密码失效、会话全部撤销
- AC-07 B4 代建老账号（已激活、无 email）行为零变化
- AC-08 发信失败注册仍成功、可重发（emailer mock）
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main as main_module
from app import rate_limit
from app.database import Base, get_db
from app.emailer import EmailSendError
from app.main import app as main_app, create_email_token, hash_password, hash_session_token
from app.models import AuthEmailToken, User


def make_db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.fixture()
def env(monkeypatch):
    db = make_db()
    sent: list[tuple[str, str, str]] = []

    def fake_send(to, subject, html):
        sent.append((to, subject, html))

    monkeypatch.setattr(main_module, "send_email", fake_send)
    main_app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(main_app), db, sent
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        db.close()


def register(c: TestClient, username="hanako", email="hanako@example.com", password="pw-123456"):
    return c.post("/api/auth/register", json={"username": username, "email": email, "password": password})


def login(c: TestClient, username="hanako", password="pw-123456"):
    return c.post("/api/auth/login", json={"username": username, "password": password})


def last_verify_token(sent, email="hanako@example.com") -> str:
    """从 mock 邮件链接里提取验证 token。"""
    html = [item for item in sent if item[0] == email][-1][2]
    return html.split("token=")[1].split('"')[0].split(" ")[0].split("<")[0]


# ---- AC-01 / AC-02：注册 → 未激活 → 验证 → 登录 ----

def test_register_creates_unverified_user_and_sends_email(env) -> None:
    c, db, sent = env
    res = register(c)
    assert res.status_code == 200
    user = db.scalar(select(User).where(User.username == "hanako"))
    assert user is not None
    assert user.activated_at is None
    assert user.email == "hanako@example.com"
    assert user.display_name == "hanako"
    assert len(sent) == 1 and sent[0][0] == "hanako@example.com"
    # 原文 token 不入库
    assert last_verify_token(sent) not in db.scalars(select(AuthEmailToken.token_hash)).all()


def test_unverified_login_matches_unknown_user_then_activation_unlocks(env) -> None:
    c, db, sent = env
    register(c)
    # 未激活登录与「账号不存在」完全同口径（AC-02）
    blocked = login(c)
    unknown = c.post("/api/auth/login", json={"username": "ghost", "password": "pw-123456"})
    assert blocked.status_code == unknown.status_code == 401
    assert blocked.json() == unknown.json()

    token = last_verify_token(sent)
    assert c.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    db.expire_all()
    assert db.scalar(select(User).where(User.username == "hanako")).activated_at is not None

    res = login(c)
    assert res.status_code == 200
    auth = {"Authorization": f"Bearer {res.json()['token']}"}
    assert c.get("/api/me", headers=auth).status_code == 200


# ---- AC-03：一次性 / 过期 / 重发 ----

def test_verify_token_is_single_use_and_expires(env) -> None:
    c, db, sent = env
    register(c)
    token = last_verify_token(sent)

    # 首次验证成功；重放同一链接幂等返回 already（用户看到「已激活」而非误导性「失效」）
    first = c.post("/api/auth/verify-email", json={"token": token})
    assert first.status_code == 200
    replay = c.post("/api/auth/verify-email", json={"token": token})
    assert replay.status_code == 200
    assert replay.json()["status"] == "already"

    # 过期令牌失败（用未激活用户测：已激活用户的重放走幂等 already 分支）
    register(c, username="hanako2", email="hanako2@example.com")
    user2 = db.scalar(select(User).where(User.username == "hanako2"))
    stale = AuthEmailToken(
        user_id=user2.id,
        token_hash=hash_session_token("stale-token"),
        purpose="verify",
        expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    db.add(stale)
    db.commit()
    res = c.post("/api/auth/verify-email", json={"token": "stale-token"})
    assert res.status_code == 400
    assert "过期" in res.json()["detail"]

    # 乱码 token 也 400，不 500
    assert c.post("/api/auth/verify-email", json={"token": ""}).status_code == 400


def test_resend_verification_throttled_and_rotates_token(env) -> None:
    c, db, sent = env
    register(c)
    old_token = last_verify_token(sent)
    # 注册刚发过一封：60s 内重发被拒（≥60s 间隔口径）
    res = c.post("/api/auth/resend-verification", json={"email": "hanako@example.com"})
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) > 0

    # 间隔放开后重发成功：旧链接作废、新链接可用
    user = db.scalar(select(User).where(User.username == "hanako"))
    user_id = user.id
    rate_limit.auth_requests.clear("resend|hanako@example.com")
    rate_limit.auth_requests.clear("resend-ip|testclient")
    assert c.post("/api/auth/resend-verification", json={"email": "hanako@example.com"}).status_code == 200
    db.expire_all()
    remaining = db.scalars(
        select(AuthEmailToken).where(AuthEmailToken.user_id == user_id, AuthEmailToken.purpose == "verify")
    ).all()
    assert len(remaining) == 1
    new_token = last_verify_token(sent, "hanako@example.com")
    assert new_token != old_token
    assert c.post("/api/auth/verify-email", json={"token": new_token}).status_code == 200


def test_resend_unified_success_for_unknown_or_activated(env) -> None:
    c, db, sent = env
    # 不存在的邮箱：统一成功，不发信
    res = c.post("/api/auth/resend-verification", json={"email": "nobody@example.com"})
    assert res.status_code == 200 and not sent
    # 已激活账号：统一成功，不发信
    db.add(User(username="ken", display_name="ken", password_hash=hash_password("pw-123456"), email="ken@example.com"))
    db.commit()
    res = c.post("/api/auth/resend-verification", json={"email": "ken@example.com"})
    assert res.status_code == 200 and not sent


# ---- AC-04：注册限频 ----

def test_register_rate_limit_per_ip(env) -> None:
    c, _db, sent = env
    for i in range(rate_limit.REGISTER_MAX_PER_IP_PER_DAY):
        assert register(c, username=f"user{i}", email=f"user{i}@example.com").status_code == 200
    res = register(c, username="user9", email="user9@example.com")
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) > 0


# ---- AC-05：字段级校验 ----

@pytest.mark.parametrize(
    "password",
    ["a1234", "x" * 65, "has space1", "has`tick", "中文密码 six", "pw12345\n"],
)
def test_register_rejects_bad_passwords(env, password) -> None:
    c, _db, _sent = env
    res = register(c, password=password)
    assert res.status_code == 400
    errors = res.json()["detail"]["errors"]
    assert [item["field"] for item in errors] == ["password"]


def test_register_accepts_documented_special_symbols(env) -> None:
    c, _db, _sent = env
    res = register(c, username="sym", email="sym@example.com", password="aB1!@#$%^&*()-_+=[]{};:',.<>?/|~")
    assert res.status_code == 200


@pytest.mark.parametrize(
    "username,email",
    [("", "a@example.com"), ("x" * 65, "a@example.com"), ("ok", "not-an-email"), ("ok", ""), ("ok", "a@b")],
)
def test_register_field_validation(env, username, email) -> None:
    c, _db, _sent = env
    res = register(c, username=username, email=email)
    assert res.status_code == 400
    fields = {item["field"] for item in res.json()["detail"]["errors"]}
    assert fields <= {"username", "email"}


@pytest.mark.parametrize("username", ["Hanako", "hanako-tanaka", "hanako tanaka", "hanako。", "用户名"])
def test_register_rejects_invalid_username_characters(env, username) -> None:
    """用户名规则（2026-11-13 收紧）：仅小写英文 + 数字 + 下划线。"""
    c, _db, _sent = env
    res = register(c, username=username, email="u@example.com", password="good-pw-1")
    assert res.status_code == 400
    errors = res.json()["detail"]["errors"]
    assert errors[0]["field"] == "username"
    assert "小写英文" in errors[0]["message"]


def test_register_duplicate_username_and_email(env) -> None:
    c, _db, _sent = env
    register(c)
    res = register(c, username="hanako", email="other@example.com")
    assert res.status_code == 400
    assert {item["field"] for item in res.json()["detail"]["errors"]} == {"username"}
    res = register(c, username="other", email="hanako@example.com")
    assert res.status_code == 400
    assert {item["field"] for item in res.json()["detail"]["errors"]} == {"email"}


def test_register_username_unique_case_sensitive(env) -> None:
    """唯一性区分大小写（2026-11-13 用户确认）：存量「Hanako」不挡新注册「hanako」（不同账号）。"""
    c, db, _sent = env
    db.add(User(username="Hanako", display_name="Hanako", password_hash=hash_password("legacy-pw")))
    db.commit()
    assert register(c, username="hanako", email="new@example.com").status_code == 200
    # exact match：同名同大小写才冲突
    res = register(c, username="hanako", email="another@example.com")
    assert res.status_code == 400
    assert res.json()["detail"]["errors"][0]["field"] == "username"


# ---- AC-07：B4 代建老账号零变化 ----

def test_legacy_admin_created_account_unchanged(env) -> None:
    c, db, _sent = env
    db.add(User(username="legacy", display_name="Legacy", password_hash=hash_password("legacy-pw"), email=None))
    db.commit()
    res = login(c, "legacy", "legacy-pw")
    assert res.status_code == 200
    assert c.get("/api/me", headers={"Authorization": f"Bearer {res.json()['token']}"}).status_code == 200


# ---- AC-06：忘记/重置密码 ----

def test_forgot_and_reset_password_flow(env) -> None:
    c, db, sent = env
    register(c)
    c.post("/api/auth/verify-email", json={"token": last_verify_token(sent)})
    old_login = login(c)
    old_token = old_login.json()["token"]

    # 注册邮件的 60s 间隔已过（测试放开计数）
    rate_limit.auth_requests.clear("resend|hanako@example.com")
    rate_limit.auth_requests.clear("resend-ip|testclient")
    res = c.post("/api/auth/forgot-password", json={"email": "hanako@example.com"})
    assert res.status_code == 200
    assert any(item[1] == "JLPT GO! 重置密码" for item in sent)
    reset_html = [item for item in sent if item[1] == "JLPT GO! 重置密码"][-1][2]
    reset_token = reset_html.split("token=")[1].split('"')[0].split(" ")[0].split("<")[0]

    # 新密码规则同注册
    bad = c.post("/api/auth/reset-password", json={"token": reset_token, "password": "short"})
    assert bad.status_code == 400 and bad.json()["detail"]["errors"][0]["field"] == "password"
    # 令牌未被坏请求消耗（只做校验，未删令牌）
    res = c.post("/api/auth/reset-password", json={"token": reset_token, "password": "new-pw-123"})
    assert res.status_code == 200

    # 旧密码失效、现有会话全部撤销（AC-06）
    assert login(c, password="pw-123456").status_code == 401
    assert c.get("/api/me", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
    assert login(c, password="new-pw-123").status_code == 200
    # 一次性：重放失败
    assert c.post("/api/auth/reset-password", json={"token": reset_token, "password": "again-pw-1"}).status_code == 400


def test_forgot_password_unified_success_and_filtering(env) -> None:
    c, db, sent = env
    # 不存在 / 未验证账号不发信，统一成功
    assert c.post("/api/auth/forgot-password", json={"email": "nobody@example.com"}).status_code == 200
    register(c)
    rate_limit.auth_requests.clear("resend|hanako@example.com")
    rate_limit.auth_requests.clear("resend-ip|testclient")
    assert c.post("/api/auth/forgot-password", json={"email": "hanako@example.com"}).status_code == 200
    reset_mails = [item for item in sent if item[1] == "JLPT GO! 重置密码"]
    assert reset_mails == []  # 未激活账号不发重置邮件
    # 已激活 + 有 email：发信（先放开 60s 重发间隔）
    user = db.scalar(select(User).where(User.username == "hanako"))
    user.activated_at = datetime.utcnow()
    db.commit()
    rate_limit.auth_requests.clear("resend|hanako@example.com")
    rate_limit.auth_requests.clear("resend-ip|testclient")
    assert c.post("/api/auth/forgot-password", json={"email": "hanako@example.com"}).status_code == 200
    assert [item for item in sent if item[1] == "JLPT GO! 重置密码"]


# ---- AC-08：发信失败 ----

def test_register_succeeds_when_email_fails(env, monkeypatch) -> None:
    c, db, _sent = env

    def boom(to, subject, html):
        raise EmailSendError("resend request failed: connection refused")

    monkeypatch.setattr(main_module, "send_email", boom)
    res = register(c)
    assert res.status_code == 200
    assert res.json()["detail"] and "重发" in res.json()["detail"]
    user = db.scalar(select(User).where(User.username == "hanako"))
    assert user is not None and user.activated_at is None

    # 恢复发信后可重发（AC-08 兜底，无死账号）
    rate_limit.auth_requests.clear("resend|hanako@example.com")
    rate_limit.auth_requests.clear("resend-ip|testclient")
    res = c.post("/api/auth/resend-verification", json={"email": "hanako@example.com"})
    assert res.status_code == 200
    db.expire_all()
    assert db.scalar(
        select(AuthEmailToken).where(AuthEmailToken.user_id == user.id, AuthEmailToken.purpose == "verify")
    )


# ---- T-031：停用账号登录门禁 ----

def test_disabled_account_login_gate(env) -> None:
    c, db, sent = env
    register(c)
    token = last_verify_token(sent)
    assert c.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    assert login(c).status_code == 200

    user = db.scalar(select(User).where(User.username == "hanako"))
    user.disabled_at = datetime.utcnow()
    db.commit()

    # 持正确凭证的本人看到明确停用提示
    blocked = login(c)
    assert blocked.status_code == 401
    assert blocked.json()["detail"] == "账号已被停用"

    # 密码错误仍走通用口径，不向无凭证者泄露停用状态
    wrong = login(c, password="wrong-password")
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "Invalid username or password"

    # 恢复后可正常登录
    user.disabled_at = None
    db.commit()
    assert login(c).status_code == 200
