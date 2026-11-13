from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    current_level: str = "N5"
    is_admin: bool = False

    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    username: str
    password: str


# ---- T-028 自助注册 ----

class RegisterIn(BaseModel):
    username: str
    email: str
    password: str


class EmailIn(BaseModel):
    """重发验证 / 忘记密码：无论账号是否存在统一成功口径（不泄露状态）。"""

    email: str


class TokenIn(BaseModel):
    token: str


class ResetPasswordIn(BaseModel):
    token: str
    password: str


class StatusOut(BaseModel):
    status: str
    detail: str | None = None


class UserUpdate(BaseModel):
    current_level: str | None = None


class AdminUserOut(UserOut):
    created_at: datetime
    last_active_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AdminUserListOut(BaseModel):
    list: list[AdminUserOut]
    total: int


class AdminUserCreate(BaseModel):
    username: str
    display_name: str
    password: str
    current_level: str = "N5"


class AdminUserUpdate(BaseModel):
    display_name: str | None = None
    password: str | None = None
    current_level: str | None = None


class AdminSummaryTotalsOut(BaseModel):
    users: int
    active_users_7d: int
    new_users_7d: int
    questions_answered: int
    questions_answered_7d: int


class AdminTrendPointOut(BaseModel):
    period: str
    count: int


class AdminSummaryOut(BaseModel):
    totals: AdminSummaryTotalsOut
    registration_trend: list[AdminTrendPointOut]
    activity_trend: list[AdminTrendPointOut]


