from datetime import datetime
from typing import Any, List, Optional

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


class ModuleOut(BaseModel):
    title: str
    notes: str = ""
    videoQuery: str = ""
    videoId: str = ""
    blogQuery: str = ""
    blogUrl: str = ""
    completed: bool = False
    quiz: Optional[Any] = None


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
