from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    current_level: Mapped[str] = mapped_column(String(16), default="N5", nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 冗余活跃时间戳：登录/提交做题时更新，供后台列表排序与活跃统计。
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    # 签发端："app" = 学习端（C 端用户），"admin" = 后台（管理员）。两端 token 互不通用。
    scope: Mapped[str] = mapped_column(String(16), default="app", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    subject_label: Mapped[str] = mapped_column(String(32), nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, default=0)
    task_order: Mapped[int | None] = mapped_column(Integer)

    questions: Mapped[list["Question"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, default="")
    source_index: Mapped[int | None] = mapped_column(Integer)

    book: Mapped[Book] = relationship(back_populates="questions")
    options: Mapped[list["Option"]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )


class Option(Base):
    __tablename__ = "options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id"), index=True, nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_correct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)

    question: Mapped[Question] = relationship(back_populates="options")


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    subject_label: Mapped[str] = mapped_column(String(32), nullable=False)
    book_id: Mapped[int | None] = mapped_column(ForeignKey("books.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    correct: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    answers: Mapped[list["AttemptAnswer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class AttemptAnswer(Base):
    __tablename__ = "attempt_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempts.id"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id"), index=True, nullable=False
    )
    chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("options.id"))
    correct_option_id: Mapped[int] = mapped_column(ForeignKey("options.id"), nullable=False)
    is_correct: Mapped[int] = mapped_column(Integer, nullable=False)

    attempt: Mapped[Attempt] = relationship(back_populates="answers")


class WrongQuestion(Base):
    __tablename__ = "wrong_questions"
    __table_args__ = (UniqueConstraint("user_id", "question_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id"), index=True, nullable=False
    )
    wrong_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("options.id"))
    last_wrong_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Exam(Base):
    __tablename__ = "exams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    exam_date: Mapped[int | None] = mapped_column(BigInteger)
    exam_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    is_published: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    media_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    media_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    vocabulary_num: Mapped[int | None] = mapped_column(Integer)
    grammar_num: Mapped[int | None] = mapped_column(Integer)
    reading_num: Mapped[int | None] = mapped_column(Integer)
    listening_num: Mapped[int | None] = mapped_column(Integer)
    duration_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    layer_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    layers: Mapped[list["ExamLayer"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )
    questions: Mapped[list["ExamQuestion"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )


class ExamLayer(Base):
    __tablename__ = "exam_layers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    question_type: Mapped[int | None] = mapped_column(Integer)
    data_type: Mapped[int | None] = mapped_column(Integer)
    questions_num: Mapped[int | None] = mapped_column(Integer)
    question_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    image_path: Mapped[str] = mapped_column(Text, default="", nullable=False)

    exam: Mapped[Exam] = relationship(back_populates="layers")
    questions: Mapped[list["ExamQuestion"]] = relationship(
        back_populates="layer", cascade="all, delete-orphan"
    )


class ExamQuestion(Base):
    __tablename__ = "exam_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True, nullable=False)
    layer_id: Mapped[int] = mapped_column(ForeignKey("exam_layers.id"), index=True, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    question_type: Mapped[int | None] = mapped_column(Integer)
    data_type: Mapped[int | None] = mapped_column(Integer)
    identity_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    passage: Mapped[str] = mapped_column(Text, default="", nullable=False)
    analysis: Mapped[str] = mapped_column(Text, default="", nullable=False)
    translation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    subtitle: Mapped[str] = mapped_column(Text, default="", nullable=False)
    parent_external_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    image_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    media_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    media_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at_text: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_at_text: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    answer_status: Mapped[str] = mapped_column(String(32), default="unverified", index=True, nullable=False)
    analysis_status: Mapped[str] = mapped_column(String(32), default="missing", index=True, nullable=False)
    analysis_document_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    review_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    answer_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    exam: Mapped[Exam] = relationship(back_populates="questions")
    layer: Mapped[ExamLayer] = relationship(back_populates="questions")
    options: Mapped[list["ExamOption"]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )


class ExamOption(Base):
    __tablename__ = "exam_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("exam_questions.id"), index=True, nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_correct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)

    question: Mapped[ExamQuestion] = relationship(back_populates="options")


class ExamQuestionRevision(Base):
    __tablename__ = "exam_question_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("exam_questions.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_json: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ExamAttempt(Base):
    __tablename__ = "exam_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    correct: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    answers: Mapped[list["ExamAttemptAnswer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class ExamRun(Base):
    __tablename__ = "exam_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True, nullable=False)
    current_section: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ready", index=True, nullable=False)
    section_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    section_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("exam_attempts.id"))


class ExamRunAnswer(Base):
    __tablename__ = "exam_run_answers"
    __table_args__ = (UniqueConstraint("run_id", "question_id", name="uq_exam_run_question"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("exam_runs.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(ForeignKey("exam_questions.id"), index=True, nullable=False)
    chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("exam_options.id"))


class ExamAttemptAnswer(Base):
    __tablename__ = "exam_attempt_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("exam_attempts.id"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("exam_questions.id"), index=True, nullable=False
    )
    chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("exam_options.id"))
    correct_option_id: Mapped[int] = mapped_column(ForeignKey("exam_options.id"), nullable=False)
    is_correct: Mapped[int] = mapped_column(Integer, nullable=False)

    attempt: Mapped[ExamAttempt] = relationship(back_populates="answers")




class PracticeAttempt(Base):
    __tablename__ = "practice_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), default="practice", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    correct: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    answers: Mapped[list["PracticeAttemptAnswer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class PracticeAttemptAnswer(Base):
    __tablename__ = "practice_attempt_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("practice_attempts.id"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("exam_questions.id"), index=True, nullable=False
    )
    chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("exam_options.id"))
    correct_option_id: Mapped[int] = mapped_column(ForeignKey("exam_options.id"), nullable=False)
    is_correct: Mapped[int] = mapped_column(Integer, nullable=False)

    attempt: Mapped[PracticeAttempt] = relationship(back_populates="answers")


class ExamWrongQuestion(Base):
    __tablename__ = "exam_wrong_questions"
    __table_args__ = (UniqueConstraint("user_id", "question_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("exam_questions.id"), index=True, nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    wrong_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("exam_options.id"))
    last_wrong_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
