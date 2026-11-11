"""Validate that every asset path referenced by the exam tables exists."""

from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.main import ASSETS_DIR
from app.models import Exam, ExamLayer, ExamQuestion


def main() -> None:
    with SessionLocal() as db:
        paths = {
            path
            for column in (
                Exam.media_path,
                ExamLayer.image_path,
                ExamQuestion.image_path,
                ExamQuestion.media_path,
            )
            for path in db.scalars(select(column))
            if path
        }
    missing = [path for path in sorted(paths) if not (Path(ASSETS_DIR) / path).is_file()]
    print(f"validated {len(paths)} referenced asset paths; missing={len(missing)}")
    if missing:
        for path in missing[:20]:
            print(f"missing: {path}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
