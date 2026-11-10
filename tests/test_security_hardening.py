from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main as main_module
from app import rate_limit
from app.database import Base, get_db
from app.main import app as main_app, ensure_default_user, hash_password, hash_session_token, migrate_session_tokens
from app.models import Book, Option, Question, User, UserSession


def make_db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.fixture()
def env():
    db = make_db()
    db.add(User(id=1, username="amy", display_name="Amy", password_hash=hash_password("learner-pw")))
    db.add(User(id=2, username="root", display_name="Root", password_hash=hash_password("admin-pw"), is_admin=True))
    book = Book(id=1, external_id=1, name="B", level="N3", subject="grammar", subject_label="文法")
    db.add(book)
    q = Question(id=1, external_id=1, book_id=1, level="N3", subject="grammar", stem="s", question_text="q", answer_text="a")
    db.add(q)
    db.add_all(
        [
            Option(id=1, question_id=1, text="right", is_correct=1, source_order=0),
            Option(id=2, question_id=1, text="wrong", is_correct=0, source_order=1),
        ]
    )
    q2 = Question(id=2, external_id=2, book_id=1, level="N3", subject="grammar", stem="s", question_text="q2", answer_text="a")
    db.add(q2)
    db.add(Option(id=3, question_id=2, text="other", is_correct=1, source_order=0))
    db.commit()
    main_app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(main_app), db
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        db.close()


def login(c: TestClient, username="amy", password="learner-pw", path="/api/auth/login"):
    return c.post(path, json={"username": username, "password": password})


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- 3. 登录防暴力破解 ----

def test_login_locks_after_repeated_failures(env) -> None:
    c, _ = env
    for _ in range(rate_limit.LOGIN_MAX_FAILURES_PER_ACCOUNT):
        assert login(c, password="nope").status_code == 401
    res = login(c, password="nope")
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) > 0
    # 锁定期间正确密码也不放行
    assert login(c).status_code == 429
    # 其他账号不受这个账号锁定的影响
    assert login(c, "root", "admin-pw", "/api/admin/auth/login").status_code == 200


def test_successful_login_clears_failures(env) -> None:
    c, _ = env
    for _ in range(rate_limit.LOGIN_MAX_FAILURES_PER_ACCOUNT - 1):
        login(c, password="nope")
    assert login(c).status_code == 200
    for _ in range(rate_limit.LOGIN_MAX_FAILURES_PER_ACCOUNT - 1):
        assert login(c, password="nope").status_code == 401


def test_unknown_user_gets_same_error(env) -> None:
    c, _ = env
    assert login(c, "ghost", "x").json() == login(c, "amy", "x").json()


# ---- 4. 会话安全 ----

def test_tokens_are_stored_hashed(env) -> None:
    c, db = env
    token = login(c).json()["token"]
    stored = db.scalars(select(UserSession.token)).all()
    assert token not in stored
    assert hash_session_token(token) in stored
    assert c.get("/api/me", headers=auth(token)).status_code == 200


def test_legacy_plaintext_tokens_are_migrated(env) -> None:
    _, db = env
    db.add(UserSession(user_id=1, token="legacy-token", scope="app", expires_at=datetime.utcnow() + timedelta(days=1)))
    db.add(UserSession(user_id=1, token="expired", scope="app", expires_at=datetime.utcnow() - timedelta(days=1)))
    db.commit()
    migrate_session_tokens(db)
    tokens = db.scalars(select(UserSession.token)).all()
    assert tokens == [hash_session_token("legacy-token")]
    c = TestClient(main_app)
    assert c.get("/api/me", headers=auth("legacy-token")).status_code == 200


def test_password_reset_revokes_sessions(env) -> None:
    c, _ = env
    learner_token = login(c).json()["token"]
    admin_token = login(c, "root", "admin-pw", "/api/admin/auth/login").json()["token"]
    res = c.put("/api/admin/users/1", json={"password": "short"}, headers=auth(admin_token))
    assert res.status_code == 400  # 少于 8 位
    assert c.get("/api/me", headers=auth(learner_token)).status_code == 200
    res = c.put("/api/admin/users/1", json={"password": "new-password-1"}, headers=auth(admin_token))
    assert res.status_code == 200
    assert c.get("/api/me", headers=auth(learner_token)).status_code == 401
    assert login(c, password="new-password-1").status_code == 200


