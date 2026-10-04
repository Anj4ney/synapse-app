from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Engagement stats (added incrementally; nullable-with-default so rows
    # created before these columns existed keep working — see
    # database.ensure_schema_upgrades for the runtime column migration).
    xp = Column(Integer, default=0, nullable=False)
    # Streak tracking: streak_count is the current consecutive-day run;
    # last_active_date is the UTC datetime of the last engagement action
    # (lesson completion / quiz submission) used to decide increment vs reset.
    streak_count = Column(Integer, default=0, nullable=False)
    last_active_date = Column(DateTime, nullable=True)
    # Earned badge ids (strings) — a small fixed catalog lives in
    # app/achievements.py; anything not in the catalog is ignored on render.
    badges = Column(JSON, default=list, nullable=False)

    courses = relationship("Course", back_populates="owner", cascade="all, delete-orphan")


class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(String(500), default="")
    # List of {"title": str, "notes": str, "videoQuery": str, "completed": bool, "quiz": dict|None}
    modules = Column(JSON, default=list, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Public sharing (Feature 12): null = not shared. A UUID set by the owner
    # via POST /api/courses/{id}/share; /api/shared/{share_id} is read-only.
    share_id = Column(String(64), unique=True, nullable=True, index=True)

    owner = relationship("User", back_populates="courses")


class Note(Base):
    """Personal lesson notes (Feature 7) — a fully separate table; Course and
    User are untouched. Keyed by (user, course, module_index); indexes shift
    when lessons are removed/reordered, kept in sync by the courses router."""
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    module_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FinalQuiz(Base):
    """Feature 2 — the 20-mark end-of-course quiz, stored so a page refresh
    does not create a different quiz. One row per (user, course); a "retake"
    replaces this row with a freshly generated quiz (attempts are preserved
    separately in QuizAttempt).

    The quiz is split into two JSON columns for an explicit security boundary:
      - questions_json: list of {question, options:[str x4], topic, difficulty}
        — safe to send to the browser.
      - answers_json: list of {correct_index (0-3), explanation, topic,
        difficulty} — server-side ONLY, never sent to the browser before the
        quiz is submitted. Used to grade answers server-side.
    Both lists are parallel (same length, same order)."""
    __tablename__ = "final_quizzes"
    __table_args__ = (
        UniqueConstraint("user_id", "course_id", name="uq_final_quiz_user_course"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    # List of {question, options, topic, difficulty} — safe to send to browser
    questions_json = Column(JSON, nullable=False, default=list)
    # List of {correct_index, explanation, topic, difficulty} — server-only
    answers_json = Column(JSON, nullable=False, default=list)
    total = Column(Integer, nullable=False, default=20)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class QuizAttempt(Base):
    """Feature 2 — every attempt at a course's final quiz. Stored separately
    from FinalQuiz so retakes (which replace FinalQuiz) never lose history.
    The best passing score across all attempts is what unlocks the certificate
    and what `is_course_completed()` checks.

    Fields:
      - score / total / percentage / passed: grading summary
      - answers_json: the user's submitted answers (list of int|null, parallel
        to FinalQuiz.questions_json at submit time — captured here so a later
        retake that replaces the quiz doesn't make this attempt unreplayable)
      - topic_breakdown_json: {topic: {correct, total, pct}} per lesson
      - analysis_json: the AI-generated {summary, strengths, weaknesses, advice,
        next_steps} (or the rule-based fallback if Gemini failed)
    """
    __tablename__ = "quiz_attempts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    score = Column(Integer, nullable=False, default=0)
    total = Column(Integer, nullable=False, default=20)
    percentage = Column(Float, nullable=False, default=0.0)
    passed = Column(Boolean, nullable=False, default=False)
    answers_json = Column(JSON, nullable=False, default=list)
    topic_breakdown_json = Column(JSON, nullable=False, default=dict)
    analysis_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Certificate(Base):
    """Feature 3 — Certificate of Completion. One row per (user, course),
    created the first time the user requests it after passing the final quiz.
    Re-issued requests return the SAME row (the unique constraint below
    prevents duplicates). Stored server-side so the certificate code can be
    verified independently of the frontend.

    Fields:
      - certificate_code: short, unique, shareable id (SYN-XXXXXX). Generated
        once, never reused.
      - score / percentage: snapshot of the user's best passing final-quiz
        score at issue time, so the certificate always shows the score that
        actually earned it even if the user later retakes and scores lower.
      - issued_at: when the certificate was first issued.
    """
    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("user_id", "course_id", name="uq_certificate_user_course"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    certificate_code = Column(String(32), unique=True, nullable=False, index=True)
    score = Column(Integer, nullable=False, default=0)
    percentage = Column(Float, nullable=False, default=0.0)
    issued_at = Column(DateTime(timezone=True), server_default=func.now())
