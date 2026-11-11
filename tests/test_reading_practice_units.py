"""読解专项组卷单位：一篇 = 一篇文章 + 其小题，不把一层里的多篇文章并成一组。"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app, ensure_default_user, hash_password
from app.models import Exam, ExamLayer, ExamOption, ExamQuestion, User


def make_db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def seed_multi_article_layer(db: Session) -> None:
    db.add(User(id="u1", username="amy", display_name="Amy", password_hash=hash_password("learner-pw")))
    exam = Exam(id=1, external_id="N1-201307", level="N1", title="2013年7月N1")
    layer = ExamLayer(id=1, external_id="L1", exam_id=1, order_index=1, question_type=32, question_count=6)
    image_layer = ExamLayer(id=2, external_id="L2", exam_id=1, order_index=2, question_type=36, question_count=2, image_path="img/a.png")
    db.add_all([exam, layer, image_layer])
    # qt=32 层装两篇文章，各带两个小题
    rows = [
        (1, "甲の文章", 1),
        (2, "甲の文章", 2),
        (3, "乙の文章", 3),
        (4, "乙の文章", 4),
        # qt=36 信息检索：无文章，两题共享整页图
        (5, "", 1),
        (6, "", 2),
    ]
    for question_id, passage, order_index in rows:
        layer_id = 1 if question_id <= 4 else 2
        question_type = 32 if question_id <= 4 else 36
        question = ExamQuestion(
            id=question_id,
            external_id=f"Q{question_id}",
            exam_id=1,
            layer_id=layer_id,
            order_index=order_index,
            level="N1",
            question_type=question_type,
            title=f"問{question_id}",
            passage=passage,
        )
        db.add(question)
        db.add(ExamOption(id=question_id * 10, question_id=question_id, text="正解", is_correct=1, source_order=0))
    db.commit()


def test_multi_article_layer_practices_as_separate_articles() -> None:
    db = make_db()
    seed_multi_article_layer(db)
    main_app.dependency_overrides[get_db] = lambda: db
    ensure_default_user(db)
    try:
        c = TestClient(main_app)
        token = c.post("/api/auth/login", json={"username": "amy", "password": "learner-pw"}).json()["token"]
        res = c.get("/api/exam-practice/random?level=N1&types=32&limit=1", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        questions = res.json()
        # limit=1 篇：只抽一篇文章的 2 个小题，不把整层 4 题都带上
        assert len(questions) == 2
        passages = {question["passage"] for question in questions}
        assert len(passages) == 1 and passages.pop()

        # limit=2 篇：两篇文章各成一组，同篇文章的小题连续（文章不交错）
        res = c.get("/api/exam-practice/random?level=N1&types=32&limit=2", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        questions = res.json()
        assert len(questions) == 4
        runs: list[str] = []
        for question in questions:
            if not runs or runs[-1] != question["passage"]:
                runs.append(question["passage"])
        assert len(set(runs)) == 2  # 两篇文章各自连续出现，不交错

        # 信息检索（无文章）整层一组：两题一起下发
        res = c.get("/api/exam-practice/random?level=N1&types=36&limit=1", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        assert [question["id"] for question in res.json()] == [5, 6]
    finally:
        main_app.dependency_overrides.pop(get_db, None)
        db.close()
