"""T-022 刀二 ②-1：practice_sessions 端点。

覆盖：创建（单活跃约束：旧 in_progress 自动 abandoned）、active 查询、
按题写进度（payload + current_index）、complete/abandon、属主隔离、校验。
幂等重启由 startup create_all + ensure_schema 保证（D10）。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app, hash_password
from app.models import PracticeSession, User


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="1", username="david", display_name="David", password_hash=hash_password("pw")))
    db.add(User(id="2", username="amy", display_name="Amy", password_hash=hash_password("pw")))
    db.commit()
    main_app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(main_app), db
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        db.close()


def login(client: TestClient, username: str) -> str:
    res = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    assert res.status_code == 200
    return res.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_session(client: TestClient, token: str, **overrides) -> dict:
    body = {"source": "exam_practice", "label": "文法专项", "question_ids": [1, 2, 3]}
    body.update(overrides)
    res = client.post("/api/practice-sessions", json=body, headers=bearer(token))
    assert res.status_code == 200
    return res.json()


def test_start_practice_session_abandons_previous_active(client) -> None:
    """单活跃会话：开新会话后旧 in_progress 自动 abandoned，作答保留。"""
    c, db = client
    token = login(c, "david")

    first = create_session(c, token)
    c.put(
        f"/api/practice-sessions/{first['id']}/answers/1",
        json={"payload": {"chosen_option_id": 9, "is_correct": False}, "current_index": 1},
        headers=bearer(token),
    )

    second = create_session(c, token, source="mixed", label="混合测试")
    assert second["id"] != first["id"]

    rows = db.query(PracticeSession).order_by(PracticeSession.created_at).all()
    assert [(r.id, r.status) for r in rows] == [
        (first["id"], "abandoned"),
        (second["id"], "in_progress"),
    ]
    # 旧会话作答保留
    assert rows[0].answers == {"1": {"chosen_option_id": 9, "is_correct": False}}

    # active 只返回新会话
    active = c.get("/api/practice-sessions/active", headers=bearer(token))
    assert active.json()["id"] == second["id"]


def test_active_empty_when_no_session(client) -> None:
    c, _ = client
    token = login(c, "david")
    assert c.get("/api/practice-sessions/active", headers=bearer(token)).json() is None


def test_save_answer_updates_progress_and_validates(client) -> None:
    c, _ = client
    token = login(c, "david")
    session = create_session(c, token)

    saved = c.put(
        f"/api/practice-sessions/{session['id']}/answers/2",
        json={"payload": 1001, "current_index": 2},
        headers=bearer(token),
    )
    assert saved.status_code == 200
    assert saved.json()["answers"] == {"2": 1001}
    assert saved.json()["current_index"] == 2

    # current_index 缺省不动
    again = c.put(
        f"/api/practice-sessions/{session['id']}/answers/3",
        json={"payload": None},
        headers=bearer(token),
    )
    assert again.json()["answers"]["3"] is None
    assert again.json()["current_index"] == 2

    # 会话外题目拒绝
    res = c.put(
        f"/api/practice-sessions/{session['id']}/answers/999",
        json={"payload": 1},
        headers=bearer(token),
    )
    assert res.status_code == 409


def test_complete_and_abandon(client) -> None:
    c, _ = client
    token = login(c, "david")
    session = create_session(c, token)

    done = c.post(f"/api/practice-sessions/{session['id']}/complete", headers=bearer(token))
    assert done.json()["status"] == "completed"
    # completed 后不可再写进度
    assert (
        c.put(
            f"/api/practice-sessions/{session['id']}/answers/1",
            json={"payload": 1},
            headers=bearer(token),
        ).status_code
        == 409
    )
    # active 无会话
    assert c.get("/api/practice-sessions/active", headers=bearer(token)).json() is None

    second = create_session(c, token)
    abandoned = c.post(f"/api/practice-sessions/{second['id']}/abandon", headers=bearer(token))
    assert abandoned.json()["status"] == "abandoned"


def test_owner_isolation(client) -> None:
    c, _ = client
    david = login(c, "david")
    amy = login(c, "amy")
    session = create_session(c, david)

    assert c.get(f"/api/practice-sessions/{session['id']}", headers=bearer(amy)).status_code == 404
    assert (
        c.put(
            f"/api/practice-sessions/{session['id']}/answers/1",
            json={"payload": 1},
            headers=bearer(amy),
        ).status_code
        == 404
    )
    # amy 开新会话不影响 david 的会话
    create_session(c, amy)
    still_active = c.get(f"/api/practice-sessions/{session['id']}", headers=bearer(david))
    assert still_active.json()["status"] == "in_progress"


def test_create_requires_question_ids(client) -> None:
    c, _ = client
    token = login(c, "david")
    res = c.post(
        "/api/practice-sessions",
        json={"source": "exam_practice", "question_ids": []},
        headers=bearer(token),
    )
    assert res.status_code == 422


def test_exam_practice_questions_by_ids_preserves_order(client) -> None:
    """②-2 恢复原题：按 ids 取回真题（顺序保持、行结构与 random 一致）。"""
    from app.models import Exam, ExamLayer, ExamOption, ExamQuestion

    c, db = client
    exam = Exam(id=30, external_id="e30", level="N5", title="2020年12月 N5", exam_date=202012, question_count=2)
    layer = ExamLayer(id=300, external_id="e30L1", exam_id=30, order_index=1)
    q1 = ExamQuestion(id=301, external_id="e30q1", exam_id=30, layer_id=300, order_index=1, level="N5", question_type=1, title="問題1")
    q2 = ExamQuestion(id=302, external_id="e30q2", exam_id=30, layer_id=300, order_index=2, level="N5", question_type=1, title="問題2")
    db.add_all([exam, layer, q1, q2])
    db.add_all([
        ExamOption(id=3001, question_id=301, text="あ", is_correct=1, source_order=1),
        ExamOption(id=3002, question_id=302, text="い", is_correct=0, source_order=1),
    ])
    db.commit()

    token = login(c, "david")
    res = c.get("/api/exam-practice/questions", params={"ids": "302,301"}, headers=bearer(token))
    assert res.status_code == 200
    rows = res.json()
    assert [row["id"] for row in rows] == [302, 301]  # 请求顺序保持
    assert rows[0]["exam_title"] == "2020年12月 N5"
    assert {option["text"] for option in rows[0]["options"]} == {"い"}

    # 缺失 id 静默跳过；参数校验
    assert [row["id"] for row in c.get("/api/exam-practice/questions", params={"ids": "302,99999"}, headers=bearer(token)).json()] == [302]
    assert c.get("/api/exam-practice/questions", params={"ids": "abc"}, headers=bearer(token)).status_code == 400


def test_textbook_questions_quiz_by_ids(client) -> None:
    """②-2 恢复原题：按 ids 取回教材题（QuizQuestionOut 行结构）。"""
    from app.models import Book, Option, Question

    c, db = client
    book = Book(id=10, external_id=10, name="N5 词汇", level="N5", subject="vocabulary", subject_label="文字词汇", question_count=1)
    q = Question(id=100, external_id=100, book_id=10, level="N5", subject="vocabulary", stem="スタート", question_text="开始", answer_text="はじめ", source_index=1)
    db.add_all([book, q])
    db.add(Option(id=1000, question_id=100, text="はじめ", is_correct=1, source_order=1))
    db.commit()

    token = login(c, "david")
    res = c.get("/api/questions/quiz", params={"ids": "100"}, headers=bearer(token))
    assert res.status_code == 200
    rows = res.json()
    assert rows[0]["id"] == 100
    assert rows[0]["stem"] == "スタート"
    assert rows[0]["options"][0]["text"] == "はじめ"

    # 路由顺序：/api/questions/{id}/quiz 不吞并 /api/questions/quiz
    assert c.get("/api/questions/quiz", params={"ids": "100,100"}, headers=bearer(token)).json()[0]["id"] == 100


def test_put_without_payload_only_updates_index(client) -> None:
    """翻题心跳：payload 字段缺省只更新 current_index，不碰题进度；显式 null 才清空。"""
    c, _ = client
    token = login(c, "david")
    session = create_session(c, token)

    c.put(f"/api/practice-sessions/{session['id']}/answers/1", json={"payload": 900}, headers=bearer(token))
    moved = c.put(f"/api/practice-sessions/{session['id']}/answers/1", json={"current_index": 2}, headers=bearer(token))
    assert moved.json()["answers"] == {"1": 900}
    assert moved.json()["current_index"] == 2

    cleared = c.put(f"/api/practice-sessions/{session['id']}/answers/1", json={"payload": None, "current_index": 0}, headers=bearer(token))
    assert cleared.json()["answers"] == {"1": None}
    assert cleared.json()["current_index"] == 0