class SessionOut(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


class AdminMeOut(BaseModel):
    """后台 /api/admin/me 与登录返回的管理员信息：无学习语义字段（D12）。"""

    id: str
    username: str
    display_name: str

    model_config = ConfigDict(from_attributes=True)


class AdminSessionOut(BaseModel):
    token: str
    expires_at: datetime
    user: AdminMeOut


class BookOut(BaseModel):
    id: int
    external_id: int
    name: str
    level: str
    subject: str
    subject_label: str
    question_count: int

    model_config = ConfigDict(from_attributes=True)


class QuizOptionOut(BaseModel):
    id: int
    text: str


class QuizQuestionOut(BaseModel):
    id: int
    book_id: int
    book_name: str
    stem: str
    question_text: str
    options: list[QuizOptionOut]


class QuizOut(BaseModel):
    mode: str
    title: str
    level: str
    subject: str
    subject_label: str
    book_id: int | None = None
    questions: list[QuizQuestionOut]


class AttemptAnswerIn(BaseModel):
    question_id: int
    chosen_option_id: int | None = None


class CheckAnswerIn(BaseModel):
    question_id: int
    chosen_option_id: int | None = None


class CheckAnswerOut(BaseModel):
    question_id: int
    chosen_option_id: int | None
    correct_option_id: int
    answer_text: str
    is_correct: bool


class AttemptIn(BaseModel):
    mode: str
    level: str
    subject: str
    subject_label: str
    book_id: int | None = None
    title: str
    answers: list[AttemptAnswerIn]
    # attempt 级时长（T-014，秒）：客户端可选；服务端钳制后落库
    duration_seconds: int | None = None


class AttemptAnswerOut(BaseModel):
    question_id: int
    stem: str
    chosen_option_id: int | None
    chosen_text: str | None
    correct_option_id: int
    answer_text: str
    is_correct: bool
    options: list[QuizOptionOut] = []


class AttemptOut(BaseModel):
    id: int
    mode: str
    level: str
    subject: str
    subject_label: str
    book_id: int | None
    title: str
    correct: int
    total: int
    created_at: datetime
    answers: list[AttemptAnswerOut] = []


class WrongQuestionOut(BaseModel):
    question_id: int
    stem: str
    level: str
    subject: str
    subject_label: str
    book_id: int
    book_name: str
    answer_text: str
    wrong_count: int
    last_chosen_text: str | None
    last_wrong_at: datetime


class ExamOut(BaseModel):
    id: int
    external_id: str
    level: str
    title: str
    exam_date: int | None
    media_url: str
    question_count: int
    layer_count: int
    vocabulary_num: int | None
    grammar_num: int | None
    reading_num: int | None
    listening_num: int | None

    model_config = ConfigDict(from_attributes=True)


class ExamLayerOut(BaseModel):
    id: int
    external_id: str
    order_index: int
    title: str
    question_type: int | None
    data_type: int | None
    questions_num: int | None
    question_count: int
    image_url: str = ""

    model_config = ConfigDict(from_attributes=True)


class ExamOptionOut(BaseModel):
    id: int
    text: str
    source_order: int


class ExamQuestionOut(BaseModel):
    id: int
    external_id: str
    layer_id: int
    layer_external_id: str
    order_index: int
    question_type: int | None
    data_type: int | None
    title: str
    passage: str
    subtitle: str
    segment_count: int = 0
    image_url: str
    media_url: str
    options: list[ExamOptionOut]


class ExamQuestionReviewOut(ExamQuestionOut):
    analysis: str
    translation: str


class RandomExamQuestionOut(ExamQuestionOut):
    exam_id: int
    exam_title: str
    practice_order: int
    # 题组级材料图（读解图表/信息检索页面图），练习端渲染为整篇共享材料。
    layer_image_url: str = ""


class ExamDetailOut(ExamOut):
    layers: list[ExamLayerOut]


class ExamAttemptAnswerIn(BaseModel):
    question_id: int
    chosen_option_id: int | None = None


class ExamAttemptIn(BaseModel):
    exam_id: int
    answers: list[ExamAttemptAnswerIn]
    # attempt 级时长（T-014，秒）：客户端可选；服务端钳制后落库
    duration_seconds: int | None = None


class ExamSectionOut(BaseModel):
    key: str
    label: str
    duration_minutes: int
    question_ids: list[int]


class ExamRunOut(BaseModel):
    id: int
    exam_id: int
    status: str
    current_section: int
    section_started_at: datetime | None
    section_expires_at: datetime | None
    sections: list[ExamSectionOut]
    answers: dict[int, int | None] = {}
    attempt_id: int | None = None


class ExamRunAnswerIn(BaseModel):
    chosen_option_id: int | None = None


class ExamAttemptAnswerOut(BaseModel):
    question_id: int
    title: str
    chosen_option_id: int | None
    chosen_text: str | None
    correct_option_id: int
    answer_text: str
    analysis: str
    is_correct: bool


class ListeningSegmentOut(BaseModel):
    start: float
    end: float
    text: str
    translation: str = ""


class ListeningCheckOut(ExamAttemptAnswerOut):
    segments: list[ListeningSegmentOut] = []


class ExamAttemptOut(BaseModel):
    id: int
    exam_id: int
    level: str
    title: str
    correct: int
    total: int
    created_at: datetime
    answers: list[ExamAttemptAnswerOut] = []




class PracticeAttemptIn(BaseModel):
    level: str
    category: str
    mode: str = "practice"
    title: str
    answers: list[ExamAttemptAnswerIn]
    # attempt 级时长（T-014，秒）：客户端可选；服务端钳制后落库
    duration_seconds: int | None = None


class PracticeAttemptOut(BaseModel):
    id: int
    level: str
    category: str
    mode: str
    title: str
    correct: int
    total: int
    created_at: datetime
    answers: list[ExamAttemptAnswerOut] = []


class ExamWrongQuestionOut(BaseModel):
    question_id: int
    title: str
    level: str
    category: str
    category_label: str
    exam_id: int
    exam_title: str
    answer_text: str
    wrong_count: int
    last_chosen_text: str | None
    last_wrong_at: datetime


class FavoriteToggleIn(BaseModel):
    source: Literal["textbook", "exam"]
    question_id: int


class FavoriteOut(BaseModel):
    """收藏列表打平行：textbook 填 book_* 字段，exam 填 exam_* 字段。"""

    source: str
    question_id: int
    stem: str
    level: str
    category: str
    category_label: str
    book_id: int | None = None
    book_name: str | None = None
    exam_id: int | None = None
    exam_title: str | None = None
    answer_text: str
    created_at: datetime


class FavoriteIdsOut(BaseModel):
    textbook: list[int]
    exam: list[int]


class QuestionSearchOptionOut(BaseModel):
    id: int
    text: str


class QuestionSearchItemOut(BaseModel):
    id: int
    source: str
    source_label: str
    source_id: int
    source_title: str
    level: str
    category: str
    category_label: str
    stem: str
    order_index: int | None = None
    options: list[QuestionSearchOptionOut] = []


class QuestionSearchOut(BaseModel):
    items: list[QuestionSearchItemOut]
    total: int
    page: int
    page_size: int


class AdminExamQuestionListItemOut(BaseModel):
    id: int
    exam_id: int
    exam_title: str
    level: str
    order_index: int
    question_type: int | None
    category: str
    category_label: str
    title: str
    passage: str
    correct_option_id: int | None
    answer_status: str
    analysis_status: str
    answer_version: int
    reviewed_at: datetime | None
    # 题组 (layer) info so the revision UI can group shared-passage questions.
    layer_id: int
    layer_order: int
    layer_title: str
    # Options for read-only display of sibling 小题 in the same 题组.
    options: list[ExamOptionOut]


class AdminExamQuestionListOut(BaseModel):
    items: list[AdminExamQuestionListItemOut]
    total: int
    page: int
    page_size: int


class AdminExamListItemOut(BaseModel):
    """Exam paper with review progress stats for the 真题校正 paper list."""

    id: int
    title: str
    level: str
    exam_date: int | None
    question_count: int
    answer_confirmed: int
    analysis_reviewed: int
    disputed: int


class AdminExamListOut(BaseModel):
    items: list[AdminExamListItemOut]
    total: int


class AdminExamLayerQuestionsOut(BaseModel):
    """All 小题 of one 题组 (layer) with full review fields — 真题校正 問題工作台."""

    items: list["AdminExamQuestionDetailOut"]
    total: int
    # 题组级共享图片（读解图表/听力场景图，学生端渲染 layer.image_url）。
    layer_image_url: str | None = None


class AdminExamQuestionRevisionOut(BaseModel):
    id: int
    user_id: str
    before: dict
    after: dict
    note: str
    created_at: datetime


class AdminExamQuestionDetailOut(ExamQuestionReviewOut):
    exam_id: int
    exam_title: str
    level: str
    correct_option_id: int | None
    answer_status: str
    analysis_status: str
    analysis_document: dict = {}
    review_note: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    answer_version: int
    revisions: list[AdminExamQuestionRevisionOut] = []


class AdminExamQuestionUpdateIn(BaseModel):
    correct_option_id: int
    answer_status: str
    analysis_status: str
    subtitle: str = ""
    translation: str = ""
    analysis: str = ""
    analysis_document: dict = {}
    review_note: str = ""


class PracticeSessionCreateIn(BaseModel):
    """T-022 刀二：专项练习/quiz 会话创建。question_ids 为选题快照，meta 为会话级恢复信息。"""

    source: str
    label: str = ""
    question_ids: list[int]
    meta: dict = {}


class PracticeSessionAnswerIn(BaseModel):
    """按题写入进度：payload 为前端进度负载（选项 id 或含即时判反馈的对象），服务端不解释；
    current_index 顺带更新（None = 不动）。"""

    payload: object | None = None
    current_index: int | None = None


class PracticeSessionOut(BaseModel):
    id: str
    source: str
    label: str
    question_ids: list[int]
    meta: dict = {}
    answers: dict[str, object] = {}
    current_index: int
    status: str
    created_at: datetime
    updated_at: datetime
