import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.orm import selectinload

from app.content_cleanup import strip_moji_markup
from app.database import Base, SessionLocal, engine
from app.main import ASSETS_DIR, ensure_default_user
from app.models import Exam, ExamLayer, ExamOption, ExamQuestion


EXAM_DATA_DIR = Path(
    os.getenv(
        "EXAM_DATA_DIR",
        Path(__file__).resolve().parents[2] / "jlpt-spider" / "data" / "normalized",
    )
).resolve()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def stored_asset_path(path: str) -> str:
    if not path:
        return ""
    raw_path = Path(path)
    if not raw_path.is_absolute():
        return raw_path.as_posix()
    try:
        return raw_path.resolve().relative_to(ASSETS_DIR.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"asset path is outside EXAM_ASSETS_DIR: {path}") from exc


def discover_levels(data_dir: Path) -> list[Path]:
    levels = sorted(path for path in data_dir.glob("N[1-5]") if path.is_dir())
    if not levels:
        raise RuntimeError(f"no normalized level directories under {data_dir}")
    return levels


def main() -> None:
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        if engine.url.drivername.startswith("postgresql"):
            db.execute(text("ALTER TABLE exams ALTER COLUMN exam_date TYPE BIGINT"))
            db.execute(text("ALTER TABLE exam_layers ALTER COLUMN external_id TYPE VARCHAR(64)"))
            db.execute(text("ALTER TABLE exam_questions ALTER COLUMN external_id TYPE VARCHAR(64)"))
            db.execute(text("ALTER TABLE exam_questions ALTER COLUMN parent_external_id TYPE VARCHAR(64)"))
            db.commit()
        ensure_default_user(db)
        totals = {"exams": 0, "layers": 0, "questions": 0, "options": 0}

        for level_dir in discover_levels(EXAM_DATA_DIR):
            exams_path = level_dir / "exams.json"
            questions_path = level_dir / "questions.json"
            if not exams_path.is_file() or not questions_path.is_file():
                raise RuntimeError(f"incomplete normalized data: {level_dir}")

            source_exams = load_json(exams_path)
            source_questions = load_json(questions_path)
            questions_by_exam: dict[str, list[dict[str, Any]]] = {}
            for question in source_questions:
                questions_by_exam.setdefault(question["exam_id"], []).append(question)

            for source_exam in source_exams:
                external_id = source_exam["exam_id"]
                exam = db.scalar(select(Exam).where(Exam.external_id == external_id))
                if exam is None:
                    exam = Exam(external_id=external_id)
                    db.add(exam)
                exam.level = source_exam["level"]
                exam.title = source_exam.get("title") or ""
                exam.exam_date = source_exam.get("date")
                exam.exam_type = source_exam.get("type") or ""
                exam.is_published = 1 if source_exam.get("isPublished", True) else 0
                exam.media_id = source_exam.get("mediaId") or ""
                exam.media_path = stored_asset_path(source_exam.get("mediaPath") or "")
                exam.vocabulary_num = source_exam.get("vocabularyNum")
                exam.grammar_num = source_exam.get("grammarNum")
                exam.reading_num = source_exam.get("readingNum")
                exam.listening_num = source_exam.get("listeningNum")
                exam.duration_json = json.dumps(source_exam.get("duration") or {}, ensure_ascii=False)
                exam.layer_count = len(source_exam.get("layers") or [])
                exam.question_count = source_exam.get("question_count") or 0
                db.flush()
                totals["exams"] += 1

                layers: dict[str, ExamLayer] = {}
                for source_layer in source_exam.get("layers") or []:
                    layer_id = source_layer["layer_id"]
                    layer = db.scalar(select(ExamLayer).where(ExamLayer.external_id == layer_id))
                    if layer is None:
                        layer = ExamLayer(external_id=layer_id)
                        db.add(layer)
                    layer.exam_id = exam.id
                    layer.order_index = source_layer["order_index"]
                    layer.title = source_layer.get("title") or ""
                    layer.question_type = source_layer.get("questionType")
                    layer.data_type = source_layer.get("dataType")
                    layer.questions_num = source_layer.get("questionsNum")
                    layer.question_count = source_layer.get("question_count") or 0
                    layer.image_path = stored_asset_path(source_layer.get("image_path") or "")
                    db.flush()
                    layers[layer_id] = layer
                    totals["layers"] += 1

                for order_index, source_question in enumerate(questions_by_exam.get(external_id, []), 1):
                    layer = layers.get(source_question["layer_id"])
                    if layer is None:
                        raise ValueError(f"question references missing layer: {source_question['question_id']}")
                    question_id = source_question["question_id"]
                    question = db.scalar(
                        select(ExamQuestion).where(ExamQuestion.external_id == question_id)
                    )
                    if question is None:
                        question = ExamQuestion(external_id=question_id)
                        db.add(question)
                    question.exam_id = exam.id
                    question.layer_id = layer.id
                    question.order_index = order_index
                    question.level = source_exam["level"]
                    layer_type = layer.question_type or 0
                    question.question_type = (
                        layer_type if 41 <= layer_type <= 46 else source_question.get("question_type")
                    )
                    question.data_type = source_question.get("data_type")
                    question.identity_json = json.dumps(source_question.get("identity") or [], ensure_ascii=False)
                    question.title = strip_moji_markup(source_question.get("title"))
                    question.passage = strip_moji_markup(source_question.get("passage"))
                    question.analysis = source_question.get("analysis") or ""
                    question.translation = source_question.get("translation") or ""
                    question.subtitle = strip_moji_markup(source_question.get("subtitle"))
                    question.parent_external_id = source_question.get("parent_id") or ""
                    question.image_id = source_question.get("image_id") or ""
                    question.image_path = stored_asset_path(source_question.get("image_path") or "")
                    question.media_id = source_question.get("media_id") or ""
                    question.media_path = stored_asset_path(source_question.get("media_path") or "")
                    question.created_at_text = source_question.get("created_at") or ""
                    question.updated_at_text = source_question.get("updated_at") or ""
                    question.answer_status = "unverified"
                    question.analysis_status = "missing"
                    question.analysis_document_json = "{}"
                    question.review_note = ""
                    question.reviewed_by = None
                    question.reviewed_at = None
                    question.answer_version = 1
                    db.flush()
                    totals["questions"] += 1

                    options = source_question.get("options") or []
                    try:
                        right_answer = int(source_question.get("right_answer"))
                    except (TypeError, ValueError):
                        right_answer = -1
                    existing = {
                        option.source_order: option
                        for option in db.scalars(
                            select(ExamOption).where(ExamOption.question_id == question.id)
                        )
                    }
                    for option_index, option_text in enumerate(options):
                        option = existing.pop(option_index, None)
                        if option is None:
                            option = ExamOption(question_id=question.id, source_order=option_index)
                            db.add(option)
                        option.text = str(option_text)
                        option.is_correct = 1 if option_index == right_answer else 0
                        totals["options"] += 1
                    if existing:
                        db.execute(
                            delete(ExamOption).where(ExamOption.id.in_([item.id for item in existing.values()]))
                        )

        layers = list(db.scalars(select(ExamLayer).options(selectinload(ExamLayer.questions))))
        for layer in layers:
            rows = sorted(layer.questions, key=lambda item: item.order_index)
            image_paths = [question.image_path for question in rows if question.image_path]
            if len(rows) > 1 and len(image_paths) == len(rows) and len(set(image_paths)) == 1:
                layer.image_path = image_paths[0]
                for question in rows:
                    question.image_id = ""
                    question.image_path = ""

        db.commit()

    print(
        "imported "
        f"{totals['exams']} exams, {totals['layers']} layers, "
        f"{totals['questions']} questions, {totals['options']} options"
    )


if __name__ == "__main__":
    main()
