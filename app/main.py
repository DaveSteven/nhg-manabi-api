import os
import random
import json
import hashlib
import hmac
import secrets
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .database import Base, engine, get_db
from .models import (
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
    Option,
    PracticeAttempt,
    PracticeAttemptAnswer,
    Question,
    User,
    UserSession,
    UserVocabularyProgress,
    VocabularyCollocation,
    VocabularyEntry,
    VocabularyExample,
    VocabularyRelation,
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
    CheckAnswerIn,
    CheckAnswerOut,
    AttemptIn,
    AttemptOut,
    BookOut,
    ExamAttemptIn,
    ExamAttemptOut,
    ExamAttemptAnswerOut,
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
    UserCreate,
    UserOut,
    UserUpdate,
    VocabularyEntryOut,
    VocabularyExampleOut,
    VocabularyRelationOut,
    VocabularyReviewIn,
    VocabularyStatsOut,
    WrongQuestionOut,
)


DEFAULT_USER_ID = 1
DEFAULT_DAVID_PASSWORD = os.getenv("DEFAULT_DAVID_PASSWORD", "214423")
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
        Path(__file__).resolve().parents[2] / "mojitest_spider" / "data" / "assets",
    )
)

app = FastAPI(title="Japanese Study API")

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
        ensure_schema(db)
        normalize_exam_layer_images(db)
        ensure_default_user(db)
        sync_postgres_sequences(db)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    ).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, salt, digest = password_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    expected = hash_password(password, salt).split("$", 2)[2]
    return hmac.compare_digest(expected, digest)


def ensure_schema(db: Session) -> None:
    if engine.url.drivername.startswith("postgresql"):
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS current_level VARCHAR(16) NOT NULL DEFAULT 'N5'"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS passage TEXT NOT NULL DEFAULT ''"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS answer_status VARCHAR(32) NOT NULL DEFAULT 'unverified'"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS analysis_status VARCHAR(32) NOT NULL DEFAULT 'missing'"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS analysis_document_json TEXT NOT NULL DEFAULT '{}'"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS review_note TEXT NOT NULL DEFAULT ''"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS reviewed_by INTEGER REFERENCES users(id)"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP"))
        db.execute(text("ALTER TABLE exam_questions ADD COLUMN IF NOT EXISTS answer_version INTEGER NOT NULL DEFAULT 1"))
        db.execute(text("CREATE INDEX IF NOT EXISTS ix_exam_questions_answer_status ON exam_questions(answer_status)"))
        db.execute(text("CREATE INDEX IF NOT EXISTS ix_exam_questions_analysis_status ON exam_questions(analysis_status)"))
        db.execute(text("ALTER TABLE exam_layers ADD COLUMN IF NOT EXISTS image_path TEXT NOT NULL DEFAULT ''"))
        db.execute(text("ALTER TABLE exam_attempt_answers ALTER COLUMN chosen_option_id DROP NOT NULL"))
        db.execute(text("ALTER TABLE vocabulary_entries ADD COLUMN IF NOT EXISTS transitivity VARCHAR(24)"))
        db.execute(text("ALTER TABLE vocabulary_relations ADD COLUMN IF NOT EXISTS term_reading_katakana VARCHAR(256) NOT NULL DEFAULT ''"))
        db.execute(text("ALTER TABLE vocabulary_examples ADD COLUMN IF NOT EXISTS reading_katakana TEXT NOT NULL DEFAULT ''"))
        db.execute(text("ALTER TABLE vocabulary_examples ADD COLUMN IF NOT EXISTS reading_hiragana TEXT NOT NULL DEFAULT ''"))
        db.execute(text("ALTER TABLE vocabulary_examples ADD COLUMN IF NOT EXISTS phonetic_guide_json TEXT NOT NULL DEFAULT '[]'"))
        db.execute(text("ALTER TABLE vocabulary_examples ADD COLUMN IF NOT EXISTS source VARCHAR(64) NOT NULL DEFAULT ''"))
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
    if "is_admin" not in columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"
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
        "reviewed_by": "INTEGER REFERENCES users(id)",
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

    ensure_nullable_exam_chosen_option(db)

    vocabulary_columns = {
        row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(vocabulary_relations)").all()
    }
    if vocabulary_columns and "term_reading_katakana" not in vocabulary_columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE vocabulary_relations ADD COLUMN term_reading_katakana VARCHAR(256) NOT NULL DEFAULT ''"
        )
        db.commit()

    example_columns = {
        row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(vocabulary_examples)").all()
    }
    if example_columns and "reading_katakana" not in example_columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE vocabulary_examples ADD COLUMN reading_katakana TEXT NOT NULL DEFAULT ''"
        )
        db.commit()
    if example_columns and "reading_hiragana" not in example_columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE vocabulary_examples ADD COLUMN reading_hiragana TEXT NOT NULL DEFAULT ''"
        )
        db.commit()
    if example_columns and "phonetic_guide_json" not in example_columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE vocabulary_examples ADD COLUMN phonetic_guide_json TEXT NOT NULL DEFAULT '[]'"
        )
        db.commit()

    entry_columns = {
        row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(vocabulary_entries)").all()
    }
    if entry_columns and "transitivity" not in entry_columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE vocabulary_entries ADD COLUMN transitivity VARCHAR(24)"
        )
        db.commit()
    if example_columns and "source" not in example_columns:
        db.connection().exec_driver_sql(
            "ALTER TABLE vocabulary_examples ADD COLUMN source VARCHAR(64) NOT NULL DEFAULT ''"
        )
        db.commit()


