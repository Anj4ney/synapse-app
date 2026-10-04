from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SignupIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    # Engagement stats — additive fields; level is computed on read
    # (xp // 100), never stored. Defaults keep older clients / rows without
    # the columns working unchanged.
    xp: int = 0
    level: int = 0
    streak_count: int = 0
    badges: List[str] = []


class ModuleOut(BaseModel):
    title: str
    notes: str = ""
    videoQuery: str = ""
    videoId: str = ""
    blogQuery: str = ""
    blogUrl: str = ""
    completed: bool = False
    quiz: Optional[Any] = None
    # Optional engagement fields — absent on older data, so they default to
    # None and the frontend simply hides those sections (graceful degrade).
    quiz_attempts: Optional[List[Any]] = None
    flashcards: Optional[List[Any]] = None
    diagram: Optional[str] = None


class CourseCreateIn(BaseModel):
    topic: str = Field(min_length=2, max_length=200)


class CourseUpdateIn(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


class ModuleAddIn(BaseModel):
    topic: str = Field(min_length=2, max_length=200)


class ModuleUpdateIn(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    videoQuery: Optional[str] = None
    blogQuery: Optional[str] = None


class ModuleCompleteIn(BaseModel):
    completed: bool


class ReorderIn(BaseModel):
    direction: str  # "up" | "down"


class QuizSubmitIn(BaseModel):
    # Graded client-side today (correctId is exposed to the browser), so the
    # frontend reports the outcome; validated so score can never exceed total.
    score: int = Field(ge=0)
    total: int = Field(ge=1)


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)


class AskOut(BaseModel):
    answer: str


class RegenerateIn(BaseModel):
    """Optional body for the existing regenerate endpoint (Feature 9).
    Omitted entirely -> None -> default difficulty (unchanged behavior)."""
    difficulty: Optional[Literal["simpler", "advanced"]] = None


class Eli5Out(BaseModel):
    notes: str


class ShareOut(BaseModel):
    share_id: str


class LeaderboardEntry(BaseModel):
    """Public leaderboard row — username, xp and derived level ONLY.
    Never password hashes, emails, ids, or any other user data."""
    username: str
    xp: int
    level: int


class SharedCourseOut(BaseModel):
    """Read-only public view of a course — title/description/modules only.
    Never includes user info (owner id/username) or edit capability."""
    title: str
    description: str = ""
    modules: List[ModuleOut] = []
    created_at: datetime


class NoteCreateIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    text: str
    created_at: datetime


class CourseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    description: str = ""
    modules: List[ModuleOut] = []
    created_at: datetime


class CourseSummaryOut(BaseModel):
    id: int
    title: str
    description: str = ""
    lesson_count: int
    completed_count: int
    created_at: datetime
    # Feature 1 — additive completion fields. All default so existing clients
    # that don't read them keep working byte-identically. `is_completed` is
    # the authoritative "course finished" flag (lessons done AND final quiz
    # passed, once Feature 2 lands); `final_quiz_passed` exposes just the
    # quiz-half of that rule so the dashboard can show a "Quiz passed" chip
    # independently; `final_score` is the best passing attempt's score (None
    # if no passing attempt yet).
    is_completed: bool = False
    final_quiz_passed: bool = False
    final_score: Optional[int] = None


class QuizSubmitOut(BaseModel):
    xp_awarded: int
    xp: int
    level: int
    streak_count: int = 0
    badges: List[str] = []
    new_badges: List[str] = []
    course: CourseOut


# ===========================================================================
# Feature 2 — Final quiz (20 marks) + AI performance analysis
# ===========================================================================

class FinalQuizQuestionOut(BaseModel):
    """What the browser sees for each question. Crucially has NO correct_index
    and NO explanation — those live server-side only and are returned only
    after the quiz is submitted (in FinalQuizReviewItem)."""
    question: str
    options: List[str]
    topic: str
    difficulty: str = "medium"


class FinalQuizOut(BaseModel):
    """The stored final quiz, safe to send to the browser. `questions` has no
    correct answers; grading is done server-side on submit."""
    id: int
    course_id: int
    total: int
    questions: List[FinalQuizQuestionOut]
    created_at: datetime


class FinalQuizSubmitIn(BaseModel):
    """Body for POST /final-quiz/submit. `answers` is a list parallel to the
    quiz's questions; each entry is the 0-based option index the user picked,
    or None if the question was left unanswered."""
    answers: List[Optional[int]]


class FinalQuizReviewItem(BaseModel):
    """One row of the post-submit review: the question, all options, the
    user's answer (None if skipped), the correct answer, and the explanation.
    Only ever returned from the submit endpoint — never from GET /final-quiz."""
    question: str
    options: List[str]
    topic: str
    difficulty: str = "medium"
    your_index: Optional[int] = None
    correct_index: int
    explanation: str = ""
    is_correct: bool


class TopicBreakdownItem(BaseModel):
    topic: str
    correct: int
    total: int
    pct: float


class FinalQuizResultOut(BaseModel):
    """The full result of a graded final-quiz submission. Includes:
      - score / total / percentage / passed
      - topic_breakdown: per-lesson correct/total/pct
      - analysis: AI strengths/weaknesses/advice (or rule-based fallback)
      - review: per-question review with correct answers + explanations
      - xp_awarded / xp / level / badges / new_badges (engagement side effects,
        same shape as the existing QuizSubmitOut so the frontend can reuse the
        same stat-application code path)
      - best_score / best_attempt_id: the user's best passing attempt, used to
        unlock the certificate
    """
    score: int
    total: int
    percentage: float
    passed: bool
    topic_breakdown: List[TopicBreakdownItem]
    analysis: dict
    review: List[FinalQuizReviewItem]
    attempt_id: int
    best_score: Optional[int] = None
    best_attempt_id: Optional[int] = None
    # Engagement side effects (XP for correct answers, streak, badges)
    xp_awarded: int
    xp: int
    level: int
    streak_count: int = 0
    badges: List[str] = []
    new_badges: List[str] = []


class QuizAttemptSummaryOut(BaseModel):
    """A single past attempt row, for the attempts-history endpoint."""
    id: int
    score: int
    total: int
    percentage: float
    passed: bool
    created_at: datetime


class FinalQuizAttemptsOut(BaseModel):
    """Response for GET /final-quiz/attempts — the list of past attempts plus
    the best passing score (None if no passing attempt yet). Used by the
    certificate unlock check and by the 'best score' chip on the course UI."""
    attempts: List[QuizAttemptSummaryOut]
    best_score: Optional[int] = None
    best_attempt_id: Optional[int] = None
    pass_mark: int


# ===========================================================================
# Feature 3 — Certificate of Completion
# ===========================================================================

class CertificateOut(BaseModel):
    """A single issued certificate. Returned by both the issue endpoint and
    the list endpoint. Carries everything the frontend needs to render the
    certificate (user name, course title, score, date, unique code) plus the
    course_id so the frontend can offer a 'back to course' link."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    course_id: int
    certificate_code: str
    score: int
    total: int
    percentage: float
    issued_at: datetime
    # Joined from the related Course + User so the frontend can render the
    # certificate without a second round-trip. These are NOT stored on the
    # certificate row itself.
    course_title: str = ""
    username: str = ""
