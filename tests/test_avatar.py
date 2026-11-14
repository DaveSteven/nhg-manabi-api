"""T-036 换头像：PATCH /api/me 的 avatar 白名单校验、显式 null 重置、未传保持。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import AVATAR_IDS, app as main_app, hash_password
from app.models import User


def make_db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.fixture()
def env():
    db = make_db()
    db.add(User(id="1", username="amy", display_name="Amy", password_hash=hash_password("learner-pw")))
    db.commit()
    main_app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(main_app), db
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        db.close()


def login(c: TestClient) -> dict[str, str]:
    res = c.post("/api/auth/login", json={"username": "amy", "password": "learner-pw"})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_avatar_ids_match_frontend_manifest() -> None:
    assert len(AVATAR_IDS) == 12


def test_set_avatar_persists(env) -> None:
    c, db = env
    headers = login(c)
    res = c.patch("/api/me", json={"avatar": "shiba-inu"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["avatar"] == "shiba-inu"
    assert c.get("/api/me", headers=headers).json()["avatar"] == "shiba-inu"
    db.expire_all()
    assert db.get(User, "1").avatar == "shiba-inu"


def test_invalid_avatar_id_rejected(env) -> None:
    c, db = env
    headers = login(c)
    assert c.patch("/api/me", json={"avatar": "http://evil/x"}, headers=headers).status_code == 400
    assert c.patch("/api/me", json={"avatar": "david"}, headers=headers).status_code == 400
    db.expire_all()
    assert db.get(User, "1").avatar is None


def test_explicit_null_resets_avatar(env) -> None:
    c, _ = env
    headers = login(c)
    assert c.patch("/api/me", json={"avatar": "koi"}, headers=headers).json()["avatar"] == "koi"
    assert c.patch("/api/me", json={"avatar": None}, headers=headers).json()["avatar"] is None


def test_patch_without_avatar_keeps_value(env) -> None:
    """等级保存路径（POST /api/me/level 复用 update_me）不带 avatar，不得误清。"""
    c, _ = env
    headers = login(c)
    c.patch("/api/me", json={"avatar": "koi"}, headers=headers)
    res = c.post("/api/me/level", json={"current_level": "n2"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["avatar"] == "koi"
    assert res.json()["current_level"] == "N2"


def test_avatar_requires_auth(env) -> None:
    c, _ = env
    assert c.patch("/api/me", json={"avatar": "koi"}).status_code in (401, 403)


# ---- T-036 扩展：编辑资料之昵称（display_name） ----


def test_update_display_name(env) -> None:
    c, db = env
    headers = login(c)
    res = c.patch("/api/me", json={"display_name": " 阿梦 "}, headers=headers)
    assert res.status_code == 200
    assert res.json()["display_name"] == "阿梦"  # strip
    db.expire_all()
    assert db.get(User, "1").display_name == "阿梦"


def test_empty_display_name_rejected(env) -> None:
    c, _ = env
    headers = login(c)
    assert c.patch("/api/me", json={"display_name": ""}, headers=headers).status_code == 400
    assert c.patch("/api/me", json={"display_name": "   "}, headers=headers).status_code == 400


def test_patch_without_display_name_keeps_value(env) -> None:
    c, _ = env
    headers = login(c)
    c.patch("/api/me", json={"display_name": "阿梦"}, headers=headers)
    res = c.patch("/api/me", json={"avatar": "koi"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["display_name"] == "阿梦"


def test_edit_profile_saves_both_fields(env) -> None:
    """编辑资料弹层一次 PATCH 同时保存头像 + 昵称。"""
    c, _ = env
    headers = login(c)
    res = c.patch("/api/me", json={"avatar": "fox", "display_name": "阿梦"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["avatar"] == "fox"
    assert res.json()["display_name"] == "阿梦"
