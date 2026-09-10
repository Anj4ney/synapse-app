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


class QuizSubmitOut(BaseModel):
    xp_awarded: int
    xp: int
    level: int
    streak_count: int = 0
    badges: List[str] = []
    new_badges: List[str] = []
    course: CourseOut
