from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import ai, models, schemas
from ..database import get_db
from .auth import get_current_user

router = APIRouter(prefix="/api/courses", tags=["courses"])


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
def update_module(
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
        mod["videoQuery"] = payload.videoQuery.strip()
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
    course.modules = modules
    db.commit()
    db.refresh(course)
    return course


@router.post("/{course_id}/modules/{index}/regenerate", response_model=schemas.CourseOut)
async def regenerate_module(
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
        fresh = await ai.generate_module(course.title, modules[index]["title"])
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

    mod = dict(modules[index])
    mod["completed"] = payload.completed
    modules[index] = mod
    course.modules = modules
    db.commit()
    db.refresh(course)
    return course


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
    modules[index] = mod
    course.modules = modules
    db.commit()
    db.refresh(course)
    return course
