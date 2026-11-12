import hashlib
import os
import re
import random
import json
import secrets
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .database import Base, engine, get_db
from .models import (
    AdminUser,
    AdminUserSession,
    Attempt,
    AttemptAnswer,
    Book,
    Exam,
    ExamAttempt,
    ExamAttemptAnswer,
    ExamLayer,
    ExamOption,
    ExamQuestion,
    ExamQuestionRevision,
    ExamWrongQuestion,
    ExamRun,
    ExamRunAnswer,
    Favorite,
    Option,
    PracticeAttempt,
    PracticeAttemptAnswer,
    Question,
    User,
    UserSession,
    WrongQuestion,
)
from .schemas import (
    AttemptAnswerOut,
    AdminExamQuestionDetailOut,
    AdminExamQuestionListItemOut,
    AdminExamQuestionListOut,
    AdminExamQuestionRevisionOut,
    AdminExamLayerQuestionsOut,
    AdminExamListItemOut,
    AdminExamListOut,
    AdminExamQuestionUpdateIn,
    AdminMeOut,
    AdminSessionOut,
    CheckAnswerIn,
    CheckAnswerOut,
    AttemptIn,
    AttemptOut,
    BookOut,
    ExamAttemptIn,
    ExamAttemptOut,
    ExamAttemptAnswerOut,
    ListeningCheckOut,
    ExamAttemptAnswerIn,
    ExamDetailOut,
    ExamLayerOut,
    ExamOptionOut,
    ExamOut,
    ExamQuestionOut,
    ExamQuestionReviewOut,
    ExamWrongQuestionOut,
    ExamRunOut,
    ExamRunAnswerIn,
    ExamSectionOut,
    FavoriteIdsOut,
    FavoriteOut,
    FavoriteToggleIn,
    RandomExamQuestionOut,
    LoginIn,
    QuizOptionOut,
    QuizOut,
    QuizQuestionOut,
    PracticeAttemptIn,
    PracticeAttemptOut,
    QuestionSearchItemOut,
    QuestionSearchOptionOut,
    QuestionSearchOut,
    SessionOut,
    UserOut,
    UserUpdate,
    WrongQuestionOut,
)
from .stats import create_stats_router
from .security import hash_password, verify_password
from .admin_users import create_admin_users_router
from .admin_stats import create_admin_stats_router
from .content_cleanup import strip_moji_markup_in_db
from .listening import listening_segments
from . import rate_limit


# 只在全新空库时用来创建开发用 C 端账号 david；不设置则不创建。
DEFAULT_DAVID_PASSWORD = os.getenv("DEFAULT_DAVID_PASSWORD", "")
# 后台种子管理员：库里还没有任何管理员时，用这两个环境变量创建一个。
DEFAULT_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()
DEFAULT_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SCOPE_APP = "app"
SESSION_DAYS = 30
JLPT_LEVELS = {"N5", "N4", "N3", "N2", "N1"}
JLPT_SECTION_TIMES = {
    "N1": [("language_reading", "语言知识・阅读", 110)],
    "N2": [("language_reading", "语言知识・阅读", 105)],
    "N3": [("vocabulary", "文字词汇", 30), ("grammar_reading", "文法・阅读", 70)],
    "N4": [("vocabulary", "文字词汇", 25), ("grammar_reading", "文法・阅读", 55)],
    "N5": [("vocabulary", "文字词汇", 20), ("grammar_reading", "文法・阅读", 40)],
}
JLPT_LISTENING_TIMES = {"N1": 55, "N2": 50, "N3": 40, "N4": 35, "N5": 30}
ASSETS_DIR = Path(
    os.getenv(
        "EXAM_ASSETS_DIR",
        Path(__file__).resolve().parents[1] / "exam_assets",
    )
)

# 接口文档会暴露完整的接口清单，默认关闭；本地开发设置 API_DOCS=1 打开。
_docs_enabled = os.getenv("API_DOCS", "0") == "1"
app = FastAPI(
    title="Japanese Study API",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)


@app.middleware("http")
async def ip_rate_limit(request: Request, call_next):
    """每个 IP 的 /api 请求总量限流（防批量抓取、防刷接口）。"""
    path = request.url.path
    if path.startswith("/api/") and path != "/api/health" and request.method != "OPTIONS":
        retry_after = rate_limit.check_ip_quota(rate_limit.client_ip(request))
        if retry_after is not None:
            return JSONResponse(
                {"detail": "Too many requests, please try again later"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
    return await call_next(request)

origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "*").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        migrate_user_ids_to_uuid(db)
        ensure_schema(db)
        ensure_user_last_active_at(db)
        normalize_exam_layer_images(db)
        strip_moji_markup_in_db(db)
        migrate_session_tokens(db)
        split_admin_accounts(db)
        ensure_default_user(db)
        ensure_default_admin(db)


def _referencing_user_columns(db: Session) -> list[tuple[str, str]]:
    """从数据库目录动态发现所有引用 users.id 的 (表, 列)。

    不维护静态清单：生产库里可能存在当前代码已不写的遗留表
    （如 user_vocabulary_progress），动态发现可以一并迁移。
    """
    if engine.url.drivername.startswith("postgresql"):
        rows = db.execute(
            text(
                "SELECT cl.relname AS table_name, att.attname AS column_name "
                "FROM pg_constraint c "
                "JOIN pg_class cl ON cl.oid = c.conrelid "
                "JOIN unnest(c.conkey) AS k(attnum) ON true "
                "JOIN pg_attribute att ON att.attrelid = c.conrelid AND att.attnum = k.attnum "
                "WHERE c.confrelid = 'users'::regclass AND c.contype = 'f'"
            )
        ).all()
        return [(r[0], r[1]) for r in rows]

    conn = db.connection()
    pairs: list[tuple[str, str]] = []
    tables = [r[0] for r in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table'").all()]
    for table in tables:
        # 行结构: (id, seq, 被引用表, 本表列, 被引用列, on_update, on_delete, match)
        for row in conn.exec_driver_sql(f"PRAGMA foreign_key_list({table})").all():
            if row[2] == "users":
                pairs.append((table, row[3]))
    return pairs


def _user_id_is_text(db: Session) -> bool:
    """users.id 已经是 UUID 文本主键时返回 True，说明迁移已完成或全新库。"""
    if engine.url.drivername.startswith("postgresql"):
        data_type = db.scalar(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'id'"
            )
        )
        return (data_type or "").lower() in ("character varying", "text", "character")
    row = db.connection().exec_driver_sql("PRAGMA table_info(users)").all()
    id_row = next((r for r in row if r[1] == "id"), None)
    if id_row is None:
        return True
    return "INT" not in str(id_row[2]).upper()


def _remap_user_refs(
    db: Session, mapping: dict[str, str], ref_columns: list[tuple[str, str]]
) -> None:
    """把所有引用表里的旧 user id 替换成新 UUID（调用方需已解除外键约束）。"""
    for table, column in ref_columns:
        for old, new in mapping.items():
            db.execute(
                text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
                {"new": new, "old": old},
            )


def migrate_user_ids_to_uuid(db: Session) -> None:
    """一次性迁移：把 users 自增 int 主键改成 UUID 文本，并同步所有引用列。

    新库（users.id 已是文本）直接跳过；迁移后 admin/学习端接口统一返回 UUID。
    """
    if _user_id_is_text(db):
        return

    db.commit()
    conn = db.connection()

    users = conn.exec_driver_sql("SELECT id, username, display_name, password_hash, "
                                 "current_level, is_admin, created_at, last_active_at "
                                 "FROM users").fetchall()
    mapping = {str(row[0]): str(uuid4()) for row in users}
    ref_columns = _referencing_user_columns(db)

    if engine.url.drivername.startswith("postgresql"):
        fks = db.execute(
            text(
                "SELECT conname, conrelid::regclass::text, pg_get_constraintdef(oid) "
                "FROM pg_constraint WHERE confrelid = 'users'::regclass AND contype = 'f'"
            )
        ).all()
        for name, _table, _definition in fks:
            db.execute(text(f'ALTER TABLE {_table} DROP CONSTRAINT "{name}"'))
        db.execute(text("ALTER TABLE users ALTER COLUMN id DROP DEFAULT"))
        db.execute(text("ALTER TABLE users ALTER COLUMN id TYPE VARCHAR(36) USING id::text"))
        for table, column in ref_columns:
            db.execute(
                text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR(36) "
                     f"USING {column}::text")
            )
        for old, new in mapping.items():
            db.execute(text("UPDATE users SET id = :new WHERE id = :old"), {"new": new, "old": old})
        db.flush()
        _remap_user_refs(db, mapping, ref_columns)
        for name, table, definition in fks:
            db.execute(text(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" {definition}'))
        db.execute(text("DROP SEQUENCE IF EXISTS users_id_seq"))
        db.commit()
        print(f"[startup] users.id migrated to UUID ({len(mapping)} users, "
              f"{len(ref_columns)} ref columns)")
        return

    # SQLite：users.id 是 INTEGER PRIMARY KEY（rowid 别名），无法直接存文本，
    # 必须重建 users 表；引用表只需在关闭外键时原地改写值。
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    conn.exec_driver_sql(
        """
        CREATE TABLE users_new (
            id VARCHAR(36) NOT NULL,
            username VARCHAR(64) NOT NULL,
            display_name VARCHAR(128) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            current_level VARCHAR(16) NOT NULL,
            is_admin BOOLEAN NOT NULL,
            created_at DATETIME,
            last_active_at DATETIME,
            PRIMARY KEY (id),
            UNIQUE (username)
        )
        """
    )
    for row in users:
        conn.exec_driver_sql(
            "INSERT INTO users_new (id, username, display_name, password_hash, "
            "current_level, is_admin, created_at, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mapping[str(row[0])], *row[1:]),
        )
    conn.exec_driver_sql("DROP TABLE users")
    conn.exec_driver_sql("ALTER TABLE users_new RENAME TO users")
    _remap_user_refs(db, mapping, ref_columns)
    violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        print(f"[startup] WARNING: foreign_key_check reported {len(violations)} violations after UUID migration")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    db.commit()
    print(f"[startup] users.id migrated to UUID ({len(mapping)} users, "
          f"{len(ref_columns)} ref columns)")


def ensure_user_last_active_at(db: Session) -> None:
    """新增 users.last_active_at 列并从历史做题/登录记录回填（仅首次执行）。"""
    if engine.url.drivername.startswith("postgresql"):
        exists = db.scalar(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'last_active_at'"
            )
        )
        if not exists:
            db.execute(text("ALTER TABLE users ADD COLUMN last_active_at TIMESTAMP"))
            db.execute(
                text(
                    "UPDATE users SET last_active_at = ("
                    "SELECT MAX(t) FROM ("
                    "SELECT MAX(created_at) AS t FROM attempts WHERE user_id = users.id "
                    "UNION ALL SELECT MAX(created_at) FROM practice_attempts WHERE user_id = users.id "
                    "UNION ALL SELECT MAX(created_at) FROM exam_attempts WHERE user_id = users.id "
                    "UNION ALL SELECT MAX(created_at) FROM user_sessions WHERE user_id = users.id"
                    ") sub)"
                )
            )
            db.commit()
        return

    columns = {
        row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(users)").all()
    }
    if "last_active_at" not in columns:
        db.connection().exec_driver_sql("ALTER TABLE users ADD COLUMN last_active_at DATETIME")
        db.execute(
            text(
                "UPDATE users SET last_active_at = ("
                "SELECT MAX(t) FROM ("
                "SELECT MAX(created_at) AS t FROM attempts WHERE user_id = users.id "
                "UNION ALL SELECT MAX(created_at) FROM practice_attempts WHERE user_id = users.id "
                "UNION ALL SELECT MAX(created_at) FROM exam_attempts WHERE user_id = users.id "
                "UNION ALL SELECT MAX(created_at) FROM user_sessions WHERE user_id = users.id"
                ") sub)"
            )
        )
        db.commit()


def ensure_schema(db: Session) -> None:
    if engine.url.drivername.startswith("postgresql"):
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS current_level VARCHAR(16) NOT NULL DEFAULT 'N5'"))
        db.execute(text("ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'app'"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS passage TEXT NOT NULL DEFAULT ''"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS answer_status VARCHAR(32) NOT NULL DEFAULT 'unverified'"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS analysis_status VARCHAR(32) NOT NULL DEFAULT 'missing'"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS analysis_document_json TEXT NOT NULL DEFAULT '{}'"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS review_note TEXT NOT NULL DEFAULT ''"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(36)"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS answer_version INTEGER NOT NULL DEFAULT 1"))
        db.execute(text("CREATE INDEX IF NOT EXISTS ix_exam_questions_answer_status ON exam_questions(answer_status)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS ix_exam_questions_analysis_status ON exam_questions(analysis_status)"))
        db.execute(text("ALTER TABLE exam_layers ADD COLUMN IF NOT EXISTS image_path TEXT NOT NULL DEFAULT ''"))
        db.execute(text("ALTER TABLE exam_attempt_answers ALTER COLUMN chosen_option_id DROP NOT NULL"))
        # T-011：三张 answer 表逐条作答时间；存量行 NULL = 未采集
        for answer_table in ("attempt_answers", "exam_attempt_answers", "practice_attempt_answers"):
            db.execute(text(f"ALTER TABLE {answer_table} ADD COLUMN IF NOT EXISTS created_at TIMESTAMP"))
        db.execute(text("ALTER TABLE exam_runs ADD COLUMN IF NOT EXISTS attempt_id INTEGER REFERENCES exam_attempts(id)"))
        db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_active_exam_run ON exam_runs(user_id, exam_id) WHERE status IN ('in_progress', 'awaiting_section')"))
        db.commit()
        return

    if not engine.url.drivername.startswith("sqlite"):
        return

    columns = {
        row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(users)").all()
    }
    if "password_hash" not in columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) NOT NULL DEFAULT ''"
        )
        db.commit()
    if "current_level" not in columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE users ADD COLUMN current_level VARCHAR(16) NOT NULL DEFAULT 'N5'"
        )
        db.commit()
    session_columns = {
        row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(user_sessions)").all()
    }
    if "scope" not in session_columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE user_sessions ADD COLUMN scope VARCHAR(16) NOT NULL DEFAULT 'app'"
        )
        db.commit()

    question_columns = {
        row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(exam_questions)").all()
    }
    if "passage" not in question_columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE exam_questions ADD COLUMN passage TEXT NOT NULL DEFAULT ''"
        )
        db.commit()
    admin_question_columns = {
        "answer_status": "VARCHAR(32) NOT NULL DEFAULT 'unverified'",
        "analysis_status": "VARCHAR(32) NOT NULL DEFAULT 'missing'",
        "analysis_document_json": "TEXT NOT NULL DEFAULT '{}'",
        "review_note": "TEXT NOT NULL DEFAULT ''",
        "reviewed_by": "VARCHAR(36)",
        "reviewed_at": "DATETIME",
        "answer_version": "INTEGER NOT NULL DEFAULT 1",
    }
    for column, definition in admin_question_columns.items():
        if column not in question_columns:
            db.connection().exec_driver_sql(
                f"ALTER TABLE exam_questions ADD COLUMN {column} {definition}"
            )
    db.commit()

    layer_columns = {
        row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(exam_layers)").all()
    }
    if layer_columns and "image_path" not in layer_columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE exam_layers ADD COLUMN image_path TEXT NOT NULL DEFAULT ''"
        )
        db.commit()

    # T-011：三张 answer 表逐条作答时间；存量行 NULL = 未采集
    for answer_table in ("attempt_answers", "exam_attempt_answers", "practice_attempt_answers"):
        answer_columns = {
            row[1]
            for row in db.connection().exec_driver_sql(f"PRAGMA table_info({answer_table})").all()
        }
        if answer_columns and "created_at" not in answer_columns:
            db.connection().exec_driver_sql(
                f"ALTER TABLE {answer_table} ADD COLUMN created_at DATETIME"
            )
            db.commit()

    ensure_nullable_exam_chosen_option(db)


