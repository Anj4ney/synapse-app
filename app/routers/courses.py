from datetime import datetime
from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import achievements, ai, models, schemas
from ..database import get_db
from .auth import get_current_user
async def generate_course(topic: str) -> dict:
    # ... (same prompt and Gemini call as before) ...
    raw = await _call_gemini(prompt, schema=course_schema, max_tokens=4000)
    parsed = _clean_json(raw)

    for m in parsed["modules"]:
        m["completed"] = False
        m["quiz"] = None
        # Automatically resolve the search query into a real playable video ID
        m["videoId"] = await get_youtube_video_id(m.get("videoQuery", topic))
        
    return parsed

router = APIRouter(prefix="/api/courses", tags=["courses"])

# Personal notes live under course-scoped paths; only the delete route sits
# under its own /api/notes prefix (per spec). Registered in main.py.
notes_router = APIRouter(prefix="/api/notes", tags=["notes"])

# Public sharing (Feature 12): unauthenticated read-only course view.
shared_router = APIRouter(prefix="/api/shared", tags=["sharing"])

# XP awards for engagement actions (spec: +10 per completed lesson, +5 per
# correct quiz answer). Pure side effects — they never alter any existing
# endpoint's response shape.
XP_PER_COMPLETED_LESSON = 10
XP_PER_CORRECT_ANSWER = 5

# ---------------------------------------------------------------------------
# Feature 1 — Course completion tracking (foundation for Features 2-5)
#
# PASS_MARK is the minimum score (out of 20) required to pass the final quiz
# and therefore to "complete" a course. Defined here as a single constant so
# it is trivial to change later. The final-quiz endpoints (Feature 2) and the
# certificate endpoint (Feature 3) both reference this same constant.
#
# is_course_completed() is the SINGLE source of truth for "has this user
# finished this course". Every feature (dashboard sorting, certificate
# gating, final-quiz unlock) calls this helper instead of re-implementing the
# rule. The rule is: ALL lessons marked complete AND a passing final-quiz
# attempt exists. Until Feature 2's final-quiz tables exist, the second clause
# is a no-op (returns True as soon as all lessons are done), so this helper
# is safe to land first.
# ---------------------------------------------------------------------------
PASS_MARK = 10  # out of 20 — change here to retune the pass threshold
FINAL_QUIZ_TOTAL = 20  # total marks the final quiz is graded out of


def _all_lessons_complete(course: models.Course) -> bool:
    """True iff the course has at least one lesson and every one is marked
    complete. Kept as a small helper so is_course_completed stays readable."""
    mods = course.modules or []
    return bool(mods) and all(bool(m.get("completed", False)) for m in mods)


def _best_passing_final_quiz_score(user_id: int, course_id: int, db: Session):
    """Returns the best passing final-quiz score for (user, course), or None
    if no passing attempt exists yet. Defensive: lazy-imports QuizAttempt so
    the module keeps importing even if the model is temporarily unavailable,
    and never raises (status helpers must be side-effect-free)."""
    try:
        from .. import models as _m
        QuizAttempt = getattr(_m, "QuizAttempt", None)
        if QuizAttempt is None:
            return None
        attempt = (
            db.query(QuizAttempt)
            .filter(
                QuizAttempt.user_id == user_id,
                QuizAttempt.course_id == course_id,
                QuizAttempt.passed.is_(True),
            )
            .order_by(QuizAttempt.score.desc(), QuizAttempt.created_at.desc())
            .first()
        )
        return attempt.score if attempt else None
    except Exception:
        # Never raise from a status helper — callers depend on this being
        # best-effort. A None return just means "not completed yet".
        return None


def is_course_completed(user_id: int, course_id: int, db: Session) -> bool:
    """Single source of truth: a course is "completed" iff every lesson is
    marked complete AND the user has a passing (>= PASS_MARK) final-quiz
    attempt. Reused by the dashboard summary, the certificate endpoint, and
    the final-quiz unlock check.

    Feature 2 is now live: the QuizAttempt table exists, so the second clause
    is enforced. A course with all lessons done but no passing final-quiz
    attempt is NOT completed — it stays in the "In Progress" bucket until
    the learner passes the final quiz."""
    course = (
        db.query(models.Course)
        .filter(models.Course.id == course_id, models.Course.user_id == user_id)
        .first()
    )
    if not course:
        return False
    if not _all_lessons_complete(course):
        return False
    return _best_passing_final_quiz_score(user_id, course_id, db) is not None


def _get_owned_course(course_id: int, user: models.User, db: Session) -> models.Course:
    course = (
        db.query(models.Course)
        .filter(models.Course.id == course_id, models.Course.user_id == user.id)
        .first()
    )
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    return course


