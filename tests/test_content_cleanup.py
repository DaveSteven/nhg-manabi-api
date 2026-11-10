from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.content_cleanup import strip_moji_markup, strip_moji_markup_in_db
from app.database import Base
from app.models import ExamQuestion


def test_strip_moji_markup() -> None:
    assert strip_moji_markup('<p data-x="1">質問</p><MOJiTest_URL><MOJiTest_URL>') == '<p data-x="1">質問</p>'
    assert strip_moji_markup("<br></MOJiTest_URL>本文") == "<br>本文"
    assert strip_moji_markup(None) == ""


def test_strip_moji_markup_in_db_is_idempotent_and_targeted() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                ExamQuestion(
                    external_id="a", exam_id=1, layer_id=1, order_index=1, level="N1",
                    title="<br><MOJiTest_URL>", passage="本文<MOJiTest_URL>", subtitle="<p>字幕</p>",
                    analysis="<MOJiTest_URL>解析",  # 不在清洗字段内，保持原样
                ),
                ExamQuestion(external_id="b", exam_id=1, layer_id=1, order_index=2, level="N1", title="干净"),
            ]
        )
        db.commit()

        assert strip_moji_markup_in_db(db) == 2  # a.title + a.passage
        assert strip_moji_markup_in_db(db) == 0

        a = db.query(ExamQuestion).filter_by(external_id="a").one()
        assert (a.title, a.passage, a.subtitle) == ("<br>", "本文", "<p>字幕</p>")
        assert a.analysis == "<MOJiTest_URL>解析"
        assert db.query(ExamQuestion).filter_by(external_id="b").one().title == "干净"
