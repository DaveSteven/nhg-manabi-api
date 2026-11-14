"""admin 题目校正 PATCH 契约测试（含 T-032 passage 富文本编辑）。

- PATCH 可选 passage：传了才更新（兼容未升级客户端），strip 处理
- passage 变更进入 revision 快照（before/after）
- 不传 passage 的旧格式请求不影响原值
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app, get_admin_user
from app.models import (
    AdminUser,
    Exam,
    ExamLayer,
    ExamOption,
    ExamQuestion,
    ExamQuestionRevision,
)


def make_db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.fixture()
def env(monkeypatch):
    db = make_db()
    admin = AdminUser(id="admin-1", username="root", display_name="root", password_hash="x")
    db.add(admin)
    exam = Exam(id=1, external_id="e1", level="N3", title="2023 N3")
    db.add(exam)
    layer = ExamLayer(id=1, external_id="e1-l1", exam_id=1, order_index=1, question_type=32)
    db.add(layer)
    question = ExamQuestion(
        id=1, external_id="q1", exam_id=1, layer_id=1, order_index=1, level="N3",
        question_type=32, passage="<p>旧原文</p>",
    )
    db.add(question)
    db.add(ExamOption(question_id=1, source_order=1, text="<p>一</p>", is_correct=1))
    db.commit()
    main_app.dependency_overrides[get_db] = lambda: db
    main_app.dependency_overrides[get_admin_user] = lambda: admin
    try:
        yield TestClient(main_app), db
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        main_app.dependency_overrides.pop(get_admin_user, None)
        db.close()


def patch_payload(**overrides):
    payload = {
        "correct_option_id": 1,
        "answer_status": "verified",
        "analysis_status": "reviewed",
    }
    payload.update(overrides)
    return payload


def test_patch_passage_updates_and_records_revision(env) -> None:
    c, db = env
    res = c.patch("/api/admin/exam-questions/1", json=patch_payload(passage="<p>新原文</p>  "))
    assert res.status_code == 200
    db.expire_all()
    question = db.get(ExamQuestion, 1)
    assert question.passage == "<p>新原文</p>"
    # 快照记录 passage，版本历史可追溯
    revision = json.loads(db.scalar(
        select(ExamQuestionRevision.after_json).where(ExamQuestionRevision.question_id == 1)
    ))
    assert revision["passage"] == "<p>新原文</p>"


def test_patch_without_passage_keeps_original(env) -> None:
    c, db = env
    res = c.patch("/api/admin/exam-questions/1", json=patch_payload())
    assert res.status_code == 200
    db.expire_all()
    assert db.get(ExamQuestion, 1).passage == "<p>旧原文</p>"
