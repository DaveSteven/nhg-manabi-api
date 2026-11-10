from app.listening import listening_segments

SUB = ('<p data-starttime="00:00:01,500" data-endtime="00:00:03,000">こんにちは</p>'
       '<p data-starttime="00:00:04,000" data-endtime="00:01:05,250">さようなら</p>')


def test_segments_pair_translation_by_position():
    result = listening_segments(SUB, '<p>你好</p><p>再见</p>')
    assert result == [
        {"start": 1.5, "end": 3.0, "text": "こんにちは", "translation": "你好"},
        {"start": 4.0, "end": 65.25, "text": "さようなら", "translation": "再见"},
    ]


def test_mismatched_translation_is_not_guessed():
    result = listening_segments(SUB, '<p>你好</p>')
    assert [s["translation"] for s in result] == ["", ""]


def test_invalid_segment_dropped_without_shifting_translation():
    sub = '<p data-starttime="bad" data-endtime="x">坏</p>' + SUB
    result = listening_segments(sub, '<p>坏译</p><p>你好</p><p>再见</p>')
    assert [s["translation"] for s in result] == ["你好", "再见"]


def test_no_subtitle():
    assert listening_segments("", "<p>x</p>") == []


def test_question_out_hides_subtitle_until_review():
    from types import SimpleNamespace as NS
    from app.main import exam_question_to_out
    q = NS(id=1, external_id="x", layer_id=1, layer=NS(external_id="l"), order_index=1, question_type=41,
           data_type=1, title="t", passage="", subtitle=SUB, translation="<p>a</p><p>b</p>", analysis="",
           image_path="", media_path="", options=[])
    public = exam_question_to_out(q)
    assert public.subtitle == "" and public.segment_count == 2
    assert not hasattr(public, "translation")
    assert exam_question_to_out(q, include_review_fields=True).subtitle == SUB
