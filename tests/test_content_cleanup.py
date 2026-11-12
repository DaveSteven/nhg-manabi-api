from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.content_cleanup import (
    clean_option_text,
    strip_moji_markup,
    strip_moji_markup_in_db,
    strip_moji_subtitle_markup,
)
from app.database import Base
from app.models import ExamOption, ExamQuestion


def test_strip_moji_markup() -> None:
    assert strip_moji_markup('<p data-x="1">質問</p><MOJiTest_URL><MOJiTest_URL>') == '<p data-x="1">質問</p>'
    assert strip_moji_markup("<br></MOJiTest_URL>本文") == "<br>本文"
    assert strip_moji_markup(None) == ""


def test_strip_moji_markup_removes_class_and_empty_timing_attrs() -> None:
    assert (
        strip_moji_markup('<p class="moji-content" data-starttime="" data-endtime="">　（3）</p>')
        == "<p>　（3）</p>"
    )
    assert strip_moji_markup('<p class="moji-trans">译文</p>') == "<p>译文</p>"
    # 属性顺序变化也能清洗
    assert strip_moji_markup('<p data-endtime="" class="moji-content" data-starttime="">文</p>') == "<p>文</p>"
    # 正常 class 不误伤
    assert strip_moji_markup('<p class="content">文</p>') == '<p class="content">文</p>'


def test_strip_moji_subtitle_markup_keeps_timing_attrs() -> None:
    # 红线：真实时间轴是听力分段功能数据，必须保留
    subtitle = '<p class="moji-content" data-starttime="00:00:04,280" data-endtime="00:00:15,980">靴下の店</p>'
    assert (
        strip_moji_subtitle_markup(subtitle)
        == '<p data-starttime="00:00:04,280" data-endtime="00:00:15,980">靴下の店</p>'
    )
    # subtitle 里空值属性也不删（保持现状语义，分段逻辑自行跳过）
    subtitle_empty = '<p class="moji-content" data-starttime="" data-endtime="">無音</p>'
    assert (
        strip_moji_subtitle_markup(subtitle_empty)
        == '<p data-starttime="" data-endtime="">無音</p>'
    )


def test_clean_option_text_unwraps_moji_spans() -> None:
    assert (
        clean_option_text('<span class="moji-notation" text="1"><span class="moji-toolkit-org" lemma="一" lemma-t="">1</span></span>')
        == "1"
    )
    assert (
        clean_option_text('<span class="moji-notation" text="店"><ruby n5="">店<rp>)</rp></ruby></span>')
        == "<ruby n5=\"\">店<rp>)</rp></ruby>"
    )


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
                ExamQuestion(
                    external_id="b", exam_id=1, layer_id=1, order_index=2, level="N1", title="干净",
                    passage='<p class="moji-content" data-starttime="" data-endtime="">文章</p>',
                    subtitle='<p class="moji-content" data-starttime="00:01" data-endtime="00:02">字</p>',
                    translation='<p class="moji-trans">翻译</p>',
                ),
            ]
        )
        db.add(
            ExamOption(
                question_id=1, source_order=0,
                text='<span class="moji-notation" text="1"><span class="moji-toolkit-org" lemma="一" lemma-t="">1</span></span>',
            )
        )
        db.commit()

        # a.title + a.passage + b.passage + b.subtitle + b.translation + option = 6
        assert strip_moji_markup_in_db(db) == 6
        assert strip_moji_markup_in_db(db) == 0

        a = db.query(ExamQuestion).filter_by(external_id="a").one()
        assert (a.title, a.passage, a.subtitle) == ("<br>", "本文", "<p>字幕</p>")
        assert a.analysis == "<MOJiTest_URL>解析"
        assert db.query(ExamQuestion).filter_by(external_id="b").one().title == "干净"

        b = db.query(ExamQuestion).filter_by(external_id="b").one()
        assert b.passage == "<p>文章</p>"
        # 字幕时间轴属性保留
        assert b.subtitle == '<p data-starttime="00:01" data-endtime="00:02">字</p>'
        assert b.translation == "<p>翻译</p>"
        option = db.query(ExamOption).one()
        assert option.text == "1"
