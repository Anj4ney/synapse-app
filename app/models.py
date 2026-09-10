from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Text
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