# 爬虫（mojitest）把「整页材料图」和「小题专属图（如四人订单表）」随机挂在某个小题上，
# 无法从数据本身推断归属。下表是逐题人工核对的结果（2026-11 对照原图确认）：
# layer external_id -> (大题材料图, 小题专属图, 持有该图的小题 external_id)。
# 无专属图的大题不在此表中；所有小题同图的通用归一化在下方函数里处理。
MIXED_LAYER_IMAGE_FIXES: dict[str, tuple[str, str, str]] = {
    "0PuTAq2zgr": ("images/807a405a-62d3-449a-bc02-255b96443e8d.png", "images/3c7807d9-63aa-4b22-a4f3-46933f4b21e5.png", "vcgtJHFey4"),
    "0jIIlvUUeH": ("images/e18879f0-d223-4d25-b679-d4e88817894e.jpg", "images/356d079d-9641-413a-9e29-3006d4742631.png", "TN1mPl8VRB"),
    "42wfuhFDZj": ("images/b4b3e573-8ee7-418a-8589-684064caee2f.png", "images/2020-05-17_5ec134544453a", "GgsVMsNYxn"),
    "5Az7sW6PIl": ("images/ef2710eb-7e39-4376-98b2-e25bc1bb096b.jpg", "images/335b3ee1-373a-4561-8d95-b5144175a167.jpg", "lGDtkcIawn"),
    "5XWJpu0sJb": ("images/f0cc8c4c-4f90-44c5-9534-07999ce06140.png", "images/a8723719-616f-41fe-9a66-3f34dfe54a65.png", "KIUELJWayJ"),
    "Af5sSJ6mSa": ("images/2020-05-17_5ec13a85b255e", "images/2020-05-17_5ec13ac92361d", "tmjXT44wdy"),
    "C0D6ooPKEA": ("images/3a7ccc9c-b7a4-43f2-afab-2c6e2bd0898c.jpg", "images/2020-03-16_5e6f3f2e6b510", "u6WdT5cH6I"),
    "CJ6A800GrU": ("images/b91690a6-58b1-4290-b8c5-747a840f9c18.png", "images/41d3d552-5e83-4939-8698-579a724e74cd.png", "lZnAAhedRu"),
    "CQgbTJbCQw": ("images/994d0323-bb9c-4eb9-802d-f0af92ca7b8d.jpg", "images/2020-05-17_5ec0c0487c840", "giXrOZj82J"),
    "CcnV4ZrwhP": ("images/a136310a-e019-4b16-bc61-3d366b322f98.jpg", "images/5ce61a0c-2d1a-45bf-b021-d192048dc5f8.jpg", "riNpXRhHxq"),
    "DJ1tG5wHdz": ("images/e419114f-d430-4795-9fce-6e4709d945c1.jpg", "images/cdfb3bc1-671a-4793-8380-2c650e45bb3f.jpg", "RDBWUrbXiD"),
    "F3oBM7i8Nw": ("images/a7382cd9-4224-4b5b-9a4f-b7935726e050.png", "images/bf562f52-34fb-4153-807f-fd664d41671c.jpg", "1qCG34pKQo"),
    "G1O54JqCbQ": ("images/508a207d-caf4-4df9-8605-000b2e806c2b.jpg", "images/5fbf2078-0711-44d7-9453-3159aa3b6a56.jpg", "G2rSoc0bUq"),
    "Ustm0rwgDx": ("images/104289c6-eed3-4fa7-9b31-d9cb86744f5f.png", "images/fc955483-7f51-42ee-b8d5-6557e6aac277.png", "TfkfVa5Fkc"),
    "YVeycZkiH5": ("images/b622dd5f-e84d-438f-96ff-ab6e02ba9e2d.jpg", "images/2020-05-17_5ec09a5597760", "bxjZTx62OJ"),
    "YlnzwjtWbZ": ("images/3ccf245f-76a5-432f-a2dd-78505cb6a642.jpg", "images/df83ebae-2e80-4e06-956f-1fee1bfd8b95.jpg", "tgEYCAzSTm"),
    "chRnu23iNq": ("images/d3af090d-307a-4ab7-98bb-69906f97b8ad.png", "images/e4b6ad2b-b43d-4221-999f-4146bf23c201.png", "NjFyUNOxYm"),
    "hwoVaNfl3i": ("images/c9d3ecea-bfa3-4cee-ab8d-a4c4f81d4d3f.png", "images/bb64c4e7-075e-4c51-bb8c-d249d4ed628c", "3zzp6cZZY2"),
    "k8u9QpiMTQ": ("images/9ae879e2-ab3b-41d4-8c9b-0c2a5d9b45ae.jpg", "images/0649773a-9ec7-4c5d-a3af-2fa813313bbc.jpg", "QS6NG25Nav"),
    "rw7tuXBVtp": ("images/6c3d6820-2204-4f2d-8441-583e7f2838c5.jpg", "images/738e1cb7-082e-420b-91ed-154ecdc067d4.jpg", "4AZgz0OFdf"),
    "sHDAKnyXHO": ("images/924f57ea-ceea-421f-bd82-934d848521a2.png", "images/536e7887-0a1f-43ec-9157-ef2f5c17dd2c.png", "p9F2yMSudH"),
    "wo6M8t5OKz": ("images/12d14564-d0be-4f7f-a24c-ea8f83a04cb6.png", "images/a000d541-ef27-4418-a91e-f47056048e21.jpg", "9uw6tB8oHN"),
    "xYtpAlmCmM": ("images/58bb4c17-fa89-4b8b-bcbf-3d98e00e67d0.png", "images/636803e9-db01-45cb-8fcb-20bcf90d4df1.png", "zumb8AVREa"),
    "ylY6o58Zm4": ("images/324b416a-d5d5-4eb0-b6e7-0e48a3380e26.jpg", "images/49039cd7-534e-427f-8e26-d63d94898e65.jpg", "l5GjTnvg45"),
}


def normalize_exam_layer_images(db: Session) -> None:
    """把读解材料图归位到题组（layer）级，小题只保留属于自己的插图。

    - 所有小题共享同一张材料图 → 上移到 layer，小题清空；
    - 「整页材料 + 小题专属图」混合型 → 按 MIXED_LAYER_IMAGE_FIXES 的人工核对结果归位。
    听力等题组每小题的图是场景图，不动。
    """
    layers = list(
        db.scalars(select(ExamLayer).options(selectinload(ExamLayer.questions)))
    )
    for layer in layers:
        questions = sorted(layer.questions, key=lambda item: item.order_index)
        image_paths = [question.image_path for question in questions if question.image_path]
        if len(questions) > 1 and len(image_paths) == len(questions) and len(set(image_paths)) == 1:
            layer.image_path = image_paths[0]
            for question in questions:
                question.image_id = ""
                question.image_path = ""
            continue

        fix = MIXED_LAYER_IMAGE_FIXES.get(layer.external_id)
        if fix:
            material_path, attachment_path, attachment_question_id = fix
            layer.image_path = material_path
            for question in questions:
                keep = question.external_id == attachment_question_id
                question.image_path = attachment_path if keep else ""
                question.image_id = ""
    db.commit()


def ensure_nullable_exam_chosen_option(db: Session) -> None:
    columns = db.connection().exec_driver_sql("PRAGMA table_info(exam_attempt_answers)").all()
    chosen_column = next((row for row in columns if row[1] == "chosen_option_id"), None)
    if not chosen_column or not chosen_column[3]:
        return

    db.commit()
    db.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
    db.connection().exec_driver_sql(
        """
        CREATE TABLE exam_attempt_answers_new (
            id INTEGER NOT NULL,
            attempt_id INTEGER NOT NULL,
            user_id VARCHAR(36) NOT NULL,
            question_id INTEGER NOT NULL,
            chosen_option_id INTEGER,
            correct_option_id INTEGER NOT NULL,
            is_correct INTEGER NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(attempt_id) REFERENCES exam_attempts (id),
            FOREIGN KEY(user_id) REFERENCES users (id),
            FOREIGN KEY(question_id) REFERENCES exam_questions (id),
            FOREIGN KEY(chosen_option_id) REFERENCES exam_options (id),
            FOREIGN KEY(correct_option_id) REFERENCES exam_options (id)
        )
        """
    )
    db.connection().exec_driver_sql(
        """
        INSERT INTO exam_attempt_answers_new (
            id,
            attempt_id,
            user_id,
            question_id,
            chosen_option_id,
            correct_option_id,
            is_correct
        )
        SELECT
            id,
            attempt_id,
            user_id,
            question_id,
            chosen_option_id,
            correct_option_id,
            is_correct
        FROM exam_attempt_answers
        """
    )
    db.connection().exec_driver_sql("DROP TABLE exam_attempt_answers")
    db.connection().exec_driver_sql("ALTER TABLE exam_attempt_answers_new RENAME TO exam_attempt_answers")
    db.connection().exec_driver_sql("CREATE INDEX ix_exam_attempt_answers_attempt_id ON exam_attempt_answers (attempt_id)")
    db.connection().exec_driver_sql("CREATE INDEX ix_exam_attempt_answers_question_id ON exam_attempt_answers (question_id)")
    db.connection().exec_driver_sql("CREATE INDEX ix_exam_attempt_answers_user_id ON exam_attempt_answers (user_id)")
    db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    db.commit()


