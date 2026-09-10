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
        out.append(
            schemas.CourseSummaryOut(
                id=c.id,
                title=c.title,
                description=c.description or "",
                lesson_count=len(mods),
                completed_count=sum(1 for m in mods if m.get("completed")),
                created_at=c.created_at,
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
