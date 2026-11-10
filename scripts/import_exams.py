import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.database import Base, SessionLocal, engine
from app.content_cleanup import strip_moji_markup
from app.main import ASSETS_DIR, ensure_default_user
from app.models import Exam, ExamLayer, ExamOption, ExamQuestion


EXAM_DATA_DIR = Path(
    os.getenv(
        "EXAM_DATA_DIR",
        Path(__file__).resolve().parents[2]
        / ".."
        / "mojitest_spider"
        / "data"
        / "normalized",
    )
).resolve()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def stored_asset_path(path: str) -> str:
    if not path:
        return ""
    raw_path = Path(path)
    try:
        return str(raw_path.resolve().relative_to(ASSETS_DIR.resolve()))
    except ValueError:
        marker = "/data/assets/"
        if marker in path:
            return path.split(marker, 1)[1]
        return path


def discover_levels(data_dir: Path) -> list[Path]:
    return sorted(path for path in data_dir.iterdir() if path.is_dir())


def main() -> None:
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        if engine.url.drivername.startswith("postgresql"):
            db.execute(text("ALTER TABLE exams ALTER COLUMN exam_date TYPE BIGINT"))
            db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS passage TEXT NOT NULL DEFAULT ''"))
            db.commit()
        ensure_default_user(db)
        exam_total = 0
        layer_total = 0
        question_total = 0
        option_total = 0

        for level_dir in discover_levels(EXAM_DATA_DIR):
            exams_path = level_dir / "exams_with_assets.json"
            questions_path = level_dir / "questions_with_assets.json"
            if not exams_path.exists() or not questions_path.exists():
                continue

            level = level_dir.name
            source_exams = load_json(exams_path)
            source_questions = load_json(questions_path)
            questions_by_exam: dict[str, list[dict[str, Any]]] = {}
            for question in source_questions:
                questions_by_exam.setdefault(question["exam_id"], []).append(question)

            for source_exam in source_exams:
                exam = db.scalar(
                    select(Exam).where(Exam.external_id == source_exam["objectId"])
                )
                if not exam:
                    exam = Exam(external_id=source_exam["objectId"])
                    db.add(exam)

                exam.level = level
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
                exam_total += 1

                layer_by_external_id: dict[str, ExamLayer] = {}
                for order_index, source_layer in enumerate(source_exam.get("layers") or [], start=1):
                    layer = db.scalar(
                        select(ExamLayer).where(
                            ExamLayer.external_id == source_layer["objectId"]
                        )
                    )
                    if not layer:
                        layer = ExamLayer(external_id=source_layer["objectId"])
                        db.add(layer)
                    layer.exam_id = exam.id
                    layer.order_index = order_index
                    layer.title = source_layer.get("title") or ""
                    layer.question_type = source_layer.get("questionType")
                    layer.data_type = source_layer.get("dataType")
                    layer.questions_num = source_layer.get("questionsNum")
                    layer.question_count = source_layer.get("question_count") or 0
                    db.flush()
                    layer_by_external_id[layer.external_id] = layer
                    layer_total += 1

                for order_index, source_question in enumerate(
                    questions_by_exam.get(exam.external_id, []),
                    start=1,
                ):
                    layer = layer_by_external_id.get(source_question.get("layer_id"))
                    if not layer:
                        continue
                    question = db.scalar(
                        select(ExamQuestion).where(
                            ExamQuestion.external_id == source_question["question_id"]
                        )
                    )
                    if not question:
                        question = ExamQuestion(external_id=source_question["question_id"])
                        db.add(question)
                    question.exam_id = exam.id
                    question.layer_id = layer.id
                    question.order_index = order_index
                    question.level = level
                    # 听力（41-46）的题型属于整个大题（layer），以 layer 为准；
                    # 源数据里部分即時応答题被错标成 44，重导入时防止复发。
                    layer_type = layer.question_type or 0
                    if 41 <= layer_type <= 46:
                        question.question_type = layer_type
                    else:
                        question.question_type = source_question.get("question_type")
                    question.data_type = source_question.get("data_type")
                    question.identity_json = json.dumps(
                        source_question.get("identity") or [], ensure_ascii=False
                    )
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
                    db.flush()
                    question_total += 1

                    right_answer = source_question.get("right_answer")
                    try:
                        right_answer_index = int(right_answer)
                    except (TypeError, ValueError):
                        right_answer_index = -1

                    for option_index, option_text in enumerate(source_question.get("options") or []):
                        option = db.scalar(
                            select(ExamOption).where(
                                ExamOption.question_id == question.id,
                                ExamOption.source_order == option_index,
                            )
                        )
                        if not option:
                            option = ExamOption(
                                question_id=question.id,
                                source_order=option_index,
                            )
                            db.add(option)
                        option.text = option_text
                        option.is_correct = 1 if option_index == right_answer_index else 0
                        option_total += 1

        layers = list(db.scalars(select(ExamLayer).options(selectinload(ExamLayer.questions))))
        for layer in layers:
            questions = sorted(layer.questions, key=lambda item: item.order_index)
            image_paths = [question.image_path for question in questions if question.image_path]
            if len(questions) > 1 and len(image_paths) == len(questions) and len(set(image_paths)) == 1:
                layer.image_path = image_paths[0]
                for question in questions:
                    question.image_id = ""
                    question.image_path = ""

        recipe_layer = next((layer for layer in layers if layer.external_id == "sHDAKnyXHO"), None)
        if recipe_layer:
            recipe_layer.image_path = "images/924f57ea-ceea-421f-bd82-934d848521a2.png"
            for question in recipe_layer.questions:
                if question.external_id == "d7GXT2AEDj":
                    question.image_id = ""
                    question.image_path = ""

        db.commit()

    print(
        f"imported {exam_total} exams, {layer_total} layers, "
        f"{question_total} questions, {option_total} options"
    )


if __name__ == "__main__":
    main()