def ensure_default_user(db: Session) -> User | None:
    """开发用 C 端账号 david：只在全新空库且配置了 DEFAULT_DAVID_PASSWORD 时创建。

    已删除的 david 不会被重新创建；已存在的 david 只修正为 C 端账号。
    """
    user = db.scalar(select(User).where(User.username == "david"))
    if user:
        return user

    if not DEFAULT_DAVID_PASSWORD or db.scalar(select(User.id).limit(1)) is not None:
        return None

    user = User(
        username="david",
        display_name="David",
        password_hash=hash_password(DEFAULT_DAVID_PASSWORD),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def ensure_default_admin(db: Session) -> None:
    """库里没有管理员且配置了 ADMIN_USERNAME / ADMIN_PASSWORD 时，创建后台种子管理员。"""
    if db.scalar(select(AdminUser.id).limit(1)) is not None:
        return
    if not DEFAULT_ADMIN_USERNAME or not DEFAULT_ADMIN_PASSWORD:
        print("[startup] WARNING: no administrator exists; set ADMIN_USERNAME and ADMIN_PASSWORD to create one")
        return
    # D12 补充口径：管理员与学员是两套账号，跨表重名不校验。
    db.add(
        AdminUser(
            username=DEFAULT_ADMIN_USERNAME,
            display_name=DEFAULT_ADMIN_USERNAME,
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
        )
    )
    db.commit()


def hash_session_token(token: str) -> str:
    """库里只存 token 的 SHA-256，数据库泄露也拿不到可用的登录凭证。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def migrate_session_tokens(db: Session) -> None:
    """把旧的明文 token 原地转成哈希（已登录用户不受影响），并清理过期会话。"""
    db.query(UserSession).filter(UserSession.expires_at < datetime.utcnow()).delete()
    for session in db.scalars(select(UserSession).where(func.length(UserSession.token) != 64)):
        session.token = hash_session_token(session.token)
    db.commit()


# SQLite 建表 SQL 里指向 users 的外键片段（REFERENCES users(id) / REFERENCES "users" ("id") 等）。
_SQLITE_USERS_FK_RE = re.compile(r'REFERENCES\s+"?users"?\s*\([^)]*\)', re.IGNORECASE)


def _users_has_is_admin(db: Session) -> bool:
    if engine.url.drivername.startswith("postgresql"):
        return db.scalar(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'is_admin'"
            )
        )
    columns = {
        row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(users)").all()
    }
    return "is_admin" in columns


def _sqlite_rebuild_table_without_users_fk(db: Session, table: str) -> None:
    """SQLite 无法原地删除外键约束：按 migrate_user_ids_to_uuid 的模式重建表。

    重建时去掉建表 SQL 中指向 users 的外键片段，其余列与数据原样保留；
    重建前后由调用方控制 PRAGMA foreign_keys 开关，子表引用在 rename 时自动跟随。
    """
    conn = db.connection()
    row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row or not row[0]:
        return

    indexes = [
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL",
            (table,),
        ).all()
    ]
    new_sql = _SQLITE_USERS_FK_RE.sub("", row[0])
    columns = [r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})").all()]
    column_list = ", ".join(columns)

    # legacy_alter_table=ON 让 RENAME 不改写子表外键引用：子表继续指向本表原名，
    # 旧表删除后由按原名重建的新表顶上，避免引用悬空到 *_split_old。
    conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    conn.exec_driver_sql(f"ALTER TABLE {table} RENAME TO {table}_split_old")
    conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    conn.exec_driver_sql(new_sql)
    conn.exec_driver_sql(
        f"INSERT INTO {table} ({column_list}) SELECT {column_list} FROM {table}_split_old"
    )
    conn.exec_driver_sql(f"DROP TABLE {table}_split_old")
    for index_sql in indexes:
        conn.exec_driver_sql(index_sql)


def split_admin_accounts(db: Session) -> None:
    """一次性迁移（D12）：把 users 中的管理员拆到 admin_users 独立表，并删除 users.is_admin。

    - 管理员迁入新表时生成新 UUID：现有管理员会话失效需重登（同事务清空 admin scope
      会话），学员会话不受影响；
    - exam_questions.reviewed_by / exam_question_revisions.user_id 语义上是管理员，
      去掉指向 users 的数据库级外键，不做历史回填。

    幂等：users 已无 is_admin 列（新库或已迁移）时直接返回。
    """
    if not _users_has_is_admin(db):
        return

    admins = db.execute(
        text(
            "SELECT username, display_name, password_hash, created_at "
            "FROM users WHERE is_admin"
        )
    ).fetchall()
    for username, display_name, password_hash, created_at in admins:
        db.execute(
            text(
                "INSERT INTO admin_users (id, username, display_name, password_hash, created_at) "
                "VALUES (:id, :username, :display_name, :password_hash, :created_at)"
            ),
            {
                "id": str(uuid4()),
                "username": username,
                "display_name": display_name,
                "password_hash": password_hash,
                "created_at": created_at,
            },
        )
    # 管理员旧 UUID 失效，其 admin 会话一并清除；'app' scope 会话理论上不存在，一并清理以防残留。
    db.execute(text("DELETE FROM user_sessions WHERE scope = 'admin'"))
    db.commit()

    # 先去掉指向 users 的外键，再删除 users 中的管理员行：
    # 修订/校对记录引用管理员 UUID，若先删行会触发外键违约。
    if engine.url.drivername.startswith("postgresql"):
        for table in ("exam_questions", "exam_question_revisions"):
            fks = db.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = CAST(:t AS regclass) "
                    "AND confrelid = 'users'::regclass AND contype = 'f'"
                ),
                {"t": table},
            ).all()
            for (name,) in fks:
                db.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"'))
        db.commit()
    else:
        db.commit()
        conn = db.connection()
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for table in ("exam_question_revisions", "exam_questions"):
            _sqlite_rebuild_table_without_users_fk(db, table)

    db.execute(text("DELETE FROM users WHERE is_admin"))

    if engine.url.drivername.startswith("postgresql"):
        db.execute(text("ALTER TABLE users DROP COLUMN is_admin"))
        db.commit()
        print(f"[startup] admin accounts split into admin_users ({len(admins)} admins, is_admin dropped)")
        return

    conn.exec_driver_sql("ALTER TABLE users DROP COLUMN is_admin")
    violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        print(f"[startup] WARNING: foreign_key_check reported {len(violations)} violations after admin split")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    db.commit()
    print(f"[startup] admin accounts split into admin_users ({len(admins)} admins, is_admin dropped)")


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip() or None


def _session_user(authorization: str | None, db: Session, scope: str) -> User:
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    session = db.scalar(select(UserSession).where(UserSession.token == hash_session_token(token)))
    if not session or session.expires_at < datetime.utcnow() or session.scope != scope:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user = db.get(User, session.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Session user not found")
    return user


def get_app_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """学习端接口：必须持有学习端 token。users 表只剩学员（D12 拆表）。"""
    return _session_user(authorization, db, SESSION_SCOPE_APP)


def get_admin_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AdminUser:
    """后台接口：必须持有 admin 会话表中的有效 token。"""
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    session = db.scalar(
        select(AdminUserSession).where(AdminUserSession.token == hash_session_token(token))
    )
    if not session or session.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user = db.get(AdminUser, session.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Session user not found")
    return user


def get_content_user(user: User = Depends(get_app_user)) -> User:
    """取题接口（返回题目、选项或解析）：必须登录，并按用户限额，防止批量抓取题库。"""
    rate_limit.check_user_quota(user.id, "content")
    return user


def get_answer_check_user(user: User = Depends(get_app_user)) -> User:
    """判题接口（返回正确答案）：必须登录，单独按用户限额。"""
    rate_limit.check_user_quota(user.id, "check")
    return user


app.include_router(create_stats_router(get_app_user))


def touch_user_activity(db: Session, user: User) -> None:
    """登录/提交做题时更新冗余活跃时间戳（admin 列表与活跃统计使用）。"""
    user.last_active_at = datetime.utcnow()
    db.add(user)


def create_session_for_user(db: Session, user: User, scope: str) -> SessionOut:
    token = secrets.token_urlsafe(32)
    session = UserSession(
        user_id=user.id,
        scope=scope,
        token=hash_session_token(token),
        expires_at=datetime.utcnow() + timedelta(days=SESSION_DAYS),
    )
    db.add(session)
    touch_user_activity(db, user)
    db.commit()
    db.refresh(session)
    return SessionOut(token=token, expires_at=session.expires_at, user=user)


def create_session_for_admin(db: Session, user: AdminUser) -> AdminSessionOut:
    """签发管理员会话：写入 admin_user_sessions；AdminUser 无学习语义，不更新活跃时间戳。"""
    token = secrets.token_urlsafe(32)
    session = AdminUserSession(
        user_id=user.id,
        token=hash_session_token(token),
        expires_at=datetime.utcnow() + timedelta(days=SESSION_DAYS),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return AdminSessionOut(token=token, expires_at=session.expires_at, user=user)


def question_to_quiz_out(question: Question) -> QuizQuestionOut:
    options = list(question.options)
    random.shuffle(options)
    return QuizQuestionOut(
        id=question.id,
        book_id=question.book_id,
        book_name=question.book.name,
        stem=question.stem,
        question_text=question.question_text,
        options=[QuizOptionOut(id=option.id, text=option.text) for option in options],
    )


def build_quiz(
    *,
    mode: str,
    title: str,
    level: str,
    subject: str,
    subject_label: str,
    questions: list[Question],
    book_id: int | None = None,
) -> QuizOut:
    return QuizOut(
        mode=mode,
        title=title,
        level=level,
        subject=subject,
        subject_label=subject_label,
        book_id=book_id,
        questions=[question_to_quiz_out(question) for question in questions],
    )


def asset_url(path: str) -> str:
    if not path:
        return ""
    try:
        relative = Path(path).resolve().relative_to(ASSETS_DIR.resolve())
    except ValueError:
        relative = Path(path)
    return "/assets/" + quote(str(relative).replace("\\", "/"))


def exam_to_out(exam: Exam) -> ExamOut:
    return ExamOut(
        id=exam.id,
        external_id=exam.external_id,
        level=exam.level,
        title=exam.title,
        exam_date=exam.exam_date,
        media_url=asset_url(exam.media_path),
        question_count=exam.question_count,
        layer_count=exam.layer_count,
        vocabulary_num=exam.vocabulary_num,
        grammar_num=exam.grammar_num,
        reading_num=exam.reading_num,
        listening_num=exam.listening_num,
    )


def exam_question_to_out(
    question: ExamQuestion,
    *,
    include_review_fields: bool = False,
) -> ExamQuestionOut | ExamQuestionReviewOut:
    values = dict(
        id=question.id,
        external_id=question.external_id,
        layer_id=question.layer_id,
        layer_external_id=question.layer.external_id,
        order_index=question.order_index,
        question_type=question.question_type,
        data_type=question.data_type,
        title=question.title,
        passage=question.passage,
        # 听力原文答题前不下发，避免泄露答案；答题后由 /api/listening/check 返回。
        subtitle=question.subtitle if include_review_fields else "",
        segment_count=len(listening_segments(question.subtitle, "")),
        image_url=asset_url(question.image_path),
        media_url=asset_url(question.media_path),
        options=[
            ExamOptionOut(id=option.id, text=option.text, source_order=option.source_order)
            for option in sorted(question.options, key=lambda item: item.source_order)
        ],
    )
    if include_review_fields:
        return ExamQuestionReviewOut(
            **values,
            analysis=question.analysis,
            translation=question.translation,
        )
    return ExamQuestionOut(**values)


def exam_question_category(question_type: int | None) -> tuple[str, str]:
    value = question_type or 0
    if value >= 41:
        return "listening", "听力"
    if value >= 31:
        return "reading", "读解"
    if value >= 21:
        return "grammar", "文法"
    return "vocabulary", "文字词汇"


def exam_sections(level: str, questions: list[ExamQuestion]) -> list[ExamSectionOut]:
    definitions = [
        *JLPT_SECTION_TIMES[level],
        ("listening", "听力", JLPT_LISTENING_TIMES[level]),
    ]
    grouped: dict[str, list[int]] = {key: [] for key, _, _ in definitions}
    for question in questions:
        question_type = question.question_type or 0
        if question_type >= 41:
            key = "listening"
        elif level in {"N1", "N2"}:
            key = "language_reading"
        elif question_type < 21:
            key = "vocabulary"
        else:
            key = "grammar_reading"
        grouped[key].append(question.id)
    return [
        ExamSectionOut(
            key=key,
            label=label,
            duration_minutes=duration,
            question_ids=grouped[key],
        )
        for key, label, duration in definitions
    ]


def exam_run_to_out(db: Session, run: ExamRun) -> ExamRunOut:
    exam = db.get(Exam, run.exam_id)
    questions = list(
        db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.exam_id == run.exam_id)
            .order_by(ExamQuestion.order_index)
        )
    )
    answers = {
        row.question_id: row.chosen_option_id
        for row in db.scalars(select(ExamRunAnswer).where(ExamRunAnswer.run_id == run.id))
    }
    return ExamRunOut(
        id=run.id,
        exam_id=run.exam_id,
        status=run.status,
        current_section=run.current_section,
        section_started_at=run.section_started_at,
        section_expires_at=run.section_expires_at,
        sections=exam_sections(exam.level, questions),
        answers=answers,
        attempt_id=run.attempt_id,
    )


def finalize_exam_run(db: Session, run: ExamRun) -> None:
    if run.attempt_id:
        return
    exam = db.get(Exam, run.exam_id)
    questions = list(
        db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.exam_id == run.exam_id)
            .options(selectinload(ExamQuestion.options))
            .order_by(ExamQuestion.order_index)
        )
    )
    saved_answers = {
        row.question_id: row.chosen_option_id
        for row in db.scalars(select(ExamRunAnswer).where(ExamRunAnswer.run_id == run.id))
    }
    attempt = ExamAttempt(
        user_id=run.user_id,
        exam_id=exam.id,
        level=exam.level,
        title=exam.title,
        correct=0,
        total=len(questions),
        created_at=datetime.utcnow(),
    )
    db.add(attempt)
    db.flush()
    correct_count = 0
    for question in questions:
        correct_option = next(option for option in question.options if option.is_correct)
        chosen_option_id = saved_answers.get(question.id)
        is_correct = chosen_option_id == correct_option.id
        correct_count += int(is_correct)
        db.add(
            ExamAttemptAnswer(
                attempt_id=attempt.id,
                user_id=run.user_id,
                question_id=question.id,
                chosen_option_id=chosen_option_id,
                correct_option_id=correct_option.id,
                is_correct=int(is_correct),
            )
        )
    attempt.correct = correct_count
    run.status = "completed"
    run.completed_at = datetime.utcnow()
    run.section_started_at = None
    run.section_expires_at = None
    run.attempt_id = attempt.id


def advance_exam_run_section(db: Session, run: ExamRun) -> None:
    sections = exam_run_to_out(db, run).sections
    next_section = run.current_section + 1
    run.section_started_at = None
    run.section_expires_at = None
    if next_section >= len(sections):
        finalize_exam_run(db, run)
    else:
        run.current_section = next_section
        run.status = "awaiting_section"


def sync_exam_run_timeout(db: Session, run: ExamRun) -> None:
    if (
        run.status == "in_progress"
        and run.section_expires_at
        and run.section_expires_at <= datetime.utcnow()
    ):
        advance_exam_run_section(db, run)
        db.commit()
        db.refresh(run)


def exam_attempt_to_out(
    attempt: ExamAttempt,
    answers: list[ExamAttemptAnswerOut] | None = None,
) -> ExamAttemptOut:
    return ExamAttemptOut(
        id=attempt.id,
        exam_id=attempt.exam_id,
        level=attempt.level,
        title=attempt.title,
        correct=attempt.correct,
        total=attempt.total,
        created_at=attempt.created_at,
        answers=answers or [],
    )


def exam_attempt_answers_to_out(db: Session, attempt: ExamAttempt) -> list[ExamAttemptAnswerOut]:
    rows = list(
        db.scalars(
            select(ExamAttemptAnswer).where(ExamAttemptAnswer.attempt_id == attempt.id)
        )
    )
    question_ids = [row.question_id for row in rows]
    option_ids = {
        option_id
        for row in rows
        for option_id in (row.chosen_option_id, row.correct_option_id)
        if option_id
    }
    questions = {
        question.id: question
        for question in db.scalars(
            select(ExamQuestion).where(ExamQuestion.id.in_(question_ids))
        )
    }
    options = {
        option.id: option
        for option in db.scalars(select(ExamOption).where(ExamOption.id.in_(option_ids)))
    }

    answers: list[ExamAttemptAnswerOut] = []
    for row in sorted(rows, key=lambda item: questions[item.question_id].order_index):
        question = questions[row.question_id]
        chosen_option = options.get(row.chosen_option_id) if row.chosen_option_id else None
        correct_option = options[row.correct_option_id]
        answers.append(
            ExamAttemptAnswerOut(
                question_id=question.id,
                title=question.title,
                chosen_option_id=chosen_option.id if chosen_option else None,
                chosen_text=chosen_option.text if chosen_option else None,
                correct_option_id=correct_option.id,
                answer_text=correct_option.text,
                analysis=question.analysis,
                is_correct=bool(row.is_correct),
            )
        )
    return answers


def practice_attempt_to_out(
    attempt: PracticeAttempt,
    answers: list[ExamAttemptAnswerOut] | None = None,
) -> PracticeAttemptOut:
    return PracticeAttemptOut(
        id=attempt.id,
        level=attempt.level,
        category=attempt.category,
        mode=attempt.mode,
        title=attempt.title,
        correct=attempt.correct,
        total=attempt.total,
        created_at=attempt.created_at,
        answers=answers or [],
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/me", response_model=UserOut)
def me(user: User = Depends(get_app_user)) -> User:
    return user


@app.patch("/api/me", response_model=UserOut)
def update_me(
    payload: UserUpdate,
    user: User = Depends(get_app_user),
    db: Session = Depends(get_db),
) -> User:
    if payload.current_level is not None:
        current_level = payload.current_level.strip().upper()
        if current_level not in JLPT_LEVELS:
            raise HTTPException(status_code=400, detail="Invalid JLPT level")
        user.current_level = current_level

    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/api/me/level", response_model=UserOut)
def update_my_level(
    payload: UserUpdate,
    user: User = Depends(get_app_user),
    db: Session = Depends(get_db),
) -> User:
    return update_me(payload, user, db)


# 用户不存在时也做一次同样耗时的哈希校验，避免通过响应时间探测用户名是否存在。
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def _authenticate(payload: LoginIn, request: Request, db: Session) -> User:
    username = payload.username.strip()
    ip = rate_limit.client_ip(request)
    rate_limit.check_login_allowed(ip, username)
    user = db.scalar(select(User).where(User.username == username))
    password_ok = verify_password(payload.password, user.password_hash if user else _DUMMY_PASSWORD_HASH)
    if not user or not password_ok:
        rate_limit.record_login_failure(ip, username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    rate_limit.record_login_success(ip, username)
    return user


def _authenticate_admin(payload: LoginIn, request: Request, db: Session) -> AdminUser:
    """后台登录校验：查 admin_users 表，保留同耗时 dummy hash 防探测与同一套限流。"""
    username = payload.username.strip()
    ip = rate_limit.client_ip(request)
    rate_limit.check_login_allowed(ip, username)
    user = db.scalar(select(AdminUser).where(AdminUser.username == username))
    password_ok = verify_password(payload.password, user.password_hash if user else _DUMMY_PASSWORD_HASH)
    if not user or not password_ok:
        rate_limit.record_login_failure(ip, username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    rate_limit.record_login_success(ip, username)
    return user


@app.post("/api/auth/login", response_model=SessionOut)
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)) -> SessionOut:
    """学习端登录：users 表只剩学员，管理员用户名在此与不存在一致地返回 401。"""
    user = _authenticate(payload, request, db)
    return create_session_for_user(db, user, SESSION_SCOPE_APP)


@app.post("/api/admin/auth/login", response_model=AdminSessionOut)
def admin_login(payload: LoginIn, request: Request, db: Session = Depends(get_db)) -> AdminSessionOut:
    """后台登录：只允许管理员。非管理员与密码错误返回同样的错误，不暴露账号类型。"""
    user = _authenticate_admin(payload, request, db)
    return create_session_for_admin(db, user)


@app.get("/api/admin/me", response_model=AdminMeOut)
def admin_me(user: AdminUser = Depends(get_admin_user)) -> AdminUser:
    return user


@app.post("/api/auth/logout")
def logout(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """共享登出端点：双表查找删除（学习端会话 + 管理员会话）。"""
    token = _bearer_token(authorization)
    if token:
        token_hash = hash_session_token(token)
        session = db.scalar(select(UserSession).where(UserSession.token == token_hash))
        if session:
            db.delete(session)
            db.commit()
        else:
            admin_session = db.scalar(
                select(AdminUserSession).where(AdminUserSession.token == token_hash)
            )
            if admin_session:
                db.delete(admin_session)
                db.commit()
    return {"status": "ok"}


@app.get("/api/books", response_model=list[BookOut])
def books(
    level: str = Query(...),
    subject: str = Query(...),
    db: Session = Depends(get_db),
    _user: User = Depends(get_app_user),
) -> list[Book]:
    return list(
        db.scalars(
            select(Book)
            .where(Book.level == level, Book.subject == subject)
            .order_by(Book.task_order, Book.id)
        )
    )


@app.get("/api/books/{book_id}/quiz", response_model=QuizOut)
def book_quiz(
    book_id: int,
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(get_content_user),
) -> QuizOut:
    book = db.get(Book, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    questions = list(
        db.scalars(
            select(Question)
            .where(Question.book_id == book.id)
            .options(selectinload(Question.options), selectinload(Question.book))
        )
    )
    random.shuffle(questions)

    return build_quiz(
        mode="book",
        title=book.name,
        level=book.level,
        subject=book.subject,
        subject_label=book.subject_label,
        book_id=book.id,
        questions=questions[:limit],
    )


ANSWER_REVIEW_STATUSES = {"unverified", "auto_verified", "verified", "disputed", "corrected"}
ANALYSIS_REVIEW_STATUSES = {"missing", "draft", "reviewed", "disputed"}


app.include_router(create_admin_users_router(get_admin_user))
app.include_router(create_admin_stats_router(get_admin_user))


def correct_exam_option_id(question: ExamQuestion) -> int | None:
    option = next((item for item in question.options if item.is_correct), None)
    return option.id if option else None


def admin_question_snapshot(question: ExamQuestion) -> dict:
    try:
        document = json.loads(question.analysis_document_json or "{}")
    except json.JSONDecodeError:
        document = {}
    return {
        "correct_option_id": correct_exam_option_id(question),
        "answer_status": question.answer_status,
        "analysis_status": question.analysis_status,
        "subtitle": question.subtitle,
        "translation": question.translation,
        "analysis": question.analysis,
        "analysis_document": document,
        "review_note": question.review_note,
        "answer_version": question.answer_version,
    }


def admin_question_detail_out(db: Session, question: ExamQuestion) -> AdminExamQuestionDetailOut:
    base = exam_question_to_out(question, include_review_fields=True).model_dump()
    revisions = list(db.scalars(
        select(ExamQuestionRevision)
        .where(ExamQuestionRevision.question_id == question.id)
        .order_by(ExamQuestionRevision.created_at.desc(), ExamQuestionRevision.id.desc())
        .limit(20)
    ))
    try:
        document = json.loads(question.analysis_document_json or "{}")
    except json.JSONDecodeError:
        document = {}
    return AdminExamQuestionDetailOut(
        **base,
        exam_id=question.exam_id,
        exam_title=question.exam.title,
        level=question.level,
        correct_option_id=correct_exam_option_id(question),
        answer_status=question.answer_status,
        analysis_status=question.analysis_status,
        analysis_document=document,
        review_note=question.review_note,
        reviewed_by=question.reviewed_by,
        reviewed_at=question.reviewed_at,
        answer_version=question.answer_version,
        revisions=[AdminExamQuestionRevisionOut(
            id=row.id,
            user_id=row.user_id,
            before=json.loads(row.before_json),
            after=json.loads(row.after_json),
            note=row.note,
            created_at=row.created_at,
        ) for row in revisions],
    )


def exam_ids_matching_year_month(
    db: Session, exam_year: int | None, exam_month: int | None
) -> list[int] | None:
    """Exam ids whose exam_date (epoch milliseconds) falls in the given year/month.

    Filtering happens in Python so the epoch-ms → calendar conversion stays
    portable across Postgres and SQLite. None = no year/month filter requested.
    """
    if not exam_year and not exam_month:
        return None
    rows = db.execute(select(Exam.id, Exam.exam_date)).all()
    ids = []
    for exam_id, exam_date in rows:
        if not exam_date:
            continue
        date = datetime.fromtimestamp(exam_date / 1000, tz=timezone.utc)
        if exam_year and date.year != exam_year:
            continue
        if exam_month and date.month != exam_month:
            continue
        ids.append(exam_id)
    return ids


@app.get("/api/admin/exams", response_model=AdminExamListOut)
def admin_exams(
    q: str | None = Query(None, max_length=100),
    level: str | None = Query(None),
    exam_year: int | None = Query(None, ge=1990, le=2100),
    db: Session = Depends(get_db),
    _admin: AdminUser = Depends(get_admin_user),
) -> AdminExamListOut:
    """Exam papers with per-paper review progress for the 真题校正 paper list."""
    filters = [Exam.is_published == 1]
    if q and q.strip():
        filters.append(Exam.title.ilike(f"%{q.strip()}%"))
    if level:
        filters.append(Exam.level == level)
    # exam_date is an epoch-milliseconds integer (e.g. 1782835200000).
    year_month_ids = exam_ids_matching_year_month(db, exam_year, None)
    if year_month_ids is not None:
        filters.append(Exam.id.in_(year_month_ids))
    exams = list(db.scalars(
        select(Exam).where(*filters).order_by(Exam.exam_date.desc().nullslast(), Exam.title.desc())
    ))
    stats: dict[int, tuple[int, int, int, int]] = {}
    exam_ids = [exam.id for exam in exams]
    if exam_ids:
        answer_confirmed = case(
            (ExamQuestion.answer_status.in_(["auto_verified", "verified", "corrected"]), 1), else_=0
        )
        analysis_reviewed = case((ExamQuestion.analysis_status == "reviewed", 1), else_=0)
        disputed = case(
            (or_(ExamQuestion.answer_status == "disputed", ExamQuestion.analysis_status == "disputed"), 1),
            else_=0,
        )
        rows = db.execute(
            select(
                ExamQuestion.exam_id,
                func.count(ExamQuestion.id),
                func.sum(answer_confirmed),
                func.sum(analysis_reviewed),
                func.sum(disputed),
            )
            .where(ExamQuestion.exam_id.in_(exam_ids))
            .group_by(ExamQuestion.exam_id)
        ).all()
        for exam_id, total, confirmed, reviewed, disp in rows:
            stats[exam_id] = (
                int(total or 0), int(confirmed or 0), int(reviewed or 0), int(disp or 0),
            )
    items = []
    for exam in exams:
        paper_total, confirmed, reviewed, disp = stats.get(exam.id, (0, 0, 0, 0))
        items.append(AdminExamListItemOut(
            id=exam.id, title=exam.title, level=exam.level, exam_date=exam.exam_date,
            question_count=paper_total or exam.question_count,
            answer_confirmed=confirmed, analysis_reviewed=reviewed, disputed=disp,
        ))
    return AdminExamListOut(items=items, total=len(items))


@app.get("/api/admin/exam-questions", response_model=AdminExamQuestionListOut)
def admin_exam_questions(
    q: str | None = Query(None, max_length=100),
    level: str | None = Query(None),
    category: str | None = Query(None),
    answer_status: str | None = Query(None),
    analysis_status: str | None = Query(None),
    exam_year: int | None = Query(None, ge=1990, le=2100),
    exam_month: int | None = Query(None, ge=1, le=12),
    exam_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    # Papers can hold 180+ questions; revision pages load a whole paper at once.
    page_size: int = Query(30, ge=1, le=300),
    db: Session = Depends(get_db),
    _admin: AdminUser = Depends(get_admin_user),
) -> AdminExamQuestionListOut:
    filters = [Exam.is_published == 1]
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(or_(ExamQuestion.title.ilike(pattern), ExamQuestion.passage.ilike(pattern), ExamQuestion.options.any(ExamOption.text.ilike(pattern))))
    if level:
        filters.append(ExamQuestion.level == level)
    if category == "vocabulary":
        filters.append(ExamQuestion.question_type < 21)
    elif category == "grammar":
        filters.extend([ExamQuestion.question_type >= 21, ExamQuestion.question_type < 31])
    elif category == "reading":
        filters.extend([ExamQuestion.question_type >= 31, ExamQuestion.question_type < 41])
    elif category == "listening":
        filters.append(ExamQuestion.question_type >= 41)
    if answer_status:
        filters.append(ExamQuestion.answer_status == answer_status)
    if analysis_status:
        filters.append(ExamQuestion.analysis_status == analysis_status)
    if exam_id:
        filters.append(ExamQuestion.exam_id == exam_id)
    # exam_date is an epoch-milliseconds integer (e.g. 1782835200000).
    year_month_ids = exam_ids_matching_year_month(db, exam_year, exam_month)
    if year_month_ids is not None:
        filters.append(Exam.id.in_(year_month_ids))
    base_query = select(ExamQuestion).join(Exam, Exam.id == ExamQuestion.exam_id).where(*filters)
    total = db.scalar(select(func.count()).select_from(base_query.subquery())) or 0
    if exam_id:
        # Inside one paper: follow the paper's own 题组 (layer) order.
        order_by = (ExamLayer.order_index, ExamQuestion.order_index)
        base_query = base_query.join(ExamLayer, ExamLayer.id == ExamQuestion.layer_id)
    else:
        order_by = (ExamQuestion.level, Exam.exam_date.desc().nullslast(), ExamQuestion.order_index)
    rows = list(db.scalars(
        base_query.options(
            selectinload(ExamQuestion.options),
            selectinload(ExamQuestion.exam),
            selectinload(ExamQuestion.layer),
        )
        .order_by(*order_by)
        .offset((page - 1) * page_size).limit(page_size)
    ))
    items = []
    for question in rows:
        item_category, item_label = exam_question_category(question.question_type)
        items.append(AdminExamQuestionListItemOut(
            id=question.id, exam_id=question.exam_id, exam_title=question.exam.title,
            level=question.level, order_index=question.order_index,
            question_type=question.question_type, category=item_category,
            category_label=item_label, title=question.title, passage=question.passage,
            correct_option_id=correct_exam_option_id(question),
            answer_status=question.answer_status, analysis_status=question.analysis_status,
            answer_version=question.answer_version, reviewed_at=question.reviewed_at,
            layer_id=question.layer_id, layer_order=question.layer.order_index,
            layer_title=question.layer.title,
            options=[
                ExamOptionOut(id=option.id, text=option.text, source_order=option.source_order)
                for option in sorted(question.options, key=lambda row: row.source_order)
            ],
        ))
    return AdminExamQuestionListOut(items=items, total=total, page=page, page_size=page_size)


@app.get("/api/admin/exam-layers/{layer_id}/questions", response_model=AdminExamLayerQuestionsOut)
def admin_exam_layer_questions(
    layer_id: int,
    db: Session = Depends(get_db),
    _admin: AdminUser = Depends(get_admin_user),
) -> AdminExamLayerQuestionsOut:
    """All 小题 of one 题组 with full review fields — the 問題工作台 loads a whole layer at once."""
    layer = db.scalar(select(ExamLayer).where(ExamLayer.id == layer_id))
    if not layer:
        raise HTTPException(status_code=404, detail="Exam layer not found")
    rows = list(db.scalars(
        select(ExamQuestion).where(ExamQuestion.layer_id == layer_id).options(
            selectinload(ExamQuestion.options), selectinload(ExamQuestion.exam), selectinload(ExamQuestion.layer)
        ).order_by(ExamQuestion.order_index)
    ))
    return AdminExamLayerQuestionsOut(
        items=[admin_question_detail_out(db, question) for question in rows],
        total=len(rows),
        layer_image_url=asset_url(layer.image_path) if layer.image_path else None,
    )


@app.get("/api/admin/exam-questions/{question_id}", response_model=AdminExamQuestionDetailOut)
def admin_exam_question_detail(
    question_id: int,
    db: Session = Depends(get_db),
    _admin: AdminUser = Depends(get_admin_user),
) -> AdminExamQuestionDetailOut:
    question = db.scalar(select(ExamQuestion).where(ExamQuestion.id == question_id).options(
        selectinload(ExamQuestion.options), selectinload(ExamQuestion.layer), selectinload(ExamQuestion.exam)
    ))
    if not question:
        raise HTTPException(status_code=404, detail="Exam question not found")
    return admin_question_detail_out(db, question)


@app.patch("/api/admin/exam-questions/{question_id}", response_model=AdminExamQuestionDetailOut)
def update_admin_exam_question(
    question_id: int,
    payload: AdminExamQuestionUpdateIn,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_admin_user),
) -> AdminExamQuestionDetailOut:
    question = db.scalar(select(ExamQuestion).where(ExamQuestion.id == question_id).options(
        selectinload(ExamQuestion.options), selectinload(ExamQuestion.layer), selectinload(ExamQuestion.exam)
    ))
    if not question:
        raise HTTPException(status_code=404, detail="Exam question not found")
    if payload.answer_status not in ANSWER_REVIEW_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid answer review status")
    if payload.analysis_status not in ANALYSIS_REVIEW_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid analysis review status")
    if payload.correct_option_id not in {option.id for option in question.options}:
        raise HTTPException(status_code=422, detail="Correct option must belong to this question")
    before = admin_question_snapshot(question)
    for option in question.options:
        option.is_correct = int(option.id == payload.correct_option_id)
    question.answer_status = payload.answer_status
    question.analysis_status = payload.analysis_status
    question.subtitle = payload.subtitle.strip()
    question.translation = payload.translation.strip()
    question.analysis = payload.analysis.strip()
    question.analysis_document_json = json.dumps(payload.analysis_document, ensure_ascii=False)
    question.review_note = payload.review_note.strip()
    question.reviewed_by = admin.id
    question.reviewed_at = datetime.utcnow()
    question.answer_version = (question.answer_version or 0) + 1
    db.flush()
    after = admin_question_snapshot(question)
    db.add(ExamQuestionRevision(
        question_id=question.id, user_id=admin.id,
        before_json=json.dumps(before, ensure_ascii=False),
        after_json=json.dumps(after, ensure_ascii=False),
        note=question.review_note,
    ))
    db.commit()
    db.refresh(question)
    return admin_question_detail_out(db, question)


@app.get("/api/search/questions", response_model=QuestionSearchOut)
def search_questions(
    q: str = Query(..., min_length=2, max_length=100),
    level: str | None = Query(None),
    category: str | None = Query(None),
    source: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(get_content_user),
) -> QuestionSearchOut:
    query_text = q.strip()
    if len(query_text) < 2:
        raise HTTPException(status_code=422, detail="Search query must contain at least 2 characters")
    if level and level not in JLPT_LEVELS:
        raise HTTPException(status_code=422, detail="Invalid JLPT level")
    if category and category not in {"vocabulary", "grammar", "reading", "listening"}:
        raise HTTPException(status_code=422, detail="Invalid question category")
    if source and source not in {"library", "exam"}:
        raise HTTPException(status_code=422, detail="Invalid question source")

    pattern = f"%{query_text}%"
    items: list[QuestionSearchItemOut] = []

    if source in {None, "", "library"} and category not in {"reading", "listening"}:
        library_filters = [
            or_(
                Question.stem.ilike(pattern),
                Question.question_text.ilike(pattern),
                Question.options.any(Option.text.ilike(pattern)),
            )
        ]
        if level:
            library_filters.append(Question.level == level)
        if category == "grammar":
            library_filters.append(Question.subject == "grammar")
        elif category == "vocabulary":
            library_filters.append(Question.subject.in_(["meaning", "kanji"]))
        library_questions = list(
            db.scalars(
                select(Question)
                .where(*library_filters)
                .options(selectinload(Question.options), selectinload(Question.book))
                .order_by(Question.level, Question.id)
            )
        )
        for question in library_questions:
            item_category = "grammar" if question.subject == "grammar" else "vocabulary"
            item_label = "文法" if item_category == "grammar" else "文字词汇"
            items.append(QuestionSearchItemOut(
                id=question.id,
                source="library",
                source_label="基础题库",
                source_id=question.book_id,
                source_title=question.book.name,
                level=question.level,
                category=item_category,
                category_label=item_label,
                stem=question.stem or question.question_text,
                order_index=question.source_index,
                options=[
                    QuestionSearchOptionOut(id=option.id, text=option.text)
                    for option in sorted(question.options, key=lambda row: row.source_order)
                ],
            ))

    if source in {None, "", "exam"}:
        exam_filters = [
            Exam.is_published == 1,
            or_(
                ExamQuestion.title.ilike(pattern),
                ExamQuestion.passage.ilike(pattern),
                ExamQuestion.subtitle.ilike(pattern),
                ExamQuestion.options.any(ExamOption.text.ilike(pattern)),
            ),
        ]
        if level:
            exam_filters.append(ExamQuestion.level == level)
        if category == "vocabulary":
            exam_filters.append(ExamQuestion.question_type < 21)
        elif category == "grammar":
            exam_filters.extend([ExamQuestion.question_type >= 21, ExamQuestion.question_type < 31])
        elif category == "reading":
            exam_filters.extend([ExamQuestion.question_type >= 31, ExamQuestion.question_type < 41])
        elif category == "listening":
            exam_filters.append(ExamQuestion.question_type >= 41)
        exam_questions = list(
            db.scalars(
                select(ExamQuestion)
                .join(Exam, Exam.id == ExamQuestion.exam_id)
                .where(*exam_filters)
                .options(selectinload(ExamQuestion.options), selectinload(ExamQuestion.exam))
                .order_by(ExamQuestion.level, ExamQuestion.exam_id.desc(), ExamQuestion.order_index)
            )
        )
        for question in exam_questions:
            item_category, item_label = exam_question_category(question.question_type)
            stem_parts = []
            for value in (question.passage, question.title, question.subtitle):
                if value and value not in stem_parts:
                    stem_parts.append(value)
            items.append(QuestionSearchItemOut(
                id=question.id,
                source="exam",
                source_label="历年试卷",
                source_id=question.exam_id,
                source_title=question.exam.title,
                level=question.level,
                category=item_category,
                category_label=item_label,
                stem="\n".join(stem_parts),
                order_index=question.order_index,
                options=[
                    QuestionSearchOptionOut(id=option.id, text=option.text)
                    for option in sorted(question.options, key=lambda row: row.source_order)
                ],
            ))

    items.sort(key=lambda item: (item.level, item.source, item.source_title, item.order_index or 0), reverse=True)
    total = len(items)
    offset = (page - 1) * page_size
    return QuestionSearchOut(items=items[offset:offset + page_size], total=total, page=page, page_size=page_size)


@app.get("/api/exams", response_model=list[ExamOut])
def exams(
    level: str = Query(...),
    db: Session = Depends(get_db),
    _user: User = Depends(get_app_user),
) -> list[ExamOut]:
    rows = list(
        db.scalars(
            select(Exam)
            .where(Exam.level == level)
            .order_by(Exam.exam_date.desc().nullslast(), Exam.title.desc())
        )
    )
    return [exam_to_out(row) for row in rows]


@app.get("/api/exams/{exam_id}", response_model=ExamDetailOut)
def exam_detail(
    exam_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_app_user),
) -> ExamDetailOut:
    exam = db.scalar(
        select(Exam)
        .where(Exam.id == exam_id)
        .options(selectinload(Exam.layers))
    )
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    base = exam_to_out(exam).model_dump()
    base["layers"] = [
        ExamLayerOut(
            **ExamLayerOut.model_validate(layer).model_dump(exclude={"image_url"}),
            image_url=asset_url(layer.image_path),
        )
        for layer in sorted(exam.layers, key=lambda item: item.order_index)
    ]
    return ExamDetailOut(**base)


@app.get("/api/exams/{exam_id}/questions", response_model=list[ExamQuestionOut])
def exam_questions(
    exam_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_content_user),
) -> list[ExamQuestionOut]:
    exam = db.get(Exam, exam_id)
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    questions = list(
        db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.exam_id == exam_id)
            .options(selectinload(ExamQuestion.options), selectinload(ExamQuestion.layer))
            .order_by(ExamQuestion.order_index)
        )
    )
    return [exam_question_to_out(question) for question in questions]


@app.post("/api/exam-runs", response_model=ExamRunOut)
def start_exam_run(
    exam_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> ExamRunOut:
    exam = db.get(Exam, exam_id)
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    active = db.scalar(
        select(ExamRun)
        .where(
            ExamRun.user_id == user.id,
            ExamRun.exam_id == exam_id,
            ExamRun.status.in_(["in_progress", "awaiting_section"]),
        )
        .order_by(ExamRun.started_at.desc())
    )
    if active:
        sync_exam_run_timeout(db, active)
        return exam_run_to_out(db, active)

    questions = list(db.scalars(select(ExamQuestion).where(ExamQuestion.exam_id == exam_id)))
    sections = exam_sections(exam.level, questions)
    if not questions or not sections[0].question_ids:
        raise HTTPException(status_code=409, detail="Exam sections are incomplete")

    now = datetime.utcnow()
    run = ExamRun(
        user_id=user.id,
        exam_id=exam_id,
        current_section=0,
        status="in_progress",
        section_started_at=now,
        section_expires_at=now + timedelta(minutes=sections[0].duration_minutes),
        started_at=now,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        active = db.scalar(
            select(ExamRun).where(
                ExamRun.user_id == user.id,
                ExamRun.exam_id == exam_id,
                ExamRun.status.in_(["in_progress", "awaiting_section"]),
            )
        )
        if not active:
            raise
        return exam_run_to_out(db, active)
    db.refresh(run)
    return exam_run_to_out(db, run)


@app.get("/api/exam-runs", response_model=list[ExamRunOut])
def list_exam_runs(
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> list[ExamRunOut]:
    runs = list(
        db.scalars(
            select(ExamRun)
            .where(
                ExamRun.user_id == user.id,
                ExamRun.status.in_(["in_progress", "awaiting_section"]),
            )
            .order_by(ExamRun.started_at.desc())
        )
    )
    for run in runs:
        sync_exam_run_timeout(db, run)
    return [exam_run_to_out(db, run) for run in runs if run.status != "completed"]


@app.get("/api/exam-runs/{run_id}", response_model=ExamRunOut)
def get_exam_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> ExamRunOut:
    run = db.get(ExamRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Exam run not found")
    sync_exam_run_timeout(db, run)
    return exam_run_to_out(db, run)


@app.put("/api/exam-runs/{run_id}/answers/{question_id}", response_model=ExamRunOut)
def save_exam_run_answer(
    run_id: int,
    question_id: int,
    payload: ExamRunAnswerIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> ExamRunOut:
    run = db.get(ExamRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Exam run not found")
    sync_exam_run_timeout(db, run)
    if run.status != "in_progress":
        raise HTTPException(status_code=409, detail="Current section is closed")

    current_section = exam_run_to_out(db, run).sections[run.current_section]
    if question_id not in current_section.question_ids:
        raise HTTPException(status_code=409, detail="Question is not in the current section")
    option = db.get(ExamOption, payload.chosen_option_id) if payload.chosen_option_id else None
    if option and option.question_id != question_id:
        raise HTTPException(status_code=400, detail="Option does not belong to question")

    answer = db.scalar(
        select(ExamRunAnswer).where(
            ExamRunAnswer.run_id == run.id,
            ExamRunAnswer.question_id == question_id,
        )
    )
    if answer:
        answer.chosen_option_id = option.id if option else None
    else:
        db.add(
            ExamRunAnswer(
                run_id=run.id,
                question_id=question_id,
                chosen_option_id=option.id if option else None,
            )
        )
    db.commit()
    db.refresh(run)
    return exam_run_to_out(db, run)


@app.post("/api/exam-runs/{run_id}/section/complete", response_model=ExamRunOut)
def complete_exam_run_section(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> ExamRunOut:
    run = db.get(ExamRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Exam run not found")
    sync_exam_run_timeout(db, run)
    if run.status != "in_progress":
        return exam_run_to_out(db, run)
    advance_exam_run_section(db, run)
    db.commit()
    db.refresh(run)
    return exam_run_to_out(db, run)


@app.post("/api/exam-runs/{run_id}/section/start", response_model=ExamRunOut)
def start_exam_run_section(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> ExamRunOut:
    run = db.get(ExamRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Exam run not found")
    sync_exam_run_timeout(db, run)
    if run.status != "awaiting_section":
        raise HTTPException(status_code=409, detail="Exam run is not awaiting a section")

    sections = exam_run_to_out(db, run).sections
    section = sections[run.current_section]
    now = datetime.utcnow()
    run.status = "in_progress"
    run.section_started_at = now
    run.section_expires_at = now + timedelta(minutes=section.duration_minutes)
    db.commit()
    db.refresh(run)
    return exam_run_to_out(db, run)


@app.post("/api/exam-runs/{run_id}/abandon", response_model=ExamRunOut)
def abandon_exam_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> ExamRunOut:
    run = db.get(ExamRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Exam run not found")
    if run.status in {"in_progress", "awaiting_section"}:
        run.status = "abandoned"
        run.completed_at = datetime.utcnow()
        run.section_started_at = None
        run.section_expires_at = None
        db.commit()
        db.refresh(run)
    return exam_run_to_out(db, run)


@app.get(
    "/api/exam-attempts/{attempt_id}/questions",
    response_model=list[ExamQuestionReviewOut],
)
def exam_attempt_questions(
    attempt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_content_user),
) -> list[ExamQuestionReviewOut]:
    attempt = db.get(ExamAttempt, attempt_id)
    if not attempt or attempt.user_id != user.id:
        raise HTTPException(status_code=404, detail="Exam attempt not found")

    questions = list(
        db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.exam_id == attempt.exam_id)
            .options(selectinload(ExamQuestion.options), selectinload(ExamQuestion.layer))
            .order_by(ExamQuestion.order_index)
        )
    )
    return [
        exam_question_to_out(question, include_review_fields=True)
        for question in questions
    ]


@app.get("/api/exam-practice/random", response_model=list[RandomExamQuestionOut])
def random_exam_practice(
    level: str = Query(...),
    types: str = Query(..., description="Comma-separated JLPT question type codes"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(get_content_user),
) -> list[RandomExamQuestionOut]:
    if level not in JLPT_LEVELS:
        raise HTTPException(status_code=400, detail="Unknown JLPT level")
    try:
        question_types = sorted({int(value) for value in types.split(",") if value.strip()})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid question type") from exc
    if not question_types:
        raise HTTPException(status_code=400, detail="Choose at least one question type")

    candidates = list(
        db.scalars(
            select(ExamQuestion)
            .where(
                ExamQuestion.level == level,
                ExamQuestion.question_type.in_(question_types),
            )
            .options(
                selectinload(ExamQuestion.options),
                selectinload(ExamQuestion.layer),
                selectinload(ExamQuestion.exam),
            )
            .order_by(ExamQuestion.exam_id, ExamQuestion.layer_id, ExamQuestion.order_index)
        )
    )
    units: dict[tuple, list[ExamQuestion]] = {}
    for question in candidates:
        shared_material = question.question_type == 23 or 31 <= (question.question_type or 0) <= 36
        if not shared_material:
            key = (question.exam_id, question.layer_id, question.id)
        elif (question.passage or "").strip():
            # 一层可能装多篇文章（如短編読解 3 篇 × 小题）：有文章按文章拆组，
            # 保证「篇」= 一篇文章 + 其小题；候选已按层内顺序排序，文章连续。
            key = (question.exam_id, question.layer_id, f"passage:{question.passage}")
        else:
            # 无文章的共享材料（信息检索整页图）保持整层一组，材料图不拆散。
            key = (question.exam_id, question.layer_id)
        units.setdefault(key, []).append(question)
    shuffled_units = list(units.values())
    random.shuffle(shuffled_units)
    questions: list[ExamQuestion] = []
    reading_by_article = all(31 <= question_type <= 36 for question_type in question_types)
    for unit_index, unit in enumerate(shuffled_units):
        if reading_by_article and unit_index >= limit:
            break
        if questions and len(questions) >= limit:
            if not reading_by_article:
                break
        questions.extend(unit)
    if not questions:
        raise HTTPException(status_code=404, detail="No matching exam questions found")

    return [
        RandomExamQuestionOut(
            **exam_question_to_out(question).model_dump(),
            exam_id=question.exam_id,
            exam_title=question.exam.title,
            practice_order=index,
            layer_image_url=asset_url(question.layer.image_path),
        )
        for index, question in enumerate(questions, start=1)
    ]


@app.post("/api/listening/check", response_model=ListeningCheckOut)
def check_listening_answer(
    payload: ExamAttemptIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_answer_check_user),
) -> ListeningCheckOut:
    if len(payload.answers) != 1:
        raise HTTPException(status_code=400, detail="Exactly one answer is required")
    answer = payload.answers[0]
    question = db.scalar(select(ExamQuestion).where(
        ExamQuestion.id == answer.question_id,
        ExamQuestion.exam_id == payload.exam_id,
        ExamQuestion.question_type.between(41, 46),
    ).options(selectinload(ExamQuestion.options)))
    if not question:
        raise HTTPException(status_code=404, detail="Listening question not found")
    chosen = next((o for o in question.options if o.id == answer.chosen_option_id), None)
    correct = next((o for o in question.options if o.is_correct), None)
    if not chosen:
        raise HTTPException(status_code=400, detail="Choose an option belonging to this question")
    if not correct:
        raise HTTPException(status_code=409, detail="Answer unavailable")
    return ListeningCheckOut(
        question_id=question.id, title=question.title,
        chosen_option_id=chosen.id, chosen_text=chosen.text,
        correct_option_id=correct.id, answer_text=correct.text,
        analysis=question.analysis, is_correct=chosen.id == correct.id,
        segments=listening_segments(question.subtitle, question.translation),
    )


@app.post("/api/exam-practice/check", response_model=ExamAttemptAnswerOut)
def check_practice_answer(
    answer: ExamAttemptAnswerIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_answer_check_user),
) -> ExamAttemptAnswerOut:
    """Evaluate one practice answer without creating a learning record."""
    question = db.scalar(
        select(ExamQuestion)
        .where(ExamQuestion.id == answer.question_id)
        .options(selectinload(ExamQuestion.options))
    )
    if not question:
        raise HTTPException(status_code=404, detail="Practice question not found")
    chosen = next((option for option in question.options if option.id == answer.chosen_option_id), None)
    correct = next((option for option in question.options if option.is_correct), None)
    if not chosen:
        raise HTTPException(status_code=400, detail="Choose an option belonging to this question")
    if not correct:
        raise HTTPException(status_code=409, detail="Answer unavailable")
    return ExamAttemptAnswerOut(
        question_id=question.id,
        title=question.title,
        chosen_option_id=chosen.id,
        chosen_text=chosen.text,
        correct_option_id=correct.id,
        answer_text=correct.text,
        analysis=question.analysis,
        is_correct=chosen.id == correct.id,
    )


@app.post("/api/exam-attempts", response_model=ExamAttemptOut)
def create_exam_attempt(
    payload: ExamAttemptIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> ExamAttemptOut:
    exam = db.get(Exam, payload.exam_id)
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    question_ids = [answer.question_id for answer in payload.answers]
    questions = {
        question.id: question
        for question in db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.id.in_(question_ids), ExamQuestion.exam_id == exam.id)
            .options(selectinload(ExamQuestion.options))
        )
    }
    if len(questions) != len(set(question_ids)):
        raise HTTPException(status_code=400, detail="Unknown question in answers")

    created_at = datetime.utcnow()
    correct_count = 0
    evaluated: list[ExamAttemptAnswerOut] = []
    attempt = ExamAttempt(
        user_id=user.id,
        exam_id=exam.id,
        level=exam.level,
        title=exam.title,
        correct=0,
        total=len(payload.answers),
        created_at=created_at,
    )
    db.add(attempt)
    db.flush()

    for answer in payload.answers:
        question = questions[answer.question_id]
        correct_option = next(option for option in question.options if option.is_correct)
        chosen_option = (
            db.get(ExamOption, answer.chosen_option_id) if answer.chosen_option_id else None
        )
        if chosen_option and chosen_option.question_id != question.id:
            raise HTTPException(status_code=400, detail="Chosen option does not belong to question")

        is_correct = bool(chosen_option and chosen_option.id == correct_option.id)
        correct_count += 1 if is_correct else 0
        db.add(
            ExamAttemptAnswer(
                attempt_id=attempt.id,
                user_id=user.id,
                question_id=question.id,
                chosen_option_id=chosen_option.id if chosen_option else None,
                correct_option_id=correct_option.id,
                is_correct=1 if is_correct else 0,
            )
        )
        evaluated.append(
            ExamAttemptAnswerOut(
                question_id=question.id,
                title=question.title,
                chosen_option_id=chosen_option.id if chosen_option else None,
                chosen_text=chosen_option.text if chosen_option else None,
                correct_option_id=correct_option.id,
                answer_text=correct_option.text,
                analysis=question.analysis,
                is_correct=is_correct,
            )
        )

    attempt.correct = correct_count
    touch_user_activity(db, user)
    db.commit()
    db.refresh(attempt)
    return exam_attempt_to_out(attempt, evaluated)


@app.post("/api/practice-attempts", response_model=PracticeAttemptOut)
def create_practice_attempt(
    payload: PracticeAttemptIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> PracticeAttemptOut:
    if payload.level not in JLPT_LEVELS:
        raise HTTPException(status_code=400, detail="Unknown JLPT level")
    if payload.category not in {"vocabulary", "grammar", "reading", "listening"}:
        raise HTTPException(status_code=400, detail="Unknown practice category")

    question_ids = [answer.question_id for answer in payload.answers]
    questions = {
        question.id: question
        for question in db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.id.in_(question_ids), ExamQuestion.level == payload.level)
            .options(selectinload(ExamQuestion.options))
        )
    }
    if len(questions) != len(set(question_ids)):
        raise HTTPException(status_code=400, detail="Unknown question in answers")

    created_at = datetime.utcnow()
    evaluated: list[ExamAttemptAnswerOut] = []
    attempt = PracticeAttempt(
        user_id=user.id,
        level=payload.level,
        category=payload.category,
        mode=payload.mode,
        title=payload.title,
        correct=0,
        total=len(payload.answers),
        created_at=created_at,
    )
    db.add(attempt)
    db.flush()

    for answer in payload.answers:
        question = questions[answer.question_id]
        correct_option = next(option for option in question.options if option.is_correct)
        chosen_option = db.get(ExamOption, answer.chosen_option_id) if answer.chosen_option_id else None
        if chosen_option and chosen_option.question_id != question.id:
            raise HTTPException(status_code=400, detail="Chosen option does not belong to question")
        is_correct = bool(chosen_option and chosen_option.id == correct_option.id)
        attempt.correct += 1 if is_correct else 0
        db.add(PracticeAttemptAnswer(
            attempt_id=attempt.id,
            user_id=user.id,
            question_id=question.id,
            chosen_option_id=chosen_option.id if chosen_option else None,
            correct_option_id=correct_option.id,
            is_correct=1 if is_correct else 0,
        ))

        if payload.category != "listening":
            wrong = db.scalar(select(ExamWrongQuestion).where(
                ExamWrongQuestion.user_id == user.id,
                ExamWrongQuestion.question_id == question.id,
            ))
            if is_correct:
                if wrong:
                    db.delete(wrong)
            elif wrong:
                if payload.mode != "wrong_review":
                    wrong.wrong_count += 1
                wrong.last_chosen_option_id = chosen_option.id if chosen_option else None
                wrong.last_wrong_at = created_at
            else:
                db.add(ExamWrongQuestion(
                    user_id=user.id,
                    question_id=question.id,
                    category=payload.category,
                    wrong_count=1,
                    last_chosen_option_id=chosen_option.id if chosen_option else None,
                    last_wrong_at=created_at,
                ))

        evaluated.append(ExamAttemptAnswerOut(
            question_id=question.id,
            title=question.title,
            chosen_option_id=chosen_option.id if chosen_option else None,
            chosen_text=chosen_option.text if chosen_option else None,
            correct_option_id=correct_option.id,
            answer_text=correct_option.text,
            analysis=question.analysis,
            is_correct=is_correct,
        ))

    touch_user_activity(db, user)
    db.commit()
    db.refresh(attempt)
    return practice_attempt_to_out(attempt, evaluated)


@app.get("/api/practice-attempts", response_model=list[PracticeAttemptOut])
def practice_attempts(
    level: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> list[PracticeAttemptOut]:
    stmt = select(PracticeAttempt).where(PracticeAttempt.user_id == user.id)
    if level:
        stmt = stmt.where(PracticeAttempt.level == level)
    stmt = stmt.order_by(PracticeAttempt.created_at.desc()).limit(limit)
    return [practice_attempt_to_out(row) for row in db.scalars(stmt)]


@app.get("/api/practice-wrong-questions", response_model=list[ExamWrongQuestionOut])
def practice_wrong_questions(
    level: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> list[ExamWrongQuestionOut]:
    stmt = (
        select(ExamWrongQuestion, ExamQuestion, Exam, ExamOption)
        .join(ExamQuestion, ExamWrongQuestion.question_id == ExamQuestion.id)
        .join(Exam, ExamQuestion.exam_id == Exam.id)
        .outerjoin(ExamOption, ExamWrongQuestion.last_chosen_option_id == ExamOption.id)
        .where(ExamWrongQuestion.user_id == user.id)
        .options(selectinload(ExamQuestion.options))
        .order_by(ExamWrongQuestion.last_wrong_at.desc())
    )
    if level:
        stmt = stmt.where(ExamQuestion.level == level)
    labels = {"vocabulary": "文字词汇", "grammar": "文法", "reading": "读解"}
    output = []
    for wrong, question, exam, chosen in db.execute(stmt).all():
        correct = next(option for option in question.options if option.is_correct)
        output.append(ExamWrongQuestionOut(
            question_id=question.id,
            title=question.title,
            level=question.level,
            category=wrong.category,
            category_label=labels.get(wrong.category, wrong.category),
            exam_id=exam.id,
            exam_title=exam.title,
            answer_text=correct.text,
            wrong_count=wrong.wrong_count,
            last_chosen_text=chosen.text if chosen else None,
            last_wrong_at=wrong.last_wrong_at,
        ))
    return output


@app.get("/api/practice-wrong-questions/quiz", response_model=list[RandomExamQuestionOut])
def practice_wrong_questions_quiz(
    level: str = Query(...),
    category: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_content_user),
) -> list[RandomExamQuestionOut]:
    questions = list(db.scalars(
        select(ExamQuestion)
        .join(ExamWrongQuestion, ExamWrongQuestion.question_id == ExamQuestion.id)
        .where(
            ExamWrongQuestion.user_id == user.id,
            ExamWrongQuestion.category == category,
            ExamQuestion.level == level,
        )
        .options(
            selectinload(ExamQuestion.options),
            selectinload(ExamQuestion.layer),
            selectinload(ExamQuestion.exam),
        )
        .order_by(func.random())
        .limit(limit)
    ))
    if not questions:
        raise HTTPException(status_code=404, detail="No matching practice wrong questions found")
    return [RandomExamQuestionOut(
        **exam_question_to_out(question).model_dump(),
        exam_id=question.exam_id,
        exam_title=question.exam.title,
        practice_order=index,
        layer_image_url=asset_url(question.layer.image_path),
    ) for index, question in enumerate(questions, start=1)]


@app.get("/api/exam-attempts", response_model=list[ExamAttemptOut])
def exam_attempts(
    level: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> list[ExamAttemptOut]:
    stmt = (
        select(ExamAttempt)
        .where(ExamAttempt.user_id == user.id)
        .order_by(ExamAttempt.created_at.desc())
    )
    if level:
        stmt = stmt.where(ExamAttempt.level == level)
    stmt = stmt.limit(limit)
    return [exam_attempt_to_out(row) for row in db.scalars(stmt)]


@app.get("/api/exam-attempts/{attempt_id}", response_model=ExamAttemptOut)
def exam_attempt_detail(
    attempt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> ExamAttemptOut:
    attempt = db.get(ExamAttempt, attempt_id)
    if not attempt or attempt.user_id != user.id:
        raise HTTPException(status_code=404, detail="Exam attempt not found")
    return exam_attempt_to_out(attempt, exam_attempt_answers_to_out(db, attempt))


@app.get("/api/quiz/mixed", response_model=QuizOut)
def mixed_quiz(
    level: str = Query(...),
    subject: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(get_content_user),
) -> QuizOut:
    questions = list(
        db.scalars(
            select(Question)
            .where(Question.level == level, Question.subject == subject)
            .options(selectinload(Question.options), selectinload(Question.book))
        )
    )
    if not questions:
        raise HTTPException(status_code=404, detail="No questions found")

    random.shuffle(questions)
    subject_label = questions[0].book.subject_label
    return build_quiz(
        mode="mixed",
        title=f"{level} {subject_label} 综合测试",
        level=level,
        subject=subject,
        subject_label=subject_label,
        questions=questions[:limit],
    )


@app.post("/api/attempts", response_model=AttemptOut)
def create_attempt(
    payload: AttemptIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> AttemptOut:

    question_ids = [answer.question_id for answer in payload.answers]
    questions = {
        question.id: question
        for question in db.scalars(
            select(Question)
            .where(Question.id.in_(question_ids))
            .options(selectinload(Question.options), selectinload(Question.book))
        )
    }
    if len(questions) != len(set(question_ids)):
        raise HTTPException(status_code=400, detail="Unknown question in answers")

    created_at = datetime.utcnow()
    evaluated: list[AttemptAnswerOut] = []
    correct_count = 0

    attempt = Attempt(
        user_id=user.id,
        mode=payload.mode,
        level=payload.level,
        subject=payload.subject,
        subject_label=payload.subject_label,
        book_id=payload.book_id,
        title=payload.title,
        correct=0,
        total=len(payload.answers),
        created_at=created_at,
    )
    db.add(attempt)
    db.flush()

    for answer in payload.answers:
        question = questions[answer.question_id]
        correct_option = next(option for option in question.options if option.is_correct)
        chosen_option = (
            db.get(Option, answer.chosen_option_id) if answer.chosen_option_id else None
        )
        is_correct = bool(chosen_option and chosen_option.id == correct_option.id)
        correct_count += 1 if is_correct else 0

        db.add(
            AttemptAnswer(
                attempt_id=attempt.id,
                user_id=user.id,
                question_id=question.id,
                chosen_option_id=chosen_option.id if chosen_option else None,
                correct_option_id=correct_option.id,
                is_correct=1 if is_correct else 0,
            )
        )

        wrong = db.scalar(
            select(WrongQuestion).where(
                WrongQuestion.user_id == user.id,
                WrongQuestion.question_id == question.id,
            )
        )

        if payload.mode == "wrong_review":
            if is_correct and wrong:
                db.delete(wrong)
            elif not is_correct and wrong:
                wrong.last_chosen_option_id = chosen_option.id if chosen_option else None
                wrong.last_wrong_at = created_at
        elif not is_correct:
            if wrong:
                wrong.wrong_count += 1
                wrong.last_chosen_option_id = chosen_option.id if chosen_option else None
                wrong.last_wrong_at = created_at
            else:
                db.add(
                    WrongQuestion(
                        user_id=user.id,
                        question_id=question.id,
                        wrong_count=1,
                        last_chosen_option_id=chosen_option.id if chosen_option else None,
                        last_wrong_at=created_at,
                    )
                )

        evaluated.append(
            AttemptAnswerOut(
                question_id=question.id,
                stem=question.stem,
                chosen_option_id=chosen_option.id if chosen_option else None,
                chosen_text=chosen_option.text if chosen_option else None,
                correct_option_id=correct_option.id,
                answer_text=correct_option.text,
                is_correct=is_correct,
                options=[
                    QuizOptionOut(id=option.id, text=option.text)
                    for option in sorted(question.options, key=lambda item: item.source_order)
                ],
            )
        )

    attempt.correct = correct_count
    touch_user_activity(db, user)
    db.commit()
    db.refresh(attempt)
    return AttemptOut(
        id=attempt.id,
        mode=attempt.mode,
        level=attempt.level,
        subject=attempt.subject,
        subject_label=attempt.subject_label,
        book_id=attempt.book_id,
        title=attempt.title,
        correct=attempt.correct,
        total=attempt.total,
        created_at=attempt.created_at,
        answers=evaluated,
    )


@app.post("/api/check-answer", response_model=CheckAnswerOut)
def check_answer(
    payload: CheckAnswerIn,
    db: Session = Depends(get_db),
    _user: User = Depends(get_answer_check_user),
) -> CheckAnswerOut:
    question = db.scalar(
        select(Question)
        .where(Question.id == payload.question_id)
        .options(selectinload(Question.options))
    )
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    correct_option = next((option for option in question.options if option.is_correct), None)
    if correct_option is None:
        raise HTTPException(status_code=409, detail="Question has no correct option")
    # 只接受属于这道题的选项，不能拿别的题的选项 ID 来试
    chosen_option = next(
        (option for option in question.options if option.id == payload.chosen_option_id),
        None,
    )
    return CheckAnswerOut(
        question_id=question.id,
        chosen_option_id=chosen_option.id if chosen_option else None,
        correct_option_id=correct_option.id,
        answer_text=correct_option.text,
        is_correct=bool(chosen_option and chosen_option.id == correct_option.id),
    )


@app.get("/api/wrong-questions/quiz", response_model=QuizOut)
def wrong_questions_quiz(
    level: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_content_user),
) -> QuizOut:
    questions = list(
        db.scalars(
            select(Question)
            .join(WrongQuestion, WrongQuestion.question_id == Question.id)
            .where(WrongQuestion.user_id == user.id, Question.level == level)
            .options(selectinload(Question.options), selectinload(Question.book))
        )
    )
    if not questions:
        raise HTTPException(status_code=404, detail="No wrong questions found")

    random.shuffle(questions)
    return build_quiz(
        mode="wrong_review",
        title=f"{level} 错题复习",
        level=level,
        subject="wrong_review",
        subject_label="错题",
        questions=questions[:limit],
    )


@app.get("/api/attempts", response_model=list[AttemptOut])
def attempts(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> list[AttemptOut]:
    rows = list(
        db.scalars(
            select(Attempt)
            .where(Attempt.user_id == user.id)
            .order_by(Attempt.created_at.desc())
            .limit(limit)
        )
    )
    return [
        AttemptOut(
            id=row.id,
            mode=row.mode,
            level=row.level,
            subject=row.subject,
            subject_label=row.subject_label,
            book_id=row.book_id,
            title=row.title,
            correct=row.correct,
            total=row.total,
            created_at=row.created_at,
            answers=[],
        )
        for row in rows
    ]


@app.get("/api/wrong-questions", response_model=list[WrongQuestionOut])
def wrong_questions(
    level: str | None = None,
    subject: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> list[WrongQuestionOut]:
    stmt = (
        select(WrongQuestion, Question, Book, Option)
        .join(Question, WrongQuestion.question_id == Question.id)
        .join(Book, Question.book_id == Book.id)
        .outerjoin(Option, WrongQuestion.last_chosen_option_id == Option.id)
        .where(WrongQuestion.user_id == user.id)
        .order_by(WrongQuestion.last_wrong_at.desc())
    )
    if level:
        stmt = stmt.where(Question.level == level)
    if subject:
        stmt = stmt.where(Question.subject == subject)

    output = []
    for wrong, question, book, chosen in db.execute(stmt).all():
        output.append(
            WrongQuestionOut(
                question_id=question.id,
                stem=question.stem,
                level=question.level,
                subject=question.subject,
                subject_label=book.subject_label,
                book_id=book.id,
                book_name=book.name,
                answer_text=question.answer_text,
                wrong_count=wrong.wrong_count,
                last_chosen_text=chosen.text if chosen else None,
                last_wrong_at=wrong.last_wrong_at,
            )
        )
    return output


# --- 试题收藏 MVP ----------------------------------------------------------------


def _favorite_textbook_out(favorite: Favorite, question: Question, book: Book) -> FavoriteOut:
    return FavoriteOut(
        source="textbook",
        question_id=question.id,
        stem=question.stem,
        level=question.level,
        category=question.subject,
        category_label=book.subject_label,
        book_id=book.id,
        book_name=book.name,
        answer_text=question.answer_text,
        created_at=favorite.created_at,
    )


def _favorite_exam_out(favorite: Favorite, question: ExamQuestion, exam: Exam) -> FavoriteOut:
    category, category_label = exam_question_category(question.question_type)
    correct = next((option for option in question.options if option.is_correct), None)
    return FavoriteOut(
        source="exam",
        question_id=question.id,
        stem=question.title,
        level=question.level,
        category=category,
        category_label=category_label,
        exam_id=exam.id,
        exam_title=exam.title,
        answer_text=correct.text if correct else "",
        created_at=favorite.created_at,
    )


# 单题跳转端点：收藏列表「点开即练」用。声明在收藏列表端点之前不影响路由（路径无交集），
# 但 /api/favorites/ids 必须先于任何带路径参数的 /api/favorites/* 声明。


@app.get("/api/favorites/ids", response_model=FavoriteIdsOut)
def favorite_ids(
    level: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> FavoriteIdsOut:
    textbook_stmt = (
        select(Favorite.question_id)
        .join(Question, Favorite.question_id == Question.id)
        .where(Favorite.user_id == user.id, Favorite.source == "textbook")
    )
    exam_stmt = (
        select(Favorite.question_id)
        .join(ExamQuestion, Favorite.question_id == ExamQuestion.id)
        .where(Favorite.user_id == user.id, Favorite.source == "exam")
    )
    if level:
        textbook_stmt = textbook_stmt.where(Question.level == level)
        exam_stmt = exam_stmt.where(ExamQuestion.level == level)
    return FavoriteIdsOut(
        textbook=list(db.scalars(textbook_stmt)),
        exam=list(db.scalars(exam_stmt)),
    )


@app.get("/api/favorites", response_model=list[FavoriteOut])
def list_favorites(
    level: str | None = None,
    source: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> list[FavoriteOut]:
    output: list[FavoriteOut] = []
    if source in (None, "textbook"):
        stmt = (
            select(Favorite, Question, Book)
            .join(Question, Favorite.question_id == Question.id)
            .join(Book, Question.book_id == Book.id)
            .where(Favorite.user_id == user.id, Favorite.source == "textbook")
        )
        if level:
            stmt = stmt.where(Question.level == level)
        output.extend(
            _favorite_textbook_out(favorite, question, book)
            for favorite, question, book in db.execute(stmt).all()
        )
    if source in (None, "exam"):
        stmt = (
            select(Favorite, ExamQuestion, Exam)
            .join(ExamQuestion, Favorite.question_id == ExamQuestion.id)
            .join(Exam, ExamQuestion.exam_id == Exam.id)
            .where(Favorite.user_id == user.id, Favorite.source == "exam")
            .options(selectinload(ExamQuestion.options))
        )
        if level:
            stmt = stmt.where(ExamQuestion.level == level)
        output.extend(
            _favorite_exam_out(favorite, question, exam)
            for favorite, question, exam in db.execute(stmt).all()
        )
    output.sort(key=lambda item: item.created_at, reverse=True)
    return output


@app.put("/api/favorites", response_model=FavoriteOut)
def add_favorite(
    payload: FavoriteToggleIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> FavoriteOut:
    if payload.source == "textbook":
        question = db.scalar(
            select(Question)
            .where(Question.id == payload.question_id)
            .options(selectinload(Question.options), selectinload(Question.book))
        )
    else:
        question = db.scalar(
            select(ExamQuestion)
            .where(ExamQuestion.id == payload.question_id)
            .options(selectinload(ExamQuestion.options), selectinload(ExamQuestion.exam))
        )
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    favorite = db.scalar(
        select(Favorite).where(
            Favorite.user_id == user.id,
            Favorite.source == payload.source,
            Favorite.question_id == payload.question_id,
        )
    )
    if favorite is None:
        favorite = Favorite(
            user_id=user.id, source=payload.source, question_id=payload.question_id
        )
        db.add(favorite)
        db.commit()
        db.refresh(favorite)

    if payload.source == "textbook":
        return _favorite_textbook_out(favorite, question, question.book)
    return _favorite_exam_out(favorite, question, question.exam)


@app.delete("/api/favorites/{source}/{question_id}", status_code=204)
def remove_favorite(
    source: Literal["textbook", "exam"],
    question_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_app_user),
) -> None:
    favorite = db.scalar(
        select(Favorite).where(
            Favorite.user_id == user.id,
            Favorite.source == source,
            Favorite.question_id == question_id,
        )
    )
    if not favorite:
        raise HTTPException(status_code=404, detail="Favorite not found")
    db.delete(favorite)
    db.commit()


@app.get("/api/questions/{question_id}/quiz", response_model=QuizOut)
def textbook_question_quiz(
    question_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_content_user),
) -> QuizOut:
    """教材单题卷：收藏列表跳转「点开即练」用。"""
    question = db.scalar(
        select(Question)
        .where(Question.id == question_id)
        .options(selectinload(Question.options), selectinload(Question.book))
    )
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    return build_quiz(
        mode="favorite_review",
        title=f"{question.level} 收藏题练习",
        level=question.level,
        subject=question.subject,
        subject_label=question.book.subject_label,
        questions=[question],
        book_id=question.book_id,
    )


@app.get("/api/exam-practice/questions/{question_id}", response_model=RandomExamQuestionOut)
def exam_practice_question(
    question_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_content_user),
) -> RandomExamQuestionOut:
    """真题单题：收藏列表跳转「点开即练」用。"""
    question = db.scalar(
        select(ExamQuestion)
        .where(ExamQuestion.id == question_id)
        .options(
            selectinload(ExamQuestion.options),
            selectinload(ExamQuestion.layer),
            selectinload(ExamQuestion.exam),
        )
    )
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    return RandomExamQuestionOut(
        **exam_question_to_out(question).model_dump(),
        exam_id=question.exam_id,
        exam_title=question.exam.title,
        practice_order=1,
        layer_image_url=asset_url(question.layer.image_path),
    )