def test_default_user_not_recreated_and_admin_not_demoted(env, monkeypatch) -> None:
    _, db = env
    monkeypatch.setattr(main_module, "DEFAULT_DAVID_PASSWORD", "dev-pass")
    # 库里已有用户（但没有 david）：不会重建 david
    assert ensure_default_user(db) is None
    assert db.scalar(select(User).where(User.username == "david")) is None
    # id=1 不是 david 时，不会被当作 david 降级
    db.query(User).filter(User.id == 1).update({"is_admin": True})
    db.commit()
    ensure_default_user(db)
    assert db.get(User, 1).is_admin is True


# ---- 5. 题库必须登录 ----

@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/books?level=N3&subject=grammar", None),
        ("get", "/api/books/1/quiz", None),
        ("get", "/api/exams?level=N3", None),
        ("get", "/api/exams/1", None),
        ("get", "/api/exams/1/questions", None),
        ("get", "/api/exam-practice/random?level=N3&types=1", None),
        ("get", "/api/quiz/mixed?level=N3&subject=grammar", None),
        ("post", "/api/check-answer", {"question_id": 1, "chosen_option_id": 1}),
    ],
)
def test_question_endpoints_require_login(env, method, path, body) -> None:
    c, _ = env
    res = getattr(c, method)(path, json=body) if body else getattr(c, method)(path)
    assert res.status_code == 401


def test_check_answer_validates_option(env) -> None:
    c, db = env
    token = login(c).json()["token"]
    res = c.post("/api/check-answer", json={"question_id": 1, "chosen_option_id": 1}, headers=auth(token))
    assert res.status_code == 200 and res.json()["is_correct"] is True
    # 拿别的题的选项不算选中
    res = c.post("/api/check-answer", json={"question_id": 1, "chosen_option_id": 3}, headers=auth(token))
    assert res.json()["chosen_option_id"] is None and res.json()["is_correct"] is False
    # 没有正确选项的题不再 500
    db.query(Option).filter(Option.id == 3).update({"is_correct": 0})
    db.commit()
    res = c.post("/api/check-answer", json={"question_id": 2, "chosen_option_id": 3}, headers=auth(token))
    assert res.status_code == 409


# ---- 防抓取 ----

def test_content_quota_per_user(env, monkeypatch) -> None:
    c, _ = env
    monkeypatch.setitem(rate_limit.QUOTAS, "content", (3, 1000))
    token = login(c).json()["token"]
    for _ in range(3):
        assert c.get("/api/books/1/quiz", headers=auth(token)).status_code == 200
    res = c.get("/api/books/1/quiz", headers=auth(token))
    assert res.status_code == 429
    # 判题额度独立计算
    assert c.post("/api/check-answer", json={"question_id": 1}, headers=auth(token)).status_code == 200


def test_daily_quota(env, monkeypatch) -> None:
    c, _ = env
    monkeypatch.setitem(rate_limit.QUOTAS, "content", (1000, 2))
    token = login(c).json()["token"]
    assert c.get("/api/books/1/quiz", headers=auth(token)).status_code == 200
    assert c.get("/api/books/1/quiz", headers=auth(token)).status_code == 200
    res = c.get("/api/books/1/quiz", headers=auth(token))
    assert res.status_code == 429 and res.json()["detail"] == "Daily limit reached"


def test_ip_quota(env, monkeypatch) -> None:
    c, _ = env
    monkeypatch.setattr(rate_limit, "IP_REQUESTS_PER_MINUTE", 3)
    for _ in range(3):
        assert c.get("/api/me").status_code == 401
    assert c.get("/api/me").status_code == 429
    # 健康检查不计入
    assert c.get("/api/health").status_code == 200


def test_forwarded_ip_only_trusted_from_proxy() -> None:
    from starlette.requests import Request

    def req(host: str, headers: dict[str, str]) -> Request:
        return Request(
            {
                "type": "http",
                "client": (host, 1234),
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            }
        )

    assert rate_limit.client_ip(req("172.18.0.5", {"X-Real-IP": "8.8.8.8"})) == "8.8.8.8"
    assert rate_limit.client_ip(req("8.8.4.4", {"X-Real-IP": "1.2.3.4"})) == "8.8.4.4"


def test_api_docs_disabled_by_default(env) -> None:
    c, _ = env
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404
