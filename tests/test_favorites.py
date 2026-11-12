import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app, hash_password
from app.models import (
    Book,
    Exam,
    ExamLayer,
    ExamOption,
    ExamQuestion,
    Option,
    Question,
    User,
)


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(User(id="1", username="david", display_name="David", password_hash=hash_password("pw")))
    db.add(User(id="2", username="amy", display_name="Amy", password_hash=hash_password("pw")))

    book = Book(
        id=10, external_id=10, name="N5 词汇", level="N5", subject="vocabulary",
        subject_label="文字词汇", question_count=1,
    )
    question = Question(
        id=100, external_id=100, book_id=10, level="N5", subject="vocabulary",
        stem="スタート", question_text="开始", answer_text="はじめ", source_index=1,
    )
    db.add(book)
    db.add(question)
    db.add(Option(id=1000, question_id=100, text="はじめ", is_correct=1, source_order=1))
    db.add(Option(id=1001, question_id=100, text="おわり", is_correct=0, source_order=2))

    exam = Exam(
        id=20, external_id="2010.12.N5", level="N5", title="2010年12月 N5",
        exam_date=201012, question_count=1,
    )
    layer = ExamLayer(id=200, external_id="2010.12.N5.L1", exam_id=20, order_index=1)
    exam_question = ExamQuestion(
        id=200, external_id="2010.12.N5.Q1", exam_id=20, layer_id=200, order_index=1,
        level="N5", question_type=5, title="問題1の___",
    )
    db.add(exam)
    db.add(layer)
    db.add(exam_question)
    db.add(ExamOption(id=2000, question_id=200, text="がっこう", is_correct=1, source_order=1))
    db.add(ExamOption(id=2001, question_id=200, text="こうえん", is_correct=0, source_order=2))
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


def test_put_is_idempotent(client) -> None:
    c, db = client
    token = login(c, "david")
    first = c.put("/api/favorites", json={"source": "textbook", "question_id": 100}, headers=bearer(token))
    second = c.put("/api/favorites", json={"source": "textbook", "question_id": 100}, headers=bearer(token))
    assert first.status_code == 200
    assert second.status_code == 200
    favorites = db.execute(
        __import__("sqlalchemy").text("SELECT * FROM favorites")
    ).all()
    assert len(favorites) == 1


def test_delete_then_list_is_empty_and_second_delete_is_404(client) -> None:
    c, _ = client
    token = login(c, "david")
    c.put("/api/favorites", json={"source": "exam", "question_id": 200}, headers=bearer(token))
    assert c.get("/api/favorites", headers=bearer(token)).json()[0]["source"] == "exam"

    res = c.delete("/api/favorites/exam/200", headers=bearer(token))
    assert res.status_code == 204
    assert c.get("/api/favorites", headers=bearer(token)).json() == []

    res = c.delete("/api/favorites/exam/200", headers=bearer(token))
    assert res.status_code == 404


def test_list_join_fields(client) -> None:
    c, _ = client
    token = login(c, "david")
    c.put("/api/favorites", json={"source": "textbook", "question_id": 100}, headers=bearer(token))
    c.put("/api/favorites", json={"source": "exam", "question_id": 200}, headers=bearer(token))

    rows = {row["source"]: row for row in c.get("/api/favorites", headers=bearer(token)).json()}
    textbook = rows["textbook"]
    assert textbook["book_id"] == 10
    assert textbook["book_name"] == "N5 词汇"
    assert textbook["category"] == "vocabulary"
    assert textbook["category_label"] == "文字词汇"
    assert textbook["stem"] == "スタート"

    exam = rows["exam"]
    assert exam["exam_id"] == 20
    assert exam["exam_title"] == "2010年12月 N5"
    # question_type=5 → 文字词汇
    assert exam["category"] == "vocabulary"
    assert exam["answer_text"] == "がっこう"

    # 列表按收藏时间倒序（真题后收藏 → 排前）
    assert c.get("/api/favorites", headers=bearer(token)).json()[0]["source"] == "exam"


def test_user_isolation(client) -> None:
    c, _ = client
    token_a = login(c, "david")
    token_b = login(c, "amy")
    c.put("/api/favorites", json={"source": "textbook", "question_id": 100}, headers=bearer(token_a))
    assert c.get("/api/favorites", headers=bearer(token_b)).json() == []
    assert c.get("/api/favorites/ids", headers=bearer(token_b)).json() == {
        "textbook": [],
        "exam": [],
    }
    assert c.get("/api/favorites/ids", headers=bearer(token_a)).json() == {
        "textbook": [100],
        "exam": [],
    }


def test_unknown_question_is_404_and_bad_source_is_422(client) -> None:
    c, _ = client
    token = login(c, "david")
    assert c.put(
        "/api/favorites", json={"source": "textbook", "question_id": 999}, headers=bearer(token)
    ).status_code == 404
    assert c.put(
        "/api/favorites", json={"source": "exam", "question_id": 999}, headers=bearer(token)
    ).status_code == 404
    assert c.put(
        "/api/favorites", json={"source": "wrong", "question_id": 100}, headers=bearer(token)
    ).status_code == 422
    assert c.delete("/api/favorites/wrong/100", headers=bearer(token)).status_code == 422


def test_level_filter_and_question_group_endpoints(client) -> None:
    c, _ = client
    token = login(c, "david")
    c.put("/api/favorites", json={"source": "textbook", "question_id": 100}, headers=bearer(token))
    c.put("/api/favorites", json={"source": "exam", "question_id": 200}, headers=bearer(token))

    # N4 过滤后为空
    assert c.get("/api/favorites?level=N4", headers=bearer(token)).json() == []
    ids = c.get("/api/favorites/ids?level=N4", headers=bearer(token)).json()
    assert ids == {"textbook": [], "exam": []}

    # 教材单题卷
    res = c.get("/api/questions/100/quiz", headers=bearer(token))
    assert res.status_code == 200
    quiz = res.json()
    assert quiz["mode"] == "favorite_review"
    assert len(quiz["questions"]) == 1
    assert quiz["questions"][0]["id"] == 100
    assert quiz["questions"][0]["options"][0]["text"] in ("はじめ", "おわり")
    assert c.get("/api/questions/999/quiz", headers=bearer(token)).status_code == 404

    # 真题题组：qt=5 非共享材料 → 单题自身
    res = c.get("/api/exam-practice/questions/200/group", headers=bearer(token))
    assert res.status_code == 200
    questions = res.json()
    assert len(questions) == 1
    assert questions[0]["id"] == 200
    assert questions[0]["exam_id"] == 20
    assert questions[0]["exam_title"] == "2010年12月 N5"
    assert questions[0]["practice_order"] == 1
    assert questions[0]["options"][0]["text"] in ("がっこう", "こうえん")
    assert c.get("/api/exam-practice/questions/999/group", headers=bearer(token)).status_code == 404


def test_favorites_require_login(client) -> None:
    c, _ = client
    assert c.get("/api/favorites").status_code == 401
    assert c.get("/api/favorites/ids").status_code == 401
    assert c.put("/api/favorites", json={"source": "textbook", "question_id": 100}).status_code == 401
    assert c.delete("/api/favorites/textbook/100").status_code == 401
