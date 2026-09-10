"""Engagement helpers for Synapse.

Holds:
  - touch_streak(): the daily-streak rule shared by every engagement action
    (lesson completion, quiz submission).
  - BADGES + evaluate(): a small fixed badge catalog and a checker that runs
    after engagement actions and appends newly earned badge ids to
    User.badges.

All helpers only mutate the ORM objects passed in — the calling endpoint's
existing db.commit() persists them, so response shapes never change.
"""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from . import models


# ---------------------------------------------------------------------------
# Badge catalog — the single source of truth for ids/conditions. The frontend
# keeps a matching catalog for display; unknown ids stored in older rows are
# simply ignored, and missing badges default to locked, so partial data never
# breaks rendering.
# ---------------------------------------------------------------------------
BADGES = {
    "first_lesson": {
        "name": "First Steps",
        "icon": "🥾",
        "description": "Complete your first lesson.",
    },
    "first_course_completed": {
        "name": "Course Conqueror",
        "icon": "🏆",
        "description": "Complete every lesson in a course.",
    },
    "first_quiz": {
        "name": "Quiz Rookie",
        "icon": "❓",
        "description": "Finish your first quiz attempt.",
    },
    "five_quizzes_passed": {
        "name": "Quiz Master",
        "icon": "🎯",
        "description": "Pass five quizzes (score at least two-thirds).",
    },
    "seven_day_streak": {
        "name": "On Fire",
        "icon": "🔥",
        "description": "Keep a seven-day learning streak.",
    },
    "xp_500": {
        "name": "Scholar",
        "icon": "⭐",
        "description": "Earn 500 XP.",
    },
    "flashcard_fan": {
        "name": "Flashcard Fan",
        "icon": "🃏",
        "description": "Generate flashcards for a lesson.",
    },
    "note_taker": {
        "name": "Note Taker",
        "icon": "📝",
        "description": "Write your first personal lesson note.",
    },
}


def touch_streak(user) -> None:
    """Update a user's daily streak for 'today' (UTC).

    Rules (per spec):
      - last active date is today        -> streak unchanged
      - last active date was yesterday   -> streak += 1
      - older than yesterday (or never)  -> streak resets to 1

    Also stamps last_active_date to now. Safe to call repeatedly within the
    same day; the day boundary is UTC, which matches how `created_at` and the
    quiz-attempt dates are recorded.
    """
    today = datetime.utcnow().date()
    last = user.last_active_date

    last_day: date | None = None
    if last is not None:
        # The column is a DateTime, but drivers can hand back a plain date;
        # normalize so the comparison below always sees dates.
        last_day = last.date() if isinstance(last, datetime) else last

    if last_day == today:
        return  # already active today — leave the streak alone
    if last_day == today - timedelta(days=1):
        user.streak_count = (user.streak_count or 0) + 1
    else:
        user.streak_count = 1

    user.last_active_date = datetime.utcnow()


def evaluate(user, db: Session) -> list:
    """Check every badge condition against the current DB state and append
    any newly earned badge ids to user.badges.

    Returns the list of *newly* earned ids (empty when nothing was earned) so
    endpoints can surface them without changing their existing responses.
    Call this AFTER the action's own state mutations (module updates, XP,
    streak) but BEFORE the endpoint's db.commit().
    """
    earned = set(user.badges or [])

    courses = (
        db.query(models.Course)
        .filter(models.Course.user_id == user.id)
        .all()
    )
    all_modules = [m for c in courses for m in (c.modules or [])]
    attempts = [a for m in all_modules for a in (m.get("quiz_attempts") or [])]

    def passed(a) -> bool:
        total = a.get("total") or 0
        return bool(total) and (a.get("score") or 0) * 3 >= total * 2

    course_completed = any(
        (c.modules or []) and all(m.get("completed") for m in c.modules)
        for c in courses
    )
    has_note = (
        db.query(models.Note.id).filter(models.Note.user_id == user.id).first()
        is not None
    )

    conditions = {
        "first_lesson": any(m.get("completed") for m in all_modules),
        "first_course_completed": course_completed,
        "first_quiz": len(attempts) >= 1,
        "five_quizzes_passed": sum(1 for a in attempts if passed(a)) >= 5,
        "seven_day_streak": (user.streak_count or 0) >= 7,
        "xp_500": (user.xp or 0) >= 500,
        "flashcard_fan": any(m.get("flashcards") for m in all_modules),
        "note_taker": has_note,
    }

    new_badges = [
        badge_id
        for badge_id, ok in conditions.items()
        if ok and badge_id in BADGES and badge_id not in earned
    ]

    if new_badges:
        # Reassign (never mutate in place) so SQLAlchemy's JSON change
        # detection fires.
        user.badges = sorted(earned | set(new_badges))

    return new_badges