@router.get("", response_model=List[schemas.CourseSummaryOut])
def list_courses(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    courses = (
        db.query(models.Course)
        .filter(models.Course.user_id == user.id)
        .order_by(models.Course.created_at.desc())
        .all()
    )
    out = []
    for c in courses:
        mods = c.modules or []
        # Feature 1 — populate the additive completion fields via the single
        # is_course_completed() helper so the rule lives in one place.
        best_passing = _best_passing_final_quiz_score(user.id, c.id, db)
        final_quiz_passed = best_passing is not None
        # Until Feature 2 lands, is_course_completed() returns True as soon
        # as all lessons are done (no passing attempt exists yet, but the
        # helper treats that as "no final-quiz requirement yet"). Once
        # Feature 2 is live, is_course_completed() becomes "lessons done AND
        # quiz passed" automatically — no change needed here.
        completed = is_course_completed(user.id, c.id, db)
        out.append(
            schemas.CourseSummaryOut(
                id=c.id,
                title=c.title,
                description=c.description or "",
                lesson_count=len(mods),
                completed_count=sum(1 for m in mods if m.get("completed")),
                created_at=c.created_at,
                is_completed=completed,
                final_quiz_passed=final_quiz_passed,
                final_score=best_passing,
            )
        )
    return out


@router.post("", response_model=schemas.CourseOut)
async def create_course(
    payload: schemas.CourseCreateIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    try:
        generated = await ai.generate_course(payload.topic)
    except ai.AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    course = models.Course(
        user_id=user.id,
        title=generated["title"],
        description=generated.get("description", ""),
        modules=generated["modules"],
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


@router.get("/{course_id}", response_model=schemas.CourseOut)
def get_course(
    course_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    return _get_owned_course(course_id, user, db)


@router.put("/{course_id}", response_model=schemas.CourseOut)
def update_course(
    course_id: int,
    payload: schemas.CourseUpdateIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    course = _get_owned_course(course_id, user, db)
    if payload.title is not None and payload.title.strip():
        course.title = payload.title.strip()
    if payload.description is not None:
        course.description = payload.description.strip()
    db.commit()
    db.refresh(course)
    return course


@router.delete("/{course_id}")
def delete_course(
    course_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    course = _get_owned_course(course_id, user, db)
    # Personal notes (Feature 7) reference the course; drop them first so
    # FK-constrained databases (Postgres) don't reject the course delete.
    db.query(models.Note).filter(models.Note.course_id == course.id).delete()
    db.delete(course)
    db.commit()
    return {"ok": True}


@router.post("/{course_id}/modules", response_model=schemas.CourseOut)
async def add_module(
    course_id: int,
    payload: schemas.ModuleAddIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    course = _get_owned_course(course_id, user, db)
    try:
        mod = await ai.generate_module(course.title, payload.topic)
    except ai.AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    modules = list(course.modules or [])
    modules.append(mod)
    course.modules = modules
    db.commit()
    db.refresh(course)
    return course


@router.put("/{course_id}/modules/{index}", response_model=schemas.CourseOut)
async def update_module(
    course_id: int,
    index: int,
    payload: schemas.ModuleUpdateIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    mod = dict(modules[index])
    if payload.title is not None and payload.title.strip():
        mod["title"] = payload.title.strip()
    if payload.notes is not None:
        mod["notes"] = payload.notes
    if payload.videoQuery is not None:
        new_query = payload.videoQuery.strip()
        if new_query != mod.get("videoQuery", ""):
            mod["videoQuery"] = new_query
            # Re-resolve to a real playable video ID whenever the search
            # phrase actually changes, so the embedded video stays in sync.
            mod["videoId"] = await ai.get_youtube_video_id(new_query)
    modules[index] = mod
    course.modules = modules
    db.commit()
    db.refresh(course)
    return course


@router.delete("/{course_id}/modules/{index}", response_model=schemas.CourseOut)
def delete_module(
    course_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if len(modules) <= 1:
        raise HTTPException(status_code=400, detail="A course needs at least one lesson.")
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    modules.pop(index)
    # Keep personal notes (Feature 7) attached to the right lesson: drop the
    # removed lesson's notes and shift later ones down by one.
    db.query(models.Note).filter(
        models.Note.course_id == course.id, models.Note.module_index == index
    ).delete(synchronize_session=False)
    db.query(models.Note).filter(
        models.Note.course_id == course.id, models.Note.module_index > index
    ).update({models.Note.module_index: models.Note.module_index - 1}, synchronize_session=False)
    course.modules = modules
    db.commit()
    db.refresh(course)
    return course


@router.post("/{course_id}/modules/{index}/reorder", response_model=schemas.CourseOut)
def reorder_module(
    course_id: int,
    index: int,
    payload: schemas.ReorderIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    target = index + (-1 if payload.direction == "up" else 1)
    if index < 0 or index >= len(modules) or target < 0 or target >= len(modules):
        raise HTTPException(status_code=400, detail="Cannot move lesson there.")

    modules[index], modules[target] = modules[target], modules[index]
    # Swap personal-note indexes (Feature 7) so notes travel with their
    # lesson. Uses a temp value because module_index is not unique.
    notes_q = db.query(models.Note).filter(models.Note.course_id == course.id)
    notes_q.filter(models.Note.module_index == index).update(
        {models.Note.module_index: -1}, synchronize_session=False)
    notes_q.filter(models.Note.module_index == target).update(
        {models.Note.module_index: index}, synchronize_session=False)
    notes_q.filter(models.Note.module_index == -1).update(
        {models.Note.module_index: target}, synchronize_session=False)
    course.modules = modules
    db.commit()
    db.refresh(course)
    return course


@router.post("/{course_id}/modules/{index}/regenerate", response_model=schemas.CourseOut)
async def regenerate_module(
    course_id: int,
    index: int,
    payload: Optional[schemas.RegenerateIn] = None,   # optional body (Feature 9)
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    # No body (the pre-Feature-9 client behavior) -> difficulty None ->
    # the exact same prompt as before. Only an explicit difficulty changes it.
    difficulty = payload.difficulty if payload else None

    try:
        fresh = await ai.generate_module(course.title, modules[index]["title"], difficulty=difficulty)
    except ai.AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    fresh["completed"] = modules[index].get("completed", False)
    modules[index] = fresh
    course.modules = modules
    db.commit()
    db.refresh(course)
    return course


@router.patch("/{course_id}/modules/{index}/complete", response_model=schemas.CourseOut)
def toggle_complete(
    course_id: int,
    index: int,
    payload: schemas.ModuleCompleteIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    was_completed = bool(modules[index].get("completed", False))
    mod = dict(modules[index])
    mod["completed"] = payload.completed
    modules[index] = mod
    course.modules = modules

    # Engagement side effect: award XP only on the not-done -> done transition
    # (un-completing never awards, and re-completing after un-completing
    # doesn't double-award within a single toggle). Committed by the existing
    # db.commit() below; the response body is unchanged.
    if payload.completed and not was_completed:
        user.xp = (user.xp or 0) + XP_PER_COMPLETED_LESSON
    # Streaks count any lesson-completion action (not un-completions).
    if payload.completed:
        achievements.touch_streak(user)
    # Badges: re-evaluate after the action's own state changes, before commit.
    achievements.evaluate(user, db)

    db.commit()
    db.refresh(course)
    return course


@router.post("/{course_id}/modules/{index}/quiz/submit", response_model=schemas.QuizSubmitOut)
def submit_quiz(
    course_id: int,
    index: int,
    payload: schemas.QuizSubmitIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Reports the outcome of a quiz the frontend graded client-side and
    awards XP (+5 per correct answer). New endpoint — the existing quiz
    *generation* endpoint is untouched. Later features (streaks, badges,
    attempt history) hook into this same action point."""
    if payload.score > payload.total:
        raise HTTPException(status_code=400, detail="Score cannot exceed total.")

    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    xp_awarded = XP_PER_CORRECT_ANSWER * payload.score
    user.xp = (user.xp or 0) + xp_awarded
    achievements.touch_streak(user)

    # Quiz history: append the attempt (spec shape {score, total, date}) to
    # the module's quiz_attempts list. module["quiz"] itself is untouched
    # here and on regeneration, so the existing quiz flow is unchanged.
    mod = dict(modules[index])
    attempts = list(mod.get("quiz_attempts") or [])
    attempts.append({
        "score": payload.score,
        "total": payload.total,
        "date": datetime.utcnow().isoformat(timespec="seconds"),
    })
    mod["quiz_attempts"] = attempts
    modules[index] = mod
    course.modules = modules

    new_badges = achievements.evaluate(user, db)
    db.commit()
    db.refresh(user)
    db.refresh(course)

    xp = user.xp or 0
    return schemas.QuizSubmitOut(
        xp_awarded=xp_awarded,
        xp=xp,
        level=xp // 100,
        streak_count=user.streak_count or 0,
        badges=list(user.badges or []),
        new_badges=new_badges,
        course=course,
    )


@router.post("/{course_id}/modules/{index}/quiz", response_model=schemas.CourseOut)
async def generate_quiz_route(
    course_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    try:
        quiz = await ai.generate_quiz(modules[index]["title"], modules[index].get("notes", ""))
    except ai.AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    mod = dict(modules[index])
    mod["quiz"] = quiz
    # Regenerating replaces the active quiz but never wipes the attempt
    # history recorded on this module (quiz_attempts), per spec.
    modules[index] = mod
    course.modules = modules
    db.commit()
    db.refresh(course)
    return course


@router.post("/{course_id}/modules/{index}/flashcards", response_model=schemas.CourseOut)
async def generate_flashcards_route(
    course_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Generate flashcards for a lesson and store them on the module's new
    optional `flashcards` field (list of {front, back}). Existing fields are
    untouched; modules without the field keep rendering normally."""
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    try:
        result = await ai.generate_flashcards(modules[index]["title"], modules[index].get("notes", ""))
    except ai.AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    mod = dict(modules[index])
    mod["flashcards"] = result["cards"]
    modules[index] = mod
    course.modules = modules
    achievements.evaluate(user, db)   # may award flashcard_fan
    db.commit()
    db.refresh(course)
    return course


@router.post("/{course_id}/modules/{index}/ask", response_model=schemas.AskOut)
async def ask_lesson_question(
    course_id: int,
    index: int,
    payload: schemas.AskIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """"Ask about this lesson" (Feature 6): sends the lesson notes plus the
    learner's question to Gemini and returns the answer. Stateless — nothing
    is persisted, so the course data is never touched."""
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    try:
        answer = await ai.answer_question(
            modules[index]["title"], modules[index].get("notes", ""), payload.question.strip()
        )
    except ai.AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return schemas.AskOut(answer=answer)


@router.post("/{course_id}/modules/{index}/diagram", response_model=schemas.CourseOut)
async def generate_diagram_route(
    course_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Auto-generated diagrams (Feature 10): asks Gemini for a Mermaid.js
    diagram for the lesson. Stored on the new optional module["diagram"] field
    only when the model returns one — if it decides a diagram wouldn't help,
    the module is left untouched (an existing diagram is never destroyed)."""
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    try:
        result = await ai.generate_diagram(modules[index]["title"], modules[index].get("notes", ""))
    except ai.AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if result.get("diagram"):
        mod = dict(modules[index])
        mod["diagram"] = result["diagram"]
        modules[index] = mod
        course.modules = modules

    db.commit()
    db.refresh(course)
    return course


@router.post("/{course_id}/modules/{index}/eli5", response_model=schemas.Eli5Out)
async def eli5_lesson(
    course_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """"Explain Like I'm 5" (Feature 11): resummarizes the lesson's notes at a
    simpler reading level and returns the text directly. The stored notes are
    never overwritten — this endpoint does not touch course data at all."""
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    try:
        simple = await ai.explain_like_im_five(
            modules[index]["title"], modules[index].get("notes", "")
        )
    except ai.AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return schemas.Eli5Out(notes=simple)


@router.post("/{course_id}/modules/{index}/notes", response_model=schemas.NoteOut)
def create_lesson_note(
    course_id: int,
    index: int,
    payload: schemas.NoteCreateIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Personal notes (Feature 7): a learner-authored note attached to a
    specific lesson. Stored in its own table — Course/User are untouched."""
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    note = models.Note(
        user_id=user.id,
        course_id=course.id,
        module_index=index,
        text=payload.text.strip(),
    )
    db.add(note)
    achievements.evaluate(user, db)   # may award note_taker
    db.commit()
    db.refresh(note)
    return note


@router.get("/{course_id}/modules/{index}/notes", response_model=List[schemas.NoteOut])
def list_lesson_notes(
    course_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    course = _get_owned_course(course_id, user, db)
    modules = list(course.modules or [])
    if index < 0 or index >= len(modules):
        raise HTTPException(status_code=404, detail="Lesson not found.")

    return (
        db.query(models.Note)
        .filter(
            models.Note.user_id == user.id,
            models.Note.course_id == course.id,
            models.Note.module_index == index,
        )
        .order_by(models.Note.created_at.asc(), models.Note.id.asc())
        .all()
    )


@notes_router.delete("/{note_id}")
def delete_lesson_note(
    note_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    note = (
        db.query(models.Note)
        .filter(models.Note.id == note_id, models.Note.user_id == user.id)
        .first()
    )
    if not note:
        raise HTTPException(status_code=404, detail="Note not found.")
    db.delete(note)
    db.commit()
    return {"ok": True}


@router.post("/{course_id}/share", response_model=schemas.ShareOut)
def share_course(
    course_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Public sharing (Feature 12): generates a UUID share link for the
    owner's course. Calling it again returns the same id — links are stable."""
    course = _get_owned_course(course_id, user, db)
    if not course.share_id:
        course.share_id = str(uuid.uuid4())
        db.commit()
        db.refresh(course)
    return schemas.ShareOut(share_id=course.share_id)


@shared_router.get("/{share_id}", response_model=schemas.SharedCourseOut)
def get_shared_course(
    share_id: str,
    db: Session = Depends(get_db),
):
    """Unauthenticated, read-only view of a shared course. Returns only the
    course content — never user info, and no edit capability."""
    course = db.query(models.Course).filter(models.Course.share_id == share_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Shared course not found.")
    return schemas.SharedCourseOut(
        title=course.title,
        description=course.description or "",
        modules=course.modules or [],
        created_at=course.created_at,
    )


# ===========================================================================
# Feature 2 — Final quiz (20 marks) + AI performance analysis
#
# All routes live under the existing /api/courses/{course_id}/ prefix so the
# current Vercel rewrite rule handles them with no vercel.json change.
#
# Routes:
#   POST   /api/courses/{course_id}/final-quiz          generate + store (403 if locked)
#   GET    /api/courses/{course_id}/final-quiz          fetch stored quiz (no answers)
#   POST   /api/courses/{course_id}/final-quiz/retake   regenerate, replace stored quiz
#   POST   /api/courses/{course_id}/final-quiz/submit   grade server-side, store attempt
#   GET    /api/courses/{course_id}/final-quiz/attempts list past attempts + best score
# ===========================================================================

def _require_lessons_complete(course: models.Course):
    """Helper for the final-quiz routes: 403 if not every lesson is done.
    The spec requires this to be enforced on the backend, not just the UI."""
    if not _all_lessons_complete(course):
        done = sum(1 for m in (course.modules or []) if m.get("completed"))
        total = len(course.modules or [])
        raise HTTPException(
            status_code=403,
            detail=f"Complete all lessons to unlock the final quiz ({done}/{total} done).",
        )


@router.post("/{course_id}/final-quiz", response_model=schemas.FinalQuizOut)
async def generate_final_quiz_route(
    course_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Generate a fresh 20-question final quiz and store it. Locked until
    every lesson in the course is marked complete (enforced server-side).
    If a stored quiz already exists, this REPLACES it (acts as a retake too)
    — past attempts are preserved in quiz_attempts."""
    course = _get_owned_course(course_id, user, db)
    _require_lessons_complete(course)

    try:
        result = await ai.generate_final_quiz(course.title, list(course.modules or []))
    except ai.AIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Upsert: replace any existing row for (user, course)
    existing = (
        db.query(models.FinalQuiz)
        .filter(
            models.FinalQuiz.user_id == user.id,
            models.FinalQuiz.course_id == course.id,
        )
        .first()
    )
    if existing:
        existing.questions_json = result["questions"]
        existing.answers_json = result["answers"]
        existing.total = len(result["questions"])
        existing.created_at = datetime.utcnow()
        fq = existing
    else:
        fq = models.FinalQuiz(
            user_id=user.id,
            course_id=course.id,
            questions_json=result["questions"],
            answers_json=result["answers"],
            total=len(result["questions"]),
        )
        db.add(fq)
    db.commit()
    db.refresh(fq)
    return _serialize_final_quiz(fq)


@router.get("/{course_id}/final-quiz", response_model=schemas.FinalQuizOut)
def get_final_quiz_route(
    course_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Fetch the stored final quiz for this course. Returns questions and
    options only — NEVER correct_index or explanation (those are returned
    only after submission). 404 if no quiz has been generated yet."""
    course = _get_owned_course(course_id, user, db)
    fq = (
        db.query(models.FinalQuiz)
        .filter(
            models.FinalQuiz.user_id == user.id,
            models.FinalQuiz.course_id == course.id,
        )
        .first()
    )
    if not fq:
        raise HTTPException(status_code=404, detail="No final quiz generated yet.")
    return _serialize_final_quiz(fq)


@router.post("/{course_id}/final-quiz/retake", response_model=schemas.FinalQuizOut)
async def retake_final_quiz_route(
    course_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Generate a fresh quiz, replacing the stored one. Past attempts are
    preserved in quiz_attempts. Still subject to the lessons-complete gate
    (you can't retake a quiz you couldn't take in the first place)."""
    # Reuses the generate endpoint's logic — same path, same gating.
    return await generate_final_quiz_route(course_id, db, user)


@router.post("/{course_id}/final-quiz/submit", response_model=schemas.FinalQuizResultOut)
async def submit_final_quiz_route(
    course_id: int,
    payload: schemas.FinalQuizSubmitIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Grade a submitted final quiz server-side. Stores the attempt, computes
    the per-topic breakdown, asks Gemini for a strengths/weaknesses/advice
    analysis (with a rule-based fallback if Gemini fails), awards XP, touches
    the streak, and re-evaluates badges. Returns the full review (correct
    answers + explanations) plus the analysis so the frontend can render the
    results screen in one shot."""
    course = _get_owned_course(course_id, user, db)
    # The spec says submission should also be gated on lessons-complete —
    # you can't submit a final quiz for a course whose lessons aren't done.
    _require_lessons_complete(course)

    fq = (
        db.query(models.FinalQuiz)
        .filter(
            models.FinalQuiz.user_id == user.id,
            models.FinalQuiz.course_id == course.id,
        )
        .first()
    )
    if not fq:
        raise HTTPException(
            status_code=404,
            detail="No final quiz to submit. Generate one first.",
        )

    questions = list(fq.questions_json or [])
    answers_key = list(fq.answers_json or [])
    if len(questions) != len(answers_key):
        raise HTTPException(status_code=500, detail="Stored quiz is corrupted.")
    total = len(questions)

    # Normalize + validate the submitted answers list (parallel to questions).
    raw_answers = list(payload.answers or [])
    if len(raw_answers) != total:
        # Pad missing trailing answers with None rather than reject — the
        # frontend warns about unanswered questions before submit, but the
        # server must be lenient (treat anything out of range as unanswered).
        if len(raw_answers) > total:
            raise HTTPException(
                status_code=400,
                detail=f"Expected {total} answers, got {len(raw_answers)}.",
            )
        raw_answers = raw_answers + [None] * (total - len(raw_answers))

    norm_answers = []
    for a in raw_answers:
        if a is None:
            norm_answers.append(None)
            continue
        try:
            ai_val = int(a)
        except (TypeError, ValueError):
            norm_answers.append(None)
            continue
        if 0 <= ai_val < 4:
            norm_answers.append(ai_val)
        else:
            norm_answers.append(None)

    # Grade server-side
    correct_count = 0
    topic_breakdown = {}  # topic -> {correct, total}
    review_items = []
    wrong_questions_for_analysis = []
    for i, q in enumerate(questions):
        ans_meta = answers_key[i] if i < len(answers_key) else {}
        topic = q.get("topic", "Unknown")
        correct_index = int(ans_meta.get("correct_index", -1))
        your_index = norm_answers[i]
        is_correct = (your_index is not None) and (your_index == correct_index)
        if is_correct:
            correct_count += 1
        # topic breakdown
        tb = topic_breakdown.setdefault(topic, {"correct": 0, "total": 0})
        tb["total"] += 1
        if is_correct:
            tb["correct"] += 1
        # review item
        review_items.append(schemas.FinalQuizReviewItem(
            question=q.get("question", ""),
            options=list(q.get("options", [])),
            topic=topic,
            difficulty=q.get("difficulty", "medium"),
            your_index=your_index,
            correct_index=correct_index,
            explanation=ans_meta.get("explanation", ""),
            is_correct=is_correct,
        ))
        if not is_correct:
            opts = list(q.get("options", []))
            wrong_questions_for_analysis.append({
                "question": q.get("question", ""),
                "topic": topic,
                "your_answer": opts[your_index] if (your_index is not None and your_index < len(opts)) else "(no answer)",
                "correct_answer": opts[correct_index] if (0 <= correct_index < len(opts)) else "(unknown)",
            })

    # Finalize the topic breakdown (compute pct per topic)
    for topic, tb in topic_breakdown.items():
        tb["pct"] = round(100.0 * tb["correct"] / tb["total"], 1) if tb["total"] else 0.0

    percentage = round(100.0 * correct_count / total, 1) if total else 0.0
    passed = correct_count >= PASS_MARK

    # AI analysis with rule-based fallback — the submission NEVER fails just
    # because Gemini is unavailable. We try the AI; on any failure we use the
    # deterministic rule-based analysis derived from the topic breakdown.
    try:
        analysis = await ai.generate_quiz_analysis(
            course.title, correct_count, total, topic_breakdown,
            wrong_questions_for_analysis,
        )
    except Exception as e:
        print(f"[final-quiz] AI analysis failed ({e}); using rule-based fallback.")
        analysis = ai.rule_based_analysis(
            course.title, correct_count, total, topic_breakdown,
            wrong_questions_for_analysis,
        )

    # Persist the attempt (answers snapshot captured here so a later retake
    # that replaces the quiz doesn't make this attempt unreplayable)
    attempt = models.QuizAttempt(
        user_id=user.id,
        course_id=course.id,
        score=correct_count,
        total=total,
        percentage=percentage,
        passed=passed,
        answers_json=norm_answers,
        topic_breakdown_json=topic_breakdown,
        analysis_json=analysis,
    )
    db.add(attempt)

    # Engagement side effects: +5 XP per correct answer (same rate as the
    # existing per-lesson quiz), touch streak, re-evaluate badges.
    xp_awarded = XP_PER_CORRECT_ANSWER * correct_count
    user.xp = (user.xp or 0) + xp_awarded
    achievements.touch_streak(user)
    new_badges = achievements.evaluate(user, db)
    db.commit()
    db.refresh(user)
    db.refresh(attempt)

    # Best passing attempt (for certificate unlock + dashboard 'final_score')
    best_passing = (
        db.query(models.QuizAttempt)
        .filter(
            models.QuizAttempt.user_id == user.id,
            models.QuizAttempt.course_id == course.id,
            models.QuizAttempt.passed.is_(True),
        )
        .order_by(models.QuizAttempt.score.desc(), models.QuizAttempt.created_at.desc())
        .first()
    )

    xp = user.xp or 0
    return schemas.FinalQuizResultOut(
        score=correct_count,
        total=total,
        percentage=percentage,
        passed=passed,
        topic_breakdown=[
            schemas.TopicBreakdownItem(topic=t, **data)
            for t, data in topic_breakdown.items()
        ],
        analysis=analysis,
        review=review_items,
        attempt_id=attempt.id,
        best_score=best_passing.score if best_passing else None,
        best_attempt_id=best_passing.id if best_passing else None,
        xp_awarded=xp_awarded,
        xp=xp,
        level=xp // 100,
        streak_count=user.streak_count or 0,
        badges=list(user.badges or []),
        new_badges=new_badges,
    )


@router.get("/{course_id}/final-quiz/attempts", response_model=schemas.FinalQuizAttemptsOut)
def list_final_quiz_attempts_route(
    course_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """List this user's past attempts for the course's final quiz, plus the
    best passing score (None if no passing attempt yet). Used by the course
    UI's 'best score' chip and by the certificate unlock check."""
    course = _get_owned_course(course_id, user, db)
    attempts = (
        db.query(models.QuizAttempt)
        .filter(
            models.QuizAttempt.user_id == user.id,
            models.QuizAttempt.course_id == course.id,
        )
        .order_by(models.QuizAttempt.created_at.desc())
        .all()
    )
    best_passing = (
        db.query(models.QuizAttempt)
        .filter(
            models.QuizAttempt.user_id == user.id,
            models.QuizAttempt.course_id == course.id,
            models.QuizAttempt.passed.is_(True),
        )
        .order_by(models.QuizAttempt.score.desc(), models.QuizAttempt.created_at.desc())
        .first()
    )
    return schemas.FinalQuizAttemptsOut(
        attempts=[
            schemas.QuizAttemptSummaryOut(
                id=a.id, score=a.score, total=a.total,
                percentage=a.percentage, passed=a.passed, created_at=a.created_at,
            )
            for a in attempts
        ],
        best_score=best_passing.score if best_passing else None,
        best_attempt_id=best_passing.id if best_passing else None,
        pass_mark=PASS_MARK,
    )


def _serialize_final_quiz(fq: models.FinalQuiz) -> schemas.FinalQuizOut:
    """Project a FinalQuiz ORM row into the browser-safe FinalQuizOut —
    questions and options only, NO correct_index, NO explanation. This is
    the single place that strips the answers, so it's easy to audit."""
    return schemas.FinalQuizOut(
        id=fq.id,
        course_id=fq.course_id,
        total=fq.total,
        questions=[
            schemas.FinalQuizQuestionOut(
                question=q.get("question", ""),
                options=list(q.get("options", [])),
                topic=q.get("topic", ""),
                difficulty=q.get("difficulty", "medium"),
            )
            for q in (fq.questions_json or [])
        ],
        created_at=fq.created_at,
    )


# ===========================================================================
# Feature 3 — Certificate of Completion
#
# Routes:
#   POST /api/courses/{course_id}/certificate  issue (or fetch existing)
#   GET  /api/courses/{course_id}/certificate  fetch existing (404 if none)
#   GET  /api/certificates                      list all of the user's certs
#
# Issuing is gated on is_course_completed() (403 otherwise) — the single
# source of truth from Feature 1, which now requires lessons done AND a
# passing final-quiz attempt (Feature 2). The unique (user, course) index on
# the certificates table + an explicit find-first upsert means a certificate
# is issued ONCE; subsequent POSTs return the existing row unchanged.
# ===========================================================================

# Dedicated router for the cross-course certificates list. Registered in
# main.py alongside the others.
certificates_router = APIRouter(prefix="/api/certificates", tags=["certificates"])

# Charset for certificate codes: unambiguous letters + digits (no 0/O/1/I/L)
# so codes stay readable when read aloud or retyped.
_CERT_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _serialize_certificate(cert: models.Certificate, course: models.Course = None,
                           user: models.User = None) -> schemas.CertificateOut:
    """Project a Certificate row + its related Course/User into the
    browser-safe CertificateOut. course_title and username are joined in
    here so the frontend can render a certificate with one API call."""
    course_title = ""
    if course is None:
        course = cert.course if hasattr(cert, "course") else None
    if course is not None:
        course_title = course.title or ""
    username = ""
    if user is None:
        user = cert.user if hasattr(cert, "user") else None
    if user is not None:
        username = user.username or ""
    return schemas.CertificateOut(
        id=cert.id,
        course_id=cert.course_id,
        certificate_code=cert.certificate_code,
        score=cert.score,
        total=20,  # matches FINAL_QUIZ_TOTAL; certificates are always out of 20
        percentage=cert.percentage,
        issued_at=cert.issued_at,
        course_title=course_title,
        username=username,
    )


@router.post("/{course_id}/certificate", response_model=schemas.CertificateOut)
def issue_certificate(
    course_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Issue a certificate for this course, OR return the existing one if
    already issued. Gated on is_course_completed() — 403 if the course isn't
    done yet (every lesson complete AND final quiz passed). The unique
    (user, course) index on the certificates table guarantees no duplicates:
    we find-first, and only create a new row if none exists."""
    course = _get_owned_course(course_id, user, db)
    if not is_course_completed(user.id, course.id, db):
        raise HTTPException(
            status_code=403,
            detail="Complete all lessons and pass the final quiz to earn the certificate.",
        )

    existing = (
        db.query(models.Certificate)
        .filter(
            models.Certificate.user_id == user.id,
            models.Certificate.course_id == course.id,
        )
        .first()
    )
    if existing:
        return _serialize_certificate(existing, course=course, user=user)

    # Snapshot the best passing final-quiz score at issue time so the
    # certificate always shows the score that earned it, even if the user
    # later retakes and scores lower.
    best_score = _best_passing_final_quiz_score(user.id, course.id, db) or 0
    total = FINAL_QUIZ_TOTAL
    pct = round(100.0 * best_score / total, 1) if total else 0.0

    # Generate a unique code with a DB-collision retry loop (extremely rare
    # but the index on certificate_code is unique, so we must handle it).
    import secrets
    for _ in range(8):
        code = "SYN-" + "".join(secrets.choice(_CERT_CODE_ALPHABET) for _ in range(6))
        clash = (
            db.query(models.Certificate.id)
            .filter(models.Certificate.certificate_code == code)
            .first()
        )
        if not clash:
            break
    else:
        # Should never happen; fall back to a longer code.
        code = "SYN-" + "".join(secrets.choice(_CERT_CODE_ALPHABET) for _ in range(10))

    cert = models.Certificate(
        user_id=user.id,
        course_id=course.id,
        certificate_code=code,
        score=best_score,
        percentage=pct,
    )
    db.add(cert)
    db.commit()
    db.refresh(cert)
    return _serialize_certificate(cert, course=course, user=user)


@router.get("/{course_id}/certificate", response_model=schemas.CertificateOut)
def get_certificate(
    course_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Fetch the certificate for this course, if one has been issued.
    404 if none yet (the user hasn't requested one, or hasn't completed the
    course). Use POST /certificate to issue one."""
    course = _get_owned_course(course_id, user, db)
    cert = (
        db.query(models.Certificate)
        .filter(
            models.Certificate.user_id == user.id,
            models.Certificate.course_id == course.id,
        )
        .first()
    )
    if not cert:
        raise HTTPException(status_code=404, detail="No certificate issued for this course yet.")
    return _serialize_certificate(cert, course=course, user=user)


@certificates_router.get("", response_model=List[schemas.CertificateOut])
def list_certificates(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """List all of the user's earned certificates, newest first. Each entry
    includes the joined course_title + username so the certificates page can
    render without N+1 queries."""
    certs = (
        db.query(models.Certificate)
        .filter(models.Certificate.user_id == user.id)
        .order_by(models.Certificate.issued_at.desc())
        .all()
    )
    if not certs:
        return []
    # Bulk-fetch the related courses + the user to avoid N+1
    course_ids = {c.course_id for c in certs}
    courses = (
        db.query(models.Course)
        .filter(models.Course.id.in_(course_ids))
        .all()
    )
    course_by_id = {c.id: c for c in courses}
    out = []
    for cert in certs:
        course = course_by_id.get(cert.course_id)
        out.append(_serialize_certificate(cert, course=course, user=user))
    return out
