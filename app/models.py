from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
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

    # 固定规范的 UUID 主键：对外不暴露自增序号，各引用列均为 String(36)。
    # 拆表后（D12）此表只剩学员；管理员在 admin_users 表。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    current_level: Mapped[str] = mapped_column(String(16), default="N5", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 冗余活跃时间戳：登录/提交做题时更新，供后台列表排序与活跃统计。
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # T-028 自助注册：全表唯一，仅用于验证/找回；B4 代建账号 email 为空。
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    # 激活时间：NULL = 未验证（不可登录）；模型层 default 让 admin 代建的新账号自动激活。
    # 存量行由启动迁移一次性回填（见 ensure_user_email_columns），之后不再全量回填。
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)
    # 停用时间（T-031）：NULL = 正常；停用 = 保留数据 + 禁止登录 + 删除现有会话，与删除互补。
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthEmailToken(Base):
    """邮箱验证 / 密码重置令牌（T-028）。token_hash 存 SHA-256，原文只在邮件链接中；
    一次性 = 校验通过后删除行（用后即焚）。验证与重置共用一张表，purpose 区分。"""

    __tablename__ = "auth_email_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)  # 'verify' | 'reset'
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    # 签发端："app" = 学习端（C 端用户）。管理员会话在 admin_user_sessions 表，互不通用。
    scope: Mapped[str] = mapped_column(String(16), default="app", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdminUser(Base):
    """后台管理员账号（D12）：与学员表结构隔离，无学习语义字段（current_level 等）。"""

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdminUserSession(Base):
    """管理员会话：镜像 UserSession，token 存 SHA-256；无 scope 列（整表即 admin 语义）。"""

    __tablename__ = "admin_user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("admin_users.id"), index=True, nullable=False)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
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
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    subject_label: Mapped[str] = mapped_column(String(32), nullable=False)
    book_id: Mapped[int | None] = mapped_column(ForeignKey("books.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    correct: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # attempt 级时长（T-014）：秒；存量行 NULL = 未采集
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    answers: Mapped[list["AttemptAnswer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class AttemptAnswer(Base):
    __tablename__ = "attempt_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempts.id"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id"), index=True, nullable=False
    )
    chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("options.id"))
    correct_option_id: Mapped[int] = mapped_column(ForeignKey("options.id"), nullable=False)
    is_correct: Mapped[int] = mapped_column(Integer, nullable=False)
    # 逐条作答时间（T-011 迁移新增）：存量行 NULL = 未采集，只从上线时刻起算
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)

    attempt: Mapped[Attempt] = relationship(back_populates="answers")


class WrongQuestion(Base):
    __tablename__ = "wrong_questions"
    __table_args__ = (UniqueConstraint("user_id", "question_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
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
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
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
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
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
    parent_external_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
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
    # 校对者 UUID（AdminUser）；无数据库级外键——历史遗留列已去 FK，不做回填。
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
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
    # 修订者 UUID（AdminUser）；无数据库级外键——历史遗留列已去 FK，不做回填。
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_json: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ExamAttempt(Base):
    __tablename__ = "exam_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    correct: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # attempt 级时长（T-014）：秒；存量行 NULL = 未采集
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    answers: Mapped[list["ExamAttemptAnswer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class ExamRun(Base):
    __tablename__ = "exam_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
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
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("exam_questions.id"), index=True, nullable=False
    )
    chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("exam_options.id"))
    correct_option_id: Mapped[int] = mapped_column(ForeignKey("exam_options.id"), nullable=False)
    is_correct: Mapped[int] = mapped_column(Integer, nullable=False)
    # 逐条作答时间（T-011 迁移新增）：存量行 NULL = 未采集，只从上线时刻起算
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)

    attempt: Mapped[ExamAttempt] = relationship(back_populates="answers")




class PracticeSession(Base):
    """专项练习/quiz 会话（T-022 刀二 / A12）：服务端为进行中会话唯一事实源。

    - question_ids 为选题快照，保证恢复原题；
    - answers JSON 为前端进度负载（作答选项 + 即时判反馈），服务端不解释其结构；
    - 单活跃约束：同用户至多一个 in_progress——POST 新会话时事务内将旧会话置 abandoned
      （作答保留，仅不再提示恢复），并发由 uq_active_practice_session 部分唯一索引兜底
      （postgres，见 ensure_schema）；
    - 无硬过期：服务端永不静默删除（A12 刀二业务规则，72h 只影响前端建议卡）。
    """

    __tablename__ = "practice_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    question_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    # 会话级恢复信息（流类型/config/书源等），服务端不解释其结构（②-2 恢复原题用）
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    answers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    current_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="in_progress", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class PracticeAttempt(Base):
    __tablename__ = "practice_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), default="practice", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    correct: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # attempt 级时长（T-014）：秒；存量行 NULL = 未采集
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    answers: Mapped[list["PracticeAttemptAnswer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class PracticeAttemptAnswer(Base):
    __tablename__ = "practice_attempt_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("practice_attempts.id"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("exam_questions.id"), index=True, nullable=False
    )
    chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("exam_options.id"))
    correct_option_id: Mapped[int] = mapped_column(ForeignKey("exam_options.id"), nullable=False)
    is_correct: Mapped[int] = mapped_column(Integer, nullable=False)
    # 逐条作答时间（T-011 迁移新增）：存量行 NULL = 未采集，只从上线时刻起算
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)

    attempt: Mapped[PracticeAttempt] = relationship(back_populates="answers")


class ExamWrongQuestion(Base):
    __tablename__ = "exam_wrong_questions"
    __table_args__ = (UniqueConstraint("user_id", "question_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("exam_questions.id"), index=True, nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    wrong_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_chosen_option_id: Mapped[int | None] = mapped_column(ForeignKey("exam_options.id"))
    last_wrong_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Favorite(Base):
    """收藏的题目。source 区分教材题（questions）/真题（exam_questions）；
    两张题表 ID 空间独立，故 question_id 不加数据库级外键，由应用层校验 + 列表 join 过滤悬挂行。"""

    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "source", "question_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    question_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
