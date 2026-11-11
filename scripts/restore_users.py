"""Restore login accounts only; never copy sessions or learning data."""

from __future__ import annotations

import argparse
from uuid import uuid4

from sqlalchemy import MetaData, create_engine, delete, func, insert, select


EMPTY_TARGET_TABLES = (
    "user_sessions",
    "attempts",
    "attempt_answers",
    "wrong_questions",
    "exam_attempts",
    "exam_attempt_answers",
    "exam_runs",
    "exam_run_answers",
    "practice_attempts",
    "practice_attempt_answers",
    "exam_wrong_questions",
    "favorites",
    "exam_question_revisions",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    source_engine = create_engine(args.source)
    target_engine = create_engine(args.target)
    if source_engine.url.render_as_string(False) == target_engine.url.render_as_string(False):
        raise ValueError("source and target databases must differ")

    source_meta, target_meta = MetaData(), MetaData()
    source_meta.reflect(bind=source_engine)
    target_meta.reflect(bind=target_engine)
    source_users = source_meta.tables["users"]
    target_users = target_meta.tables["users"]

    with source_engine.connect() as source, target_engine.begin() as target:
        nonempty = {
            name: target.scalar(select(func.count()).select_from(target_meta.tables[name]))
            for name in EMPTY_TARGET_TABLES
            if target.scalar(select(func.count()).select_from(target_meta.tables[name]))
        }
        if nonempty:
            raise ValueError(f"target contains user data; refusing account-only restore: {nonempty}")

        accounts = [dict(row._mapping) for row in source.execute(select(source_users))]
        target.execute(delete(target_users))
        target.execute(
            insert(target_users),
            [
                {
                    "id": str(uuid4()),
                    "username": row["username"],
                    "display_name": row["display_name"],
                    "password_hash": row["password_hash"],
                    "current_level": row["current_level"],
                    "is_admin": row["is_admin"],
                    "created_at": row["created_at"],
                    "last_active_at": None,
                }
                for row in accounts
            ],
        )
    print(f"restored {len(accounts)} accounts; sessions and learning data remain empty")


if __name__ == "__main__":
    main()
