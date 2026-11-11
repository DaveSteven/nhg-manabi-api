"""Restore login accounts only; never copy sessions or learning data.

拆表（T-020 / D12）后的口径：学员入 `users`，管理员入 `admin_users`（各自生成新 UUID）。
源库兼容新旧两种 schema：含 `is_admin` 列的旧库按该列拆分，新库全部视为学员。
"""

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
    target_admins = target_meta.tables["admin_users"]
    source_has_is_admin = "is_admin" in source_users.columns

    with source_engine.connect() as source, target_engine.begin() as target:
        nonempty = {
            name: target.scalar(select(func.count()).select_from(target_meta.tables[name]))
            for name in EMPTY_TARGET_TABLES
            if target.scalar(select(func.count()).select_from(target_meta.tables[name]))
        }
        if nonempty:
            raise ValueError(f"target contains user data; refusing account-only restore: {nonempty}")

        accounts = [dict(row._mapping) for row in source.execute(select(source_users))]
        if source_has_is_admin:
            learners = [row for row in accounts if not row["is_admin"]]
            admins = [row for row in accounts if row["is_admin"]]
        else:
            learners, admins = accounts, []

        target.execute(delete(target_users))
        if learners:
            # 空列表不能执行 insert：SQLAlchemy 会生成 INSERT ... DEFAULT VALUES
            target.execute(
                insert(target_users),
                [
                    {
                        "id": str(uuid4()),
                        "username": row["username"],
                        "display_name": row["display_name"],
                        "password_hash": row["password_hash"],
                        "current_level": row["current_level"],
                        "created_at": row["created_at"],
                        "last_active_at": None,
                    }
                    for row in learners
                ],
            )
        target.execute(delete(target_admins))
        if admins:
            target.execute(
                insert(target_admins),
                [
                    {
                        "id": str(uuid4()),
                        "username": row["username"],
                        "display_name": row["display_name"],
                        "password_hash": row["password_hash"],
                        "created_at": row["created_at"],
                    }
                    for row in admins
                ],
            )
    print(
        f"restored {len(learners)} learner accounts and {len(admins)} admin accounts; "
        "sessions and learning data remain empty"
    )


if __name__ == "__main__":
    main()
