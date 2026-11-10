from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    current_level: str = "N5"
    is_admin: bool = False

    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    username: str
    display_name: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class UserUpdate(BaseModel):
    current_level: str | None = None


class SessionOut(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


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


class ExamDetailOut(ExamOut):
    layers: list[ExamLayerOut]


class ExamAttemptAnswerIn(BaseModel):
    question_id: int
    chosen_option_id: int | None = None


class ExamAttemptIn(BaseModel):
    exam_id: int
    answers: list[ExamAttemptAnswerIn]


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
    user_id: int
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
    reviewed_by: int | None
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