def normalize_exam_layer_images(db: Session) -> None:
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

    recipe_layer = next((layer for layer in layers if layer.external_id == "sHDAKnyXHO"), None)
    if recipe_layer:
        recipe_layer.image_path = "images/924f57ea-ceea-421f-bd82-934d848521a2.png"
        for question in recipe_layer.questions:
            if question.external_id == "d7GXT2AEDj":
                question.image_id = ""
                question.image_path = ""
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
            user_id INTEGER NOT NULL,
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


def ensure_default_user(db: Session) -> User:
    user = db.get(User, DEFAULT_USER_ID)
    if user:
        if not user.password_hash:
            user.password_hash = hash_password(DEFAULT_DAVID_PASSWORD)
            db.commit()
            db.refresh(user)
        if not user.current_level:
            user.current_level = "N5"
            db.commit()
            db.refresh(user)
        return user

    user = User(
        id=DEFAULT_USER_ID,
        username="david",
        display_name="David",
        password_hash=hash_password(DEFAULT_DAVID_PASSWORD),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def sync_postgres_sequences(db: Session) -> None:
    if not engine.url.drivername.startswith("postgresql"):
        return

    db.execute(
        text(
            """
            SELECT setval(
                pg_get_serial_sequence('users', 'id'),
                GREATEST((SELECT COALESCE(MAX(id), 1) FROM users), 1),
                (SELECT COUNT(*) > 0 FROM users)
            )
            """
        )
    )
    db.commit()


def get_current_user(
    authorization: str | None = Header(default=None),
    x_user_id: int | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        session = db.scalar(select(UserSession).where(UserSession.token == token))
        if not session or session.expires_at < datetime.utcnow():
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        user = db.get(User, session.user_id)
        if not user:
            raise HTTPException(status_code=401, detail="Session user not found")
        return user

    if x_user_id is None:
        return ensure_default_user(db)

    user = db.get(User, x_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def get_authenticated_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")

    token = authorization.split(" ", 1)[1].strip()
    session = db.scalar(select(UserSession).where(UserSession.token == token))
    if not session or session.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user = db.get(User, session.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Session user not found")
    return user


def create_session_for_user(db: Session, user: User) -> SessionOut:
    session = UserSession(
        user_id=user.id,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.utcnow() + timedelta(days=SESSION_DAYS),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return SessionOut(token=session.token, expires_at=session.expires_at, user=user)


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
        subtitle=question.subtitle,
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
def me(user: User = Depends(get_current_user)) -> User:
    return user


@app.patch("/api/me", response_model=UserOut)
def update_me(
    payload: UserUpdate,
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    return update_me(payload, user, db)


def create_user(payload: UserCreate, db: Session) -> User:
    username = payload.username.strip()
    display_name = payload.display_name.strip()
    password = payload.password.strip()
    if not username or not display_name or not password:
        raise HTTPException(status_code=400, detail="Username, display name, and password are required")

    existing = db.scalar(select(User).where(User.username == username))
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    user = User(username=username, display_name=display_name, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/api/auth/register", response_model=UserOut)
def register(
    payload: UserCreate,
    admin: User = Depends(get_authenticated_user),
    db: Session = Depends(get_db),
) -> User:
    if not admin.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return create_user(payload, db)


@app.post("/api/auth/login", response_model=SessionOut)
def login(payload: LoginIn, db: Session = Depends(get_db)) -> SessionOut:
    ensure_default_user(db)
    user = db.scalar(select(User).where(User.username == payload.username.strip()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return create_session_for_user(db, user)


@app.post("/api/auth/logout")
def logout(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        session = db.scalar(select(UserSession).where(UserSession.token == token))
        if session:
            db.delete(session)
            db.commit()
    return {"status": "ok"}


def vocabulary_out(
    entry: VocabularyEntry,
    progress: UserVocabularyProgress | None,
) -> VocabularyEntryOut:
    return VocabularyEntryOut(
        id=entry.id,
        lesson=entry.lesson,
        frequency_tier=entry.frequency_tier,
        word=entry.word,
        reading_hiragana=entry.reading_hiragana,
        reading_katakana=entry.reading_katakana,
        part_of_speech=entry.part_of_speech,
        transitivity=entry.transitivity,
        meaning_zh=entry.meaning_zh,
        source_page=entry.source_page,
        relations=[
            VocabularyRelationOut(
                type=item.relation_type,
                term=item.term,
                term_reading_katakana=item.term_reading_katakana,
            )
            for item in entry.relations
        ],
        collocations=[item.text for item in entry.collocations],
        examples=[
            VocabularyExampleOut(
                sentence=item.sentence,
                reading_hiragana=item.reading_hiragana,
                phonetic_guide=json.loads(item.phonetic_guide_json or "[]"),
                source=item.source,
            )
            for item in entry.examples
        ],
        study_status=progress.status if progress else "new",
        correct_count=progress.correct_count if progress else 0,
        review_count=progress.review_count if progress else 0,
    )


@app.get("/api/vocabulary", response_model=list[VocabularyEntryOut])
def vocabulary(
    lesson: int | None = Query(default=None, ge=1, le=16),
    part_of_speech: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VocabularyEntryOut]:
    statement = (
        select(VocabularyEntry)
        .options(
            selectinload(VocabularyEntry.relations),
            selectinload(VocabularyEntry.collocations),
            selectinload(VocabularyEntry.examples),
        )
        .order_by(VocabularyEntry.lesson, VocabularyEntry.id)
    )
    if lesson is not None:
        statement = statement.where(VocabularyEntry.lesson == lesson)
    if part_of_speech in {"noun", "verb", "adverb"}:
        statement = statement.where(VocabularyEntry.part_of_speech == part_of_speech)
    if q:
        query = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                VocabularyEntry.word.ilike(query),
                VocabularyEntry.reading_hiragana.ilike(query),
                VocabularyEntry.reading_katakana.ilike(query),
                VocabularyEntry.meaning_zh.ilike(query),
                VocabularyEntry.collocations.any(VocabularyCollocation.text.ilike(query)),
                VocabularyEntry.examples.any(VocabularyExample.sentence.ilike(query)),
                VocabularyEntry.relations.any(VocabularyRelation.term.ilike(query)),
            )
        )

    entries = list(db.scalars(statement.offset(offset).limit(limit)))
    if not entries:
        return []
    progress_by_entry = {
        row.entry_id: row
        for row in db.scalars(
            select(UserVocabularyProgress).where(
                UserVocabularyProgress.user_id == user.id,
                UserVocabularyProgress.entry_id.in_([entry.id for entry in entries]),
            )
        )
    }
    output = [vocabulary_out(entry, progress_by_entry.get(entry.id)) for entry in entries]
    if status in {"new", "learning", "mastered"}:
        output = [item for item in output if item.study_status == status]
    return output


@app.get("/api/vocabulary/stats", response_model=VocabularyStatsOut)
def vocabulary_stats(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VocabularyStatsOut:
    total = db.scalar(select(func.count(VocabularyEntry.id))) or 0
    rows = db.execute(
        select(UserVocabularyProgress.status, func.count(UserVocabularyProgress.id))
        .where(UserVocabularyProgress.user_id == user.id)
        .group_by(UserVocabularyProgress.status)
    ).all()
    counts = dict(rows)
    learning = counts.get("learning", 0)
    mastered = counts.get("mastered", 0)
    return VocabularyStatsOut(
        total=total,
        new=max(0, total - learning - mastered),
        learning=learning,
        mastered=mastered,
    )


@app.post("/api/vocabulary/{entry_id}/review", response_model=VocabularyEntryOut)
def review_vocabulary(
    entry_id: int,
    payload: VocabularyReviewIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VocabularyEntryOut:
    entry = db.scalar(
        select(VocabularyEntry)
        .where(VocabularyEntry.id == entry_id)
        .options(
            selectinload(VocabularyEntry.relations),
            selectinload(VocabularyEntry.collocations),
            selectinload(VocabularyEntry.examples),
        )
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Vocabulary entry not found")

    progress = db.scalar(
        select(UserVocabularyProgress).where(
            UserVocabularyProgress.user_id == user.id,
            UserVocabularyProgress.entry_id == entry_id,
        )
    )
    if progress is None:
        progress = UserVocabularyProgress(
            user_id=user.id,
            entry_id=entry_id,
            status="learning",
            correct_count=0,
            review_count=0,
            last_reviewed_at=datetime.utcnow(),
        )
    progress.review_count += 1
    if payload.remembered:
        progress.correct_count += 1
    accuracy = progress.correct_count / progress.review_count
    progress.status = "mastered" if progress.correct_count >= 3 and accuracy >= 0.75 else "learning"
    progress.last_reviewed_at = datetime.utcnow()
    db.add(progress)
    db.commit()
    db.refresh(progress)
    return vocabulary_out(entry, progress)


@app.get("/api/books", response_model=list[BookOut])
def books(
    level: str = Query(...),
    subject: str = Query(...),
    db: Session = Depends(get_db),
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


def get_admin_user(user: User = Depends(get_authenticated_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


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
    _admin: User = Depends(get_admin_user),
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
    _admin: User = Depends(get_admin_user),
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
    _admin: User = Depends(get_admin_user),
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
    _admin: User = Depends(get_admin_user),
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
    admin: User = Depends(get_admin_user),
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
    _user: User = Depends(get_authenticated_user),
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
def exam_detail(exam_id: int, db: Session = Depends(get_db)) -> ExamDetailOut:
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
def exam_questions(exam_id: int, db: Session = Depends(get_db)) -> list[ExamQuestionOut]:
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
    user: User = Depends(get_authenticated_user),
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
    user: User = Depends(get_authenticated_user),
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
    user: User = Depends(get_authenticated_user),
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
    user: User = Depends(get_authenticated_user),
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
    user: User = Depends(get_authenticated_user),
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
    user: User = Depends(get_authenticated_user),
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
    user: User = Depends(get_authenticated_user),
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
    user: User = Depends(get_authenticated_user),
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
) -> list[RandomExamQuestionOut]:
    if level not in JLPT_LEVELS:
        raise HTTPException(status_code=400, detail="Unknown JLPT level")
    try:
        question_types = sorted({int(value) for value in types.split(",") if value.strip()})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid question type") from exc
    if not question_types:
        raise HTTPException(status_code=400, detail="Choose at least one question type")

    questions = list(
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
            .order_by(func.random())
            .limit(limit)
        )
    )
    if not questions:
        raise HTTPException(status_code=404, detail="No matching exam questions found")

    return [
        RandomExamQuestionOut(
            **exam_question_to_out(question).model_dump(),
            exam_id=question.exam_id,
            exam_title=question.exam.title,
            practice_order=index,
        )
        for index, question in enumerate(questions, start=1)
    ]


@app.post("/api/listening/check", response_model=ExamAttemptAnswerOut)
def check_listening_answer(
    payload: ExamAttemptIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExamAttemptAnswerOut:
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
    return ExamAttemptAnswerOut(
        question_id=question.id, title=question.title,
        chosen_option_id=chosen.id, chosen_text=chosen.text,
        correct_option_id=correct.id, answer_text=correct.text,
        analysis=question.analysis, is_correct=chosen.id == correct.id,
    )


@app.post("/api/exam-attempts", response_model=ExamAttemptOut)
def create_exam_attempt(
    payload: ExamAttemptIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
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
    db.commit()
    db.refresh(attempt)
    return exam_attempt_to_out(attempt, evaluated)


@app.post("/api/practice-attempts", response_model=PracticeAttemptOut)
def create_practice_attempt(
    payload: PracticeAttemptIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PracticeAttemptOut:
    if payload.level not in JLPT_LEVELS:
        raise HTTPException(status_code=400, detail="Unknown JLPT level")
    if payload.category not in {"vocabulary", "grammar", "reading"}:
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

    db.commit()
    db.refresh(attempt)
    return practice_attempt_to_out(attempt, evaluated)


@app.get("/api/practice-attempts", response_model=list[PracticeAttemptOut])
def practice_attempts(
    level: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
    ) for index, question in enumerate(questions, start=1)]


@app.get("/api/exam-attempts", response_model=list[ExamAttemptOut])
def exam_attempts(
    level: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
def check_answer(payload: CheckAnswerIn, db: Session = Depends(get_db)) -> CheckAnswerOut:
    question = db.scalar(
        select(Question)
        .where(Question.id == payload.question_id)
        .options(selectinload(Question.options))
    )
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    correct_option = next(option for option in question.options if option.is_correct)
    chosen_option = (
        db.get(Option, payload.chosen_option_id) if payload.chosen_option_id else None
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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
