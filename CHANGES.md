# Synapse — Engagement & Interactivity Features: Change Log

All 15 features from the brief, implemented one at a time in the requested
order. Each feature is captured in its own git commit(s) in the delivered
repository (`git log` / `git show <hash>` reproduces every diff below). Hard
constraints honored throughout:

- **Nothing existing was renamed, restructured, or removed** — auth, course
  CRUD, lesson generation/editing/reordering, completion toggling and the
  existing quiz flow behave exactly as before (asserted by per-feature
  response-shape tests; 188 automated checks pass in total).
- Every new DB column is nullable or has a default; a best-effort, additive
  runtime migration (`app/database.py::ensure_schema_upgrades`) upgrades
  existing databases on boot, and the shipped `synapse.db` was deliberately
  left un-migrated to prove it (boot log: `[schema] added missing column
  users.xp … courses.share_id`, legacy user intact).
- New AI functions reuse the existing `_call_gemini` / `responseSchema` /
  `_clean_json` / `AIError` pattern in `app/ai.py` — no second calling style.
- Every new frontend section degrades gracefully when its data is missing
  (`module.quiz_attempts` / `flashcards` / `diagram` absent -> section
  simply doesn't render).

## Test results

`13 suites / 188 checks / 0 failures` — the runnable suites are delivered
alongside this archive in the `synapse-engagement-tests/` folder; each runs
against a throwaway SQLite DB with the Gemini layer mocked, so no API key
is needed (`python test_f1_xp.py` etc.).

---

## Feature 1 — XP & Levels

**Commits:** `F1: XP & Levels — User.xp, +10 per completed lesson, quiz submit endpoint (+5/correct), xp+level on /me, header XP bar`

**Files changed/created:**

- `app/models.py`
- `app/database.py`
- `app/main.py`
- `app/schemas.py`
- `app/routers/auth.py`
- `app/routers/courses.py`
- `index.html`

**How it integrates / no-break confirmation:**

Adds a nullable-default `xp` column to `User` and awards XP as a pure side effect inside the existing mark-complete endpoint (+10, only on the not-done -> done transition so un-completing never farms XP). The quiz was previously graded entirely client-side (the frontend compares answers against `correctId`), so to award +5 XP per correct answer server-side a small additive endpoint `POST /api/courses/{id}/modules/{i}/quiz/submit` was added, which the existing quiz UI calls once every question is answered. `/api/auth/me` now additionally returns `xp` and a derived `level` (`xp // 100`, computed on read, never stored) while `id`/`username` serialization is byte-for-byte unchanged. No existing endpoint's request/response shape was altered — verified by asserting the exact response key sets in test_f1_xp.py. A best-effort runtime column migration (`database.ensure_schema_upgrades`, additive ALTER TABLE only) was introduced and runs before `create_all()` so pre-existing databases are upgraded on boot.

**New env vars / dependencies / migration steps:** New column: `users.xp` (auto-migrated). No new env vars or deps.

<details>
<summary>Full diff — F1: XP & Levels — User.xp, +10 per completed lesson, quiz submit endpoint (+5/correct), xp+level on /me, header XP bar</summary>

```diff
commit 75e55b45c8ab6e0ca217708e6655c956cb6d7ca4
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:40:57 2026 +0000

    F1: XP & Levels — User.xp, +10 per completed lesson, quiz submit endpoint (+5/correct), xp+level on /me, header XP bar

diff --git a/app/database.py b/app/database.py
index ec66f0f..b86df80 100644
--- a/app/database.py
+++ b/app/database.py
@@ -1,6 +1,6 @@
 import os
 
-from sqlalchemy import create_engine
+from sqlalchemy import create_engine, inspect, text
 from sqlalchemy.orm import declarative_base, sessionmaker
 
 # Local dev defaults to a SQLite file. In production (e.g. on Vercel), Vercel's
@@ -21,3 +21,49 @@ def get_db():
         yield db
     finally:
         db.close()
+
+
+# ---------------------------------------------------------------------------
+# Best-effort runtime column migration.
+#
+# Base.metadata.create_all() only creates TABLES that are missing entirely —
+# it never adds new columns to tables that already exist. So a synapse.db
+# created by an older version of the app would make every query that touches
+# a new column fail with "no such column". This helper inspects the live
+# schema on startup and issues a plain ALTER TABLE ... ADD COLUMN for any
+# model column that is missing. It is additive-only (nothing is renamed,
+# re-typed, or dropped), idempotent (skips columns that already exist), and
+# best-effort (a failure logs and moves on rather than crashing the app), so
+# it is safe to run on every startup against both SQLite and Postgres.
+# New engagement features append their columns to this list as they are
+# introduced, keeping each feature's schema change self-contained.
+# ---------------------------------------------------------------------------
+_COLUMN_MIGRATIONS = [
+    # (table, column_name, ADD COLUMN type/defaults clause)
+    ("users", "xp", "INTEGER NOT NULL DEFAULT 0"),
+]
+
+
+def ensure_schema_upgrades() -> None:
+    try:
+        inspector = inspect(engine)
+        existing_tables = set(inspector.get_table_names())
+        for table, column, ddl in _COLUMN_MIGRATIONS:
+            if table not in existing_tables:
+                continue  # table will be created fresh by create_all()
+            existing_cols = {c["name"] for c in inspector.get_columns(table)}
+            if column in existing_cols:
+                continue
+            stmt = f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
+            try:
+                with engine.begin() as conn:
+                    conn.execute(text(stmt))
+                print(f"[schema] added missing column {table}.{column}")
+            except Exception as e:
+                # Best effort: if this fails (e.g. permissions on a managed
+                # DB), the operator should run the SQL by hand — see
+                # CHANGES.md for the manual migration statements.
+                print(f"[schema] could not add {table}.{column} ({e}); "
+                      f"run manually: {stmt}")
+    except Exception as e:
+        print(f"[schema] runtime column migration skipped: {e}")
diff --git a/app/main.py b/app/main.py
index 1e651a6..5fe767c 100644
--- a/app/main.py
+++ b/app/main.py
@@ -5,13 +5,17 @@ from fastapi.middleware.cors import CORSMiddleware
 from fastapi.responses import FileResponse
 
 from . import models  # noqa: F401 (ensures models are registered before create_all)
-from .database import Base, engine
+from .database import Base, engine, ensure_schema_upgrades
 from .routers import auth, courses
 
 # Vercel's docs specifically recommend basing file paths on the working
 # directory (the project root) rather than __file__ for the Python runtime.
 FRONTEND_INDEX = os.path.join(os.getcwd(), "index.html")
 
+# Upgrades pre-existing databases with any newly added columns (additive
+# ALTERs only) before create_all() — which by itself only creates missing
+# tables, not missing columns.
+ensure_schema_upgrades()
 Base.metadata.create_all(bind=engine)
 
 app = FastAPI(title="Synapse API")
diff --git a/app/models.py b/app/models.py
index 2dab66c..8f08291 100644
--- a/app/models.py
+++ b/app/models.py
@@ -12,6 +12,10 @@ class User(Base):
     username = Column(String(64), unique=True, index=True, nullable=False)
     password_hash = Column(String(255), nullable=False)
     created_at = Column(DateTime(timezone=True), server_default=func.now())
+    # Engagement stats (added incrementally; nullable-with-default so rows
+    # created before these columns existed keep working — see
+    # database.ensure_schema_upgrades for the runtime column migration).
+    xp = Column(Integer, default=0, nullable=False)
 
     courses = relationship("Course", back_populates="owner", cascade="all, delete-orphan")
 
diff --git a/app/routers/auth.py b/app/routers/auth.py
index 4114d0f..123f262 100644
--- a/app/routers/auth.py
+++ b/app/routers/auth.py
@@ -59,4 +59,12 @@ def login(payload: schemas.LoginIn, db: Session = Depends(get_db)):
 
 @router.get("/me", response_model=schemas.UserOut)
 def me(current_user: models.User = Depends(get_current_user)):
-    return current_user
+    # id/username serialization is unchanged; xp/level are additive fields.
+    # Level is derived on read (xp // 100) rather than stored, per spec.
+    xp = current_user.xp or 0
+    return schemas.UserOut(
+        id=current_user.id,
+        username=current_user.username,
+        xp=xp,
+        level=xp // 100,
+    )
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 07f1dd7..28e1a8a 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -21,6 +21,12 @@ async def generate_course(topic: str) -> dict:
 
 router = APIRouter(prefix="/api/courses", tags=["courses"])
 
+# XP awards for engagement actions (spec: +10 per completed lesson, +5 per
+# correct quiz answer). Pure side effects — they never alter any existing
+# endpoint's response shape.
+XP_PER_COMPLETED_LESSON = 10
+XP_PER_CORRECT_ANSWER = 5
+
 
 def _get_owned_course(course_id: int, user: models.User, db: Session) -> models.Course:
     course = (
@@ -247,15 +253,59 @@ def toggle_complete(
     if index < 0 or index >= len(modules):
         raise HTTPException(status_code=404, detail="Lesson not found.")
 
+    was_completed = bool(modules[index].get("completed", False))
     mod = dict(modules[index])
     mod["completed"] = payload.completed
     modules[index] = mod
     course.modules = modules
+
+    # Engagement side effect: award XP only on the not-done -> done transition
+    # (un-completing never awards, and re-completing after un-completing
+    # doesn't double-award within a single toggle). Committed by the existing
+    # db.commit() below; the response body is unchanged.
+    if payload.completed and not was_completed:
+        user.xp = (user.xp or 0) + XP_PER_COMPLETED_LESSON
+
     db.commit()
     db.refresh(course)
     return course
 
 
+@router.post("/{course_id}/modules/{index}/quiz/submit", response_model=schemas.QuizSubmitOut)
+def submit_quiz(
+    course_id: int,
+    index: int,
+    payload: schemas.QuizSubmitIn,
+    db: Session = Depends(get_db),
+    user: models.User = Depends(get_current_user),
+):
+    """Reports the outcome of a quiz the frontend graded client-side and
+    awards XP (+5 per correct answer). New endpoint — the existing quiz
+    *generation* endpoint is untouched. Later features (streaks, badges,
+    attempt history) hook into this same action point."""
+    if payload.score > payload.total:
+        raise HTTPException(status_code=400, detail="Score cannot exceed total.")
+
+    course = _get_owned_course(course_id, user, db)
+    modules = list(course.modules or [])
+    if index < 0 or index >= len(modules):
+        raise HTTPException(status_code=404, detail="Lesson not found.")
+
+    xp_awarded = XP_PER_CORRECT_ANSWER * payload.score
+    user.xp = (user.xp or 0) + xp_awarded
+    db.commit()
+    db.refresh(user)
+    db.refresh(course)
+
+    xp = user.xp or 0
+    return schemas.QuizSubmitOut(
+        xp_awarded=xp_awarded,
+        xp=xp,
+        level=xp // 100,
+        course=course,
+    )
+
+
 @router.post("/{course_id}/modules/{index}/quiz", response_model=schemas.CourseOut)
 async def generate_quiz_route(
     course_id: int,
diff --git a/app/schemas.py b/app/schemas.py
index 2914c1e..c828689 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -24,6 +24,11 @@ class UserOut(BaseModel):
     model_config = ConfigDict(from_attributes=True)
     id: int
     username: str
+    # Engagement stats — additive fields; level is computed on read
+    # (xp // 100), never stored. Defaults keep older clients / rows without
+    # the columns working unchanged.
+    xp: int = 0
+    level: int = 0
 
 
 class ModuleOut(BaseModel):
@@ -65,6 +70,13 @@ class ReorderIn(BaseModel):
     direction: str  # "up" | "down"
 
 
+class QuizSubmitIn(BaseModel):
+    # Graded client-side today (correctId is exposed to the browser), so the
+    # frontend reports the outcome; validated so score can never exceed total.
+    score: int = Field(ge=0)
+    total: int = Field(ge=1)
+
+
 class CourseOut(BaseModel):
     model_config = ConfigDict(from_attributes=True)
     id: int
@@ -81,3 +93,10 @@ class CourseSummaryOut(BaseModel):
     lesson_count: int
     completed_count: int
     created_at: datetime
+
+
+class QuizSubmitOut(BaseModel):
+    xp_awarded: int
+    xp: int
+    level: int
+    course: CourseOut
diff --git a/index.html b/index.html
index 97b331f..008669d 100644
--- a/index.html
+++ b/index.html
@@ -253,6 +253,17 @@
   .quiz-explain{margin-top:10px;font-size:13px;color:var(--muted);line-height:1.5;}
   .quiz-score{font-weight:600;margin-bottom:14px;}
 
+  /* ===== Engagement: header XP / level ===== */
+  .xp-chip{display:flex;align-items:center;gap:8px;}
+  .level-badge{
+    font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;color:#221607;
+    background:var(--gold);border-radius:6px;padding:3px 7px;white-space:nowrap;
+  }
+  .xp-bar{width:84px;height:6px;background:var(--line);border-radius:4px;overflow:hidden;}
+  .xp-bar-fill{height:100%;width:0%;background:var(--gold);transition:width .35s ease;}
+  .xp-text{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);white-space:nowrap;}
+  @media(max-width:860px){.xp-bar,.xp-text{display:none;}}
+
   @media print{
     body *{visibility:hidden;}
     #print-area, #print-area *{visibility:visible;}
@@ -271,6 +282,11 @@
     <div class="brand"><span class="dot"></span>Synapse</div>
     <div class="topbar-right">
       <button class="link-btn" id="nav-dashboard">Dashboard</button>
+      <div class="xp-chip" id="xp-chip" title="Experience points">
+        <span class="level-badge" id="level-badge">L0</span>
+        <div class="xp-bar"><div class="xp-bar-fill" id="xp-bar-fill"></div></div>
+        <span class="xp-text" id="xp-text">0 XP</span>
+      </div>
       <div class="user-chip">
         <div class="avatar" id="avatar-init">?</div>
         <span id="user-name" class="mono" style="font-size:13px;color:var(--muted);"></span>
@@ -425,6 +441,7 @@ let authMode = 'signin';
 let courseEditing = false;
 let moduleEditing = false;
 let addingLesson = false;
+let meStats = { xp:0, level:0 };   // engagement stats from /api/auth/me
 
 const el = (id) => document.getElementById(id);
 
@@ -504,11 +521,29 @@ async function enterApp(){
   el('user-name').textContent = currentUser;
   el('avatar-init').textContent = currentUser.slice(0,1).toUpperCase();
   el('topbar').classList.remove('hidden');
+  refreshMe();          // engagement: header XP bar / level badge
   await loadCourses();
   renderCourseGrid();
   showView('dashboard');
 }
 
+/* ---------------- Engagement stats (XP / level) ---------------- */
+function updateHeaderStats(){
+  const xp = meStats.xp || 0;
+  const lvl = meStats.level || 0;
+  el('level-badge').textContent = 'L' + lvl;
+  el('xp-bar-fill').style.width = (xp % 100) + '%';
+  el('xp-text').textContent = xp + ' XP';
+}
+
+async function refreshMe(){
+  try{
+    const me = await api('/auth/me');
+    meStats = { xp: me.xp || 0, level: me.level || 0 };
+    updateHeaderStats();
+  }catch(e){ /* header stats are optional chrome — never break the app */ }
+}
+
 async function loadCourses(){
   try{
     coursesSummary = await api('/courses');
@@ -826,6 +861,7 @@ function renderModuleMain(course){
         method:'PATCH', body:{ completed: !module.completed }
       });
       renderCourseView();
+      refreshMe();   // XP was awarded server-side — update the header bar
     }catch(err){ alert('Could not update completion: ' + err.message); }
   });
   el('edit-mod-btn').addEventListener('click', ()=>{ moduleEditing=true; renderCourseView(); });
@@ -873,6 +909,7 @@ function renderQuiz(course, module){
 
 function renderQuizQuestions(course, module, container){
   const quiz = module.quiz;
+  let quizSubmitted = false;   // engagement: report the attempt once, when every question is answered
   let html = `<div class="quiz-score" id="quiz-score-line"></div>`;
   quiz.questions.forEach((q,qi)=>{
     html += `<div class="quiz-q" id="quiz-q-${qi}">
@@ -912,6 +949,11 @@ function renderQuizQuestions(course, module, container){
       explainDiv.textContent = (isCorrect ? 'Correct. ' : 'Not quite. ') + (q.explanation||'');
       explainDiv.classList.remove('hidden');
       updateScore();
+      const allAnswered = quiz.questions.every((qq,qi2)=>document.querySelector(`input[name="quiz-${qi2}"]:checked`));
+      if(!quizSubmitted && allAnswered){
+        quizSubmitted = true;
+        submitQuizAttempt(course, quiz);
+      }
     });
   });
 
@@ -927,6 +969,25 @@ function renderQuizQuestions(course, module, container){
   });
 }
 
+/* ---- Engagement: report a finished quiz to the server (XP award) ---- */
+async function submitQuizAttempt(course, quiz){
+  try{
+    const selections = quiz.questions.map((q,qi)=>{
+      const r = document.querySelector(`input[name="quiz-${qi}"]:checked`);
+      return r ? r.value : null;
+    });
+    const score = quiz.questions.filter((q,qi)=>selections[qi]===q.correctId).length;
+    const resp = await api(`/courses/${course.id}/modules/${activeModuleIndex}/quiz/submit`, {
+      method:'POST', body:{ score, total: quiz.questions.length }
+    });
+    meStats = { xp: resp.xp || 0, level: resp.level || 0 };
+    updateHeaderStats();
+    // The response carries the authoritative course state; later features
+    // (attempt history) read it from here.
+    if(resp.course) activeCourse = resp.course;
+  }catch(e){ /* engagement is best-effort — never break the quiz UI */ }
+}
+
 /* ---- Course header edit ---- */
 el('toggle-course-edit').addEventListener('click', ()=>{
   const course = currentCourse();
```

</details>

---

## Feature 2 — Streaks

**Commits:** `F2: Streaks — streak_count/last_active_date columns, touch_streak on completion+quiz submit, flame chip in header`

**Files changed/created:**

- `app/models.py`
- `app/database.py`
- `app/achievements.py (new)`
- `app/schemas.py`
- `app/routers/auth.py`
- `app/routers/courses.py`
- `index.html`

**How it integrates / no-break confirmation:**

Adds `streak_count` (default 0) and nullable `last_active_date` to `User`. The shared rule lives in the new `app/achievements.py` module: `touch_streak()` increments when the last active day was yesterday, resets to 1 when older than yesterday, and is a no-op when already active today (UTC day boundary, matching how the rest of the app records timestamps). It is called from the lesson-completion endpoint (any completion action, never un-completions) and the quiz-submit endpoint. `/me` and the quiz-submit response expose `streak_count` additively. The completion endpoint's response shape is untouched — the streak update rides the existing `db.commit()`.

**New env vars / dependencies / migration steps:** New columns: `users.streak_count`, `users.last_active_date` (auto-migrated). No new env vars or deps.

<details>
<summary>Full diff — F2: Streaks — streak_count/last_active_date columns, touch_streak on completion+quiz submit, flame chip in header</summary>

```diff
commit 519082bc99ccb0c90607fa38c22f660e126d1cb4
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:42:48 2026 +0000

    F2: Streaks — streak_count/last_active_date columns, touch_streak on completion+quiz submit, flame chip in header

diff --git a/app/achievements.py b/app/achievements.py
new file mode 100644
index 0000000..d1d0ff0
--- /dev/null
+++ b/app/achievements.py
@@ -0,0 +1,42 @@
+"""Engagement helpers for Synapse.
+
+Currently holds the streak-update rule shared by every engagement action
+(lesson completion, quiz submission). Feature 3 (badges) extends this module
+with the badge catalog and evaluation logic.
+
+All helpers only mutate the ORM objects passed in — the calling endpoint's
+existing db.commit() persists them, so response shapes never change.
+"""
+
+from datetime import date, datetime, timedelta
+
+
+def touch_streak(user) -> None:
+    """Update a user's daily streak for 'today' (UTC).
+
+    Rules (per spec):
+      - last active date is today        -> streak unchanged
+      - last active date was yesterday   -> streak += 1
+      - older than yesterday (or never)  -> streak resets to 1
+
+    Also stamps last_active_date to now. Safe to call repeatedly within the
+    same day; the day boundary is UTC, which matches how `created_at` and the
+    quiz-attempt dates are recorded.
+    """
+    today = datetime.utcnow().date()
+    last = user.last_active_date
+
+    last_day: date | None = None
+    if last is not None:
+        # The column is a DateTime, but drivers can hand back a plain date;
+        # normalize so the comparison below always sees dates.
+        last_day = last.date() if isinstance(last, datetime) else last
+
+    if last_day == today:
+        return  # already active today — leave the streak alone
+    if last_day == today - timedelta(days=1):
+        user.streak_count = (user.streak_count or 0) + 1
+    else:
+        user.streak_count = 1
+
+    user.last_active_date = datetime.utcnow()
diff --git a/app/database.py b/app/database.py
index b86df80..aa0bf04 100644
--- a/app/database.py
+++ b/app/database.py
@@ -41,6 +41,8 @@ def get_db():
 _COLUMN_MIGRATIONS = [
     # (table, column_name, ADD COLUMN type/defaults clause)
     ("users", "xp", "INTEGER NOT NULL DEFAULT 0"),
+    ("users", "streak_count", "INTEGER NOT NULL DEFAULT 0"),
+    ("users", "last_active_date", "TIMESTAMP"),
 ]
 
 
diff --git a/app/models.py b/app/models.py
index 8f08291..828df55 100644
--- a/app/models.py
+++ b/app/models.py
@@ -16,6 +16,11 @@ class User(Base):
     # created before these columns existed keep working — see
     # database.ensure_schema_upgrades for the runtime column migration).
     xp = Column(Integer, default=0, nullable=False)
+    # Streak tracking: streak_count is the current consecutive-day run;
+    # last_active_date is the UTC datetime of the last engagement action
+    # (lesson completion / quiz submission) used to decide increment vs reset.
+    streak_count = Column(Integer, default=0, nullable=False)
+    last_active_date = Column(DateTime, nullable=True)
 
     courses = relationship("Course", back_populates="owner", cascade="all, delete-orphan")
 
diff --git a/app/routers/auth.py b/app/routers/auth.py
index 123f262..179780d 100644
--- a/app/routers/auth.py
+++ b/app/routers/auth.py
@@ -59,12 +59,14 @@ def login(payload: schemas.LoginIn, db: Session = Depends(get_db)):
 
 @router.get("/me", response_model=schemas.UserOut)
 def me(current_user: models.User = Depends(get_current_user)):
-    # id/username serialization is unchanged; xp/level are additive fields.
-    # Level is derived on read (xp // 100) rather than stored, per spec.
+    # id/username serialization is unchanged; xp/level/streak_count are
+    # additive fields. Level is derived on read (xp // 100) rather than
+    # stored, per spec.
     xp = current_user.xp or 0
     return schemas.UserOut(
         id=current_user.id,
         username=current_user.username,
         xp=xp,
         level=xp // 100,
+        streak_count=current_user.streak_count or 0,
     )
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 28e1a8a..6be7fd6 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -3,7 +3,7 @@ from typing import List
 from fastapi import APIRouter, Depends, HTTPException
 from sqlalchemy.orm import Session
 
-from .. import ai, models, schemas
+from .. import achievements, ai, models, schemas
 from ..database import get_db
 from .auth import get_current_user
 async def generate_course(topic: str) -> dict:
@@ -265,6 +265,9 @@ def toggle_complete(
     # db.commit() below; the response body is unchanged.
     if payload.completed and not was_completed:
         user.xp = (user.xp or 0) + XP_PER_COMPLETED_LESSON
+    # Streaks count any lesson-completion action (not un-completions).
+    if payload.completed:
+        achievements.touch_streak(user)
 
     db.commit()
     db.refresh(course)
@@ -293,6 +296,7 @@ def submit_quiz(
 
     xp_awarded = XP_PER_CORRECT_ANSWER * payload.score
     user.xp = (user.xp or 0) + xp_awarded
+    achievements.touch_streak(user)
     db.commit()
     db.refresh(user)
     db.refresh(course)
@@ -302,6 +306,7 @@ def submit_quiz(
         xp_awarded=xp_awarded,
         xp=xp,
         level=xp // 100,
+        streak_count=user.streak_count or 0,
         course=course,
     )
 
diff --git a/app/schemas.py b/app/schemas.py
index c828689..dcbe0f8 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -29,6 +29,7 @@ class UserOut(BaseModel):
     # the columns working unchanged.
     xp: int = 0
     level: int = 0
+    streak_count: int = 0
 
 
 class ModuleOut(BaseModel):
@@ -99,4 +100,5 @@ class QuizSubmitOut(BaseModel):
     xp_awarded: int
     xp: int
     level: int
+    streak_count: int = 0
     course: CourseOut
diff --git a/index.html b/index.html
index 008669d..d738eca 100644
--- a/index.html
+++ b/index.html
@@ -262,6 +262,12 @@
   .xp-bar{width:84px;height:6px;background:var(--line);border-radius:4px;overflow:hidden;}
   .xp-bar-fill{height:100%;width:0%;background:var(--gold);transition:width .35s ease;}
   .xp-text{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);white-space:nowrap;}
+  .streak-chip{
+    display:flex;align-items:center;gap:4px;font-family:'IBM Plex Mono',monospace;font-size:12.5px;
+    color:var(--muted);white-space:nowrap;
+  }
+  .streak-chip .flame{font-size:14px;line-height:1;}
+  .streak-chip.warm{color:var(--gold-2);}
   @media(max-width:860px){.xp-bar,.xp-text{display:none;}}
 
   @media print{
@@ -282,6 +288,9 @@
     <div class="brand"><span class="dot"></span>Synapse</div>
     <div class="topbar-right">
       <button class="link-btn" id="nav-dashboard">Dashboard</button>
+      <div class="streak-chip" id="streak-chip" title="Daily learning streak">
+        <span class="flame">🔥</span><span id="streak-count">0</span>
+      </div>
       <div class="xp-chip" id="xp-chip" title="Experience points">
         <span class="level-badge" id="level-badge">L0</span>
         <div class="xp-bar"><div class="xp-bar-fill" id="xp-bar-fill"></div></div>
@@ -441,7 +450,7 @@ let authMode = 'signin';
 let courseEditing = false;
 let moduleEditing = false;
 let addingLesson = false;
-let meStats = { xp:0, level:0 };   // engagement stats from /api/auth/me
+let meStats = { xp:0, level:0, streak_count:0 };   // engagement stats from /api/auth/me
 
 const el = (id) => document.getElementById(id);
 
@@ -531,15 +540,18 @@ async function enterApp(){
 function updateHeaderStats(){
   const xp = meStats.xp || 0;
   const lvl = meStats.level || 0;
+  const streak = meStats.streak_count || 0;
   el('level-badge').textContent = 'L' + lvl;
   el('xp-bar-fill').style.width = (xp % 100) + '%';
   el('xp-text').textContent = xp + ' XP';
+  el('streak-count').textContent = streak;
+  el('streak-chip').classList.toggle('warm', streak > 0);
 }
 
 async function refreshMe(){
   try{
     const me = await api('/auth/me');
-    meStats = { xp: me.xp || 0, level: me.level || 0 };
+    meStats = { xp: me.xp || 0, level: me.level || 0, streak_count: me.streak_count || 0 };
     updateHeaderStats();
   }catch(e){ /* header stats are optional chrome — never break the app */ }
 }
@@ -980,7 +992,7 @@ async function submitQuizAttempt(course, quiz){
     const resp = await api(`/courses/${course.id}/modules/${activeModuleIndex}/quiz/submit`, {
       method:'POST', body:{ score, total: quiz.questions.length }
     });
-    meStats = { xp: resp.xp || 0, level: resp.level || 0 };
+    meStats = { xp: resp.xp || 0, level: resp.level || 0, streak_count: resp.streak_count || 0 };
     updateHeaderStats();
     // The response carries the authoritative course state; later features
     // (attempt history) read it from here.
```

</details>

---

## Feature 3 — Badges / Achievements

**Commits:** `F3: Badges — User.badges JSON column, app/achievements.py catalog+evaluate, badges on /me and quiz submit, dashboard badges grid + toasts`

**Files changed/created:**

- `app/models.py`
- `app/database.py`
- `app/achievements.py`
- `app/schemas.py`
- `app/routers/auth.py`
- `app/routers/courses.py`
- `index.html`

**How it integrates / no-break confirmation:**

Adds `badges` (JSON list of badge-id strings, default []) to `User` and expands `app/achievements.py` with a fixed 8-badge catalog — including the three named in the spec (`first_course_completed`, `five_quizzes_passed`, `seven_day_streak`) plus `first_lesson`, `first_quiz`, `xp_500`, `flashcard_fan`, `note_taker`. `evaluate(user, db)` recomputes every condition from current DB state after each engagement action and appends newly earned ids (reassigning the list so SQLAlchemy's JSON change detection fires). The completion endpoint gains only this side-effect call; the quiz-submit response gains additive `badges`/`new_badges` fields. `/me` also returns `badges`. The dashboard renders an Achievements grid (earned vs. locked, gracefully ignoring unknown ids) and new badges pop a toast; the quiz-attempt conditions read `module.quiz_attempts`, which Feature 4 records — the logic was verified in test_f3 via injected attempts and re-verified through the real flow in test_f4.

**New env vars / dependencies / migration steps:** New column: `users.badges` (auto-migrated). No new env vars or deps.

<details>
<summary>Full diff — F3: Badges — User.badges JSON column, app/achievements.py catalog+evaluate, badges on /me and quiz submit, dashboard badges grid + toasts</summary>

```diff
commit ca2650c925ced72f7c9676fcfdf8187246277c46
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:45:32 2026 +0000

    F3: Badges — User.badges JSON column, app/achievements.py catalog+evaluate, badges on /me and quiz submit, dashboard badges grid + toasts

diff --git a/app/achievements.py b/app/achievements.py
index d1d0ff0..10868b3 100644
--- a/app/achievements.py
+++ b/app/achievements.py
@@ -1,8 +1,11 @@
 """Engagement helpers for Synapse.
 
-Currently holds the streak-update rule shared by every engagement action
-(lesson completion, quiz submission). Feature 3 (badges) extends this module
-with the badge catalog and evaluation logic.
+Holds:
+  - touch_streak(): the daily-streak rule shared by every engagement action
+    (lesson completion, quiz submission).
+  - BADGES + evaluate(): a small fixed badge catalog and a checker that runs
+    after engagement actions and appends newly earned badge ids to
+    User.badges.
 
 All helpers only mutate the ORM objects passed in — the calling endpoint's
 existing db.commit() persists them, so response shapes never change.
@@ -10,6 +13,60 @@ existing db.commit() persists them, so response shapes never change.
 
 from datetime import date, datetime, timedelta
 
+from sqlalchemy.orm import Session
+
+from . import models
+
+
+# ---------------------------------------------------------------------------
+# Badge catalog — the single source of truth for ids/conditions. The frontend
+# keeps a matching catalog for display; unknown ids stored in older rows are
+# simply ignored, and missing badges default to locked, so partial data never
+# breaks rendering.
+# ---------------------------------------------------------------------------
+BADGES = {
+    "first_lesson": {
+        "name": "First Steps",
+        "icon": "🥾",
+        "description": "Complete your first lesson.",
+    },
+    "first_course_completed": {
+        "name": "Course Conqueror",
+        "icon": "🏆",
+        "description": "Complete every lesson in a course.",
+    },
+    "first_quiz": {
+        "name": "Quiz Rookie",
+        "icon": "❓",
+        "description": "Finish your first quiz attempt.",
+    },
+    "five_quizzes_passed": {
+        "name": "Quiz Master",
+        "icon": "🎯",
+        "description": "Pass five quizzes (score at least two-thirds).",
+    },
+    "seven_day_streak": {
+        "name": "On Fire",
+        "icon": "🔥",
+        "description": "Keep a seven-day learning streak.",
+    },
+    "xp_500": {
+        "name": "Scholar",
+        "icon": "⭐",
+        "description": "Earn 500 XP.",
+    },
+    "flashcard_fan": {
+        "name": "Flashcard Fan",
+        "icon": "🃏",
+        "description": "Generate flashcards for a lesson.",
+    },
+    "note_taker": {
+        "name": "Note Taker",
+        "icon": "📝",
+        "description": "Write your first personal lesson note.",
+    },
+}
+
 
 def touch_streak(user) -> None:
     """Update a user's daily streak for 'today' (UTC).
@@ -40,3 +97,56 @@ def touch_streak(user) -> None:
         user.streak_count = 1
 
     user.last_active_date = datetime.utcnow()
+
+
+def evaluate(user, db: Session) -> list:
+    """Check every badge condition against the current DB state and append
+    any newly earned badge ids to user.badges.
+
+    Returns the list of *newly* earned ids (empty when nothing was earned) so
+    endpoints can surface them without changing their existing responses.
+    Call this AFTER the action's own state mutations (module updates, XP,
+    streak) but BEFORE the endpoint's db.commit().
+    """
+    earned = set(user.badges or [])
+
+    courses = (
+        db.query(models.Course)
+        .filter(models.Course.user_id == user.id)
+        .all()
+    )
+    all_modules = [m for c in courses for m in (c.modules or [])]
+    attempts = [a for m in all_modules for a in (m.get("quiz_attempts") or [])]
+
+    def passed(a) -> bool:
+        total = a.get("total") or 0
+        return bool(total) and (a.get("score") or 0) * 3 >= total * 2
+
+    course_completed = any(
+        (c.modules or []) and all(m.get("completed") for m in c.modules)
+        for c in courses
+    )
+
+    conditions = {
+        "first_lesson": any(m.get("completed") for m in all_modules),
+        "first_course_completed": course_completed,
+        "first_quiz": len(attempts) >= 1,
+        "five_quizzes_passed": sum(1 for a in attempts if passed(a)) >= 5,
+        "seven_day_streak": (user.streak_count or 0) >= 7,
+        "xp_500": (user.xp or 0) >= 500,
+        "flashcard_fan": any(m.get("flashcards") for m in all_modules),
+        # "note_taker" joins here in Feature 7 together with the Note table.
+    }
+
+    new_badges = [
+        badge_id
+        for badge_id, ok in conditions.items()
+        if ok and badge_id in BADGES and badge_id not in earned
+    ]
+
+    if new_badges:
+        # Reassign (never mutate in place) so SQLAlchemy's JSON change
+        # detection fires.
+        user.badges = sorted(earned | set(new_badges))
+
+    return new_badges
diff --git a/app/database.py b/app/database.py
index aa0bf04..a74f9da 100644
--- a/app/database.py
+++ b/app/database.py
@@ -43,6 +43,7 @@ _COLUMN_MIGRATIONS = [
     ("users", "xp", "INTEGER NOT NULL DEFAULT 0"),
     ("users", "streak_count", "INTEGER NOT NULL DEFAULT 0"),
     ("users", "last_active_date", "TIMESTAMP"),
+    ("users", "badges", "JSON DEFAULT '[]'"),
 ]
 
 
diff --git a/app/models.py b/app/models.py
index 828df55..85db93e 100644
--- a/app/models.py
+++ b/app/models.py
@@ -21,6 +21,9 @@ class User(Base):
     # (lesson completion / quiz submission) used to decide increment vs reset.
     streak_count = Column(Integer, default=0, nullable=False)
     last_active_date = Column(DateTime, nullable=True)
+    # Earned badge ids (strings) — a small fixed catalog lives in
+    # app/achievements.py; anything not in the catalog is ignored on render.
+    badges = Column(JSON, default=list, nullable=False)
 
     courses = relationship("Course", back_populates="owner", cascade="all, delete-orphan")
 
diff --git a/app/routers/auth.py b/app/routers/auth.py
index 179780d..324a6c0 100644
--- a/app/routers/auth.py
+++ b/app/routers/auth.py
@@ -69,4 +69,5 @@ def me(current_user: models.User = Depends(get_current_user)):
         xp=xp,
         level=xp // 100,
         streak_count=current_user.streak_count or 0,
+        badges=list(current_user.badges or []),
     )
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 6be7fd6..8e71a29 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -268,6 +268,8 @@ def toggle_complete(
     # Streaks count any lesson-completion action (not un-completions).
     if payload.completed:
         achievements.touch_streak(user)
+    # Badges: re-evaluate after the action's own state changes, before commit.
+    achievements.evaluate(user, db)
 
     db.commit()
     db.refresh(course)
@@ -297,6 +299,7 @@ def submit_quiz(
     xp_awarded = XP_PER_CORRECT_ANSWER * payload.score
     user.xp = (user.xp or 0) + xp_awarded
     achievements.touch_streak(user)
+    new_badges = achievements.evaluate(user, db)
     db.commit()
     db.refresh(user)
     db.refresh(course)
@@ -307,6 +310,8 @@ def submit_quiz(
         xp=xp,
         level=xp // 100,
         streak_count=user.streak_count or 0,
+        badges=list(user.badges or []),
+        new_badges=new_badges,
         course=course,
     )
 
diff --git a/app/schemas.py b/app/schemas.py
index dcbe0f8..a45725c 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -30,6 +30,7 @@ class UserOut(BaseModel):
     xp: int = 0
     level: int = 0
     streak_count: int = 0
+    badges: List[str] = []
 
 
 class ModuleOut(BaseModel):
@@ -101,4 +102,6 @@ class QuizSubmitOut(BaseModel):
     xp: int
     level: int
     streak_count: int = 0
+    badges: List[str] = []
+    new_badges: List[str] = []
     course: CourseOut
diff --git a/index.html b/index.html
index d738eca..10b0ec9 100644
--- a/index.html
+++ b/index.html
@@ -270,6 +270,27 @@
   .streak-chip.warm{color:var(--gold-2);}
   @media(max-width:860px){.xp-bar,.xp-text{display:none;}}
 
+  /* ===== Engagement: badges grid + toast ===== */
+  .badges-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px;margin-bottom:44px;}
+  .badge-card{
+    background:var(--paper-2);border:1px solid var(--line);border-radius:14px;padding:16px 18px;
+    display:flex;gap:12px;align-items:flex-start;transition:transform .15s ease;
+  }
+  .badge-card:hover{transform:translateY(-2px);}
+  .badge-icon{font-size:26px;line-height:1;filter:grayscale(1);opacity:.45;}
+  .badge-card.earned{border-color:var(--gold);background:linear-gradient(135deg,#FFFFFF 0%,#FBF3E6 100%);}
+  .badge-card.earned .badge-icon{filter:none;opacity:1;}
+  .badge-name{font-weight:600;font-size:14px;color:var(--ink);}
+  .badge-desc{font-size:12.5px;color:var(--muted);line-height:1.45;margin-top:3px;}
+  .badge-lock{font-size:11px;color:var(--muted);margin-left:auto;flex-shrink:0;}
+  .toast-wrap{position:fixed;right:18px;bottom:18px;z-index:80;display:flex;flex-direction:column;gap:8px;max-width:320px;}
+  .toast{
+    background:var(--ink);color:#fff;border-radius:12px;padding:12px 16px;font-size:13.5px;line-height:1.45;
+    box-shadow:var(--shadow);display:flex;gap:10px;align-items:center;animation:toastIn .25s ease;
+  }
+  .toast .t-icon{font-size:18px;}
+  @keyframes toastIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
+
   @media print{
     body *{visibility:hidden;}
     #print-area, #print-area *{visibility:visible;}
@@ -365,6 +386,12 @@
         <h4>No courses yet</h4>
         <p>Type a topic above and Synapse will put a full course together for you.</p>
       </div>
+
+      <div class="section-head">
+        <h3>Achievements</h3>
+        <span class="count-badge" id="badge-count">0 earned</span>
+      </div>
+      <div id="badges-grid" class="badges-grid"></div>
     </div>
   </section>
 
@@ -410,6 +437,8 @@
     </div>
   </div>
 
+  <div id="toast-wrap" class="toast-wrap"></div>
+
 </div>
 
 <script>
@@ -450,7 +479,20 @@ let authMode = 'signin';
 let courseEditing = false;
 let moduleEditing = false;
 let addingLesson = false;
-let meStats = { xp:0, level:0, streak_count:0 };   // engagement stats from /api/auth/me
+let meStats = { xp:0, level:0, streak_count:0, badges:[] };   // engagement stats from /api/auth/me
+
+/* Badge catalog — mirrors app/achievements.py. Unknown/missing data simply
+   renders as locked, so partial data never breaks the UI. */
+const BADGE_CATALOG = [
+  { id:'first_lesson',           icon:'🥾', name:'First Steps',      desc:'Complete your first lesson.' },
+  { id:'first_course_completed', icon:'🏆', name:'Course Conqueror', desc:'Complete every lesson in a course.' },
+  { id:'first_quiz',             icon:'❓', name:'Quiz Rookie',      desc:'Finish your first quiz attempt.' },
+  { id:'five_quizzes_passed',    icon:'🎯', name:'Quiz Master',      desc:'Pass five quizzes (score at least two-thirds).' },
+  { id:'seven_day_streak',       icon:'🔥', name:'On Fire',          desc:'Keep a seven-day learning streak.' },
+  { id:'xp_500',                 icon:'⭐', name:'Scholar',           desc:'Earn 500 XP.' },
+  { id:'flashcard_fan',          icon:'🃏', name:'Flashcard Fan',    desc:'Generate flashcards for a lesson.' },
+  { id:'note_taker',             icon:'📝', name:'Note Taker',        desc:'Write your first personal lesson note.' },
+];
 
 const el = (id) => document.getElementById(id);
 
@@ -530,13 +572,14 @@ async function enterApp(){
   el('user-name').textContent = currentUser;
   el('avatar-init').textContent = currentUser.slice(0,1).toUpperCase();
   el('topbar').classList.remove('hidden');
-  refreshMe();          // engagement: header XP bar / level badge
+  refreshMe();          // engagement: header XP bar / streak / badges
+  renderBadges();       // immediate locked-state render; refreshMe updates it
   await loadCourses();
   renderCourseGrid();
   showView('dashboard');
 }
 
-/* ---------------- Engagement stats (XP / level) ---------------- */
+/* ---------------- Engagement stats (XP / level / streak / badges) ---------------- */
 function updateHeaderStats(){
   const xp = meStats.xp || 0;
   const lvl = meStats.level || 0;
@@ -548,11 +591,63 @@ function updateHeaderStats(){
   el('streak-chip').classList.toggle('warm', streak > 0);
 }
 
+/* Toast — small non-blocking notification (badge earned, link copied, …) */
+function toast(icon, text, ms=3600){
+  try{
+    const t = document.createElement('div');
+    t.className = 'toast';
+    t.innerHTML = `<span class="t-icon">${icon}</span><span>${escapeHTML(text)}</span>`;
+    el('toast-wrap').appendChild(t);
+    setTimeout(()=>{ t.style.transition='opacity .3s ease'; t.style.opacity='0'; setTimeout(()=>t.remove(), 320); }, ms);
+  }catch(e){ /* cosmetic only */ }
+}
+
+function renderBadges(){
+  const grid = el('badges-grid');
+  if(!grid) return;
+  const earned = new Set(meStats.badges || []);
+  el('badge-count').textContent = earned.size + ' of ' + BADGE_CATALOG.length + ' earned';
+  grid.innerHTML = '';
+  BADGE_CATALOG.forEach(b=>{
+    const has = earned.has(b.id);
+    const card = document.createElement('div');
+    card.className = 'badge-card' + (has ? ' earned' : '');
+    card.title = has ? 'Earned' : 'Locked — ' + b.desc;
+    card.innerHTML = `
+      <span class="badge-icon">${b.icon}</span>
+      <div style="flex:1;">
+        <div class="badge-name">${escapeHTML(b.name)}</div>
+        <div class="badge-desc">${escapeHTML(b.desc)}</div>
+      </div>
+      ${has ? '' : '<span class="badge-lock">🔒</span>'}
+    `;
+    grid.appendChild(card);
+  });
+}
+
+/* Apply fresh stats from the server; announce newly earned badges via toast. */
+function applyMeStats(next){
+  const prevBadges = new Set(meStats.badges || []);
+  meStats = {
+    xp: next.xp || 0,
+    level: next.level || 0,
+    streak_count: next.streak_count != null ? next.streak_count : (meStats.streak_count || 0),
+    badges: Array.isArray(next.badges) ? next.badges : (meStats.badges || []),
+  };
+  updateHeaderStats();
+  renderBadges();
+  (meStats.badges || []).forEach(id=>{
+    if(!prevBadges.has(id)){
+      const meta = BADGE_CATALOG.find(b=>b.id===id);
+      if(meta) toast(meta.icon, `Badge earned: ${meta.name}!`);
+    }
+  });
+}
+
 async function refreshMe(){
   try{
     const me = await api('/auth/me');
-    meStats = { xp: me.xp || 0, level: me.level || 0, streak_count: me.streak_count || 0 };
-    updateHeaderStats();
+    applyMeStats(me);
   }catch(e){ /* header stats are optional chrome — never break the app */ }
 }
 
@@ -564,8 +659,8 @@ async function loadCourses(){
   }
 }
 
-el('nav-dashboard').addEventListener('click', async ()=>{ await loadCourses(); renderCourseGrid(); showView('dashboard'); });
-el('back-to-dash').addEventListener('click', async ()=>{ await loadCourses(); renderCourseGrid(); showView('dashboard'); });
+el('nav-dashboard').addEventListener('click', async ()=>{ refreshMe(); await loadCourses(); renderCourseGrid(); showView('dashboard'); });
+el('back-to-dash').addEventListener('click', async ()=>{ refreshMe(); await loadCourses(); renderCourseGrid(); showView('dashboard'); });
 
 /* ---------------- Dashboard rendering ---------------- */
 function renderCourseGrid(){
@@ -992,14 +1087,21 @@ async function submitQuizAttempt(course, quiz){
     const resp = await api(`/courses/${course.id}/modules/${activeModuleIndex}/quiz/submit`, {
       method:'POST', body:{ score, total: quiz.questions.length }
     });
-    meStats = { xp: resp.xp || 0, level: resp.level || 0, streak_count: resp.streak_count || 0 };
-    updateHeaderStats();
+    meStats = applyMeStatsSafe(resp);
     // The response carries the authoritative course state; later features
     // (attempt history) read it from here.
     if(resp.course) activeCourse = resp.course;
   }catch(e){ /* engagement is best-effort — never break the quiz UI */ }
 }
 
+function applyMeStatsSafe(resp){
+  applyMeStats({
+    xp: resp.xp, level: resp.level,
+    streak_count: resp.streak_count, badges: resp.badges,
+  });
+  return meStats;
+}
+
 /* ---- Course header edit ---- */
 el('toggle-course-edit').addEventListener('click', ()=>{
   const course = currentCourse();
```

</details>

---

## Feature 4 — Quiz History

**Commits:** `F4: Quiz history — quiz_attempts appended on submit, preserved on regeneration, ModuleOut optional field, Best/sparkline UI`

**Files changed/created:**

- `app/routers/courses.py`
- `app/schemas.py`
- `index.html`

**How it integrates / no-break confirmation:**

The quiz-submit endpoint appends `{score, total, date}` to a new optional `module["quiz_attempts"]` list; `module["quiz"]` keeps its existing behavior exactly (regeneration still replaces the active quiz, and now explicitly preserves the attempt history). `ModuleOut` gains optional `quiz_attempts` (None when absent, so older data serializes identically apart from the new null field) and the frontend renders "Best: X/Y" plus a mini bar-chart of the last 12 attempts under the quiz block — nothing renders when the field is missing. No request shape of any existing endpoint changed.

**New env vars / dependencies / migration steps:** No schema change (JSON list inside `modules`). No env vars or deps.

<details>
<summary>Full diff — F4: Quiz history — quiz_attempts appended on submit, preserved on regeneration, ModuleOut optional field, Best/sparkline UI</summary>

```diff
commit 9712450fc5db0861dffe140bace8b63dfab01db6
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:46:50 2026 +0000

    F4: Quiz history — quiz_attempts appended on submit, preserved on regeneration, ModuleOut optional field, Best/sparkline UI

diff --git a/app/routers/courses.py b/app/routers/courses.py
index 8e71a29..104b0fa 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -1,3 +1,4 @@
+from datetime import datetime
 from typing import List
 
 from fastapi import APIRouter, Depends, HTTPException
@@ -299,6 +300,21 @@ def submit_quiz(
     xp_awarded = XP_PER_CORRECT_ANSWER * payload.score
     user.xp = (user.xp or 0) + xp_awarded
     achievements.touch_streak(user)
+
+    # Quiz history: append the attempt (spec shape {score, total, date}) to
+    # the module's quiz_attempts list. module["quiz"] itself is untouched
+    # here and on regeneration, so the existing quiz flow is unchanged.
+    mod = dict(modules[index])
+    attempts = list(mod.get("quiz_attempts") or [])
+    attempts.append({
+        "score": payload.score,
+        "total": payload.total,
+        "date": datetime.utcnow().isoformat(timespec="seconds"),
+    })
+    mod["quiz_attempts"] = attempts
+    modules[index] = mod
+    course.modules = modules
+
     new_badges = achievements.evaluate(user, db)
     db.commit()
     db.refresh(user)
@@ -335,6 +351,8 @@ async def generate_quiz_route(
 
     mod = dict(modules[index])
     mod["quiz"] = quiz
+    # Regenerating replaces the active quiz but never wipes the attempt
+    # history recorded on this module (quiz_attempts), per spec.
     modules[index] = mod
     course.modules = modules
     db.commit()
diff --git a/app/schemas.py b/app/schemas.py
index a45725c..9efb34d 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -42,6 +42,9 @@ class ModuleOut(BaseModel):
     blogUrl: str = ""
     completed: bool = False
     quiz: Optional[Any] = None
+    # Optional engagement fields — absent on older data, so they default to
+    # None and the frontend simply hides those sections (graceful degrade).
+    quiz_attempts: Optional[List[Any]] = None
 
 
 class CourseCreateIn(BaseModel):
diff --git a/index.html b/index.html
index 10b0ec9..6c9515a 100644
--- a/index.html
+++ b/index.html
@@ -253,6 +253,16 @@
   .quiz-explain{margin-top:10px;font-size:13px;color:var(--muted);line-height:1.5;}
   .quiz-score{font-weight:600;margin-bottom:14px;}
 
+  /* ===== Quiz attempt history ===== */
+  .quiz-history{margin-top:16px;padding:14px 16px;border:1px solid var(--line);border-radius:12px;background:#FAFBFE;}
+  .quiz-history .qh-best{font-size:13.5px;font-weight:600;color:var(--ink);}
+  .qh-bars{display:flex;gap:5px;align-items:flex-end;margin-top:10px;height:34px;}
+  .qh-bar{width:14px;background:var(--line);border-radius:3px 3px 0 0;min-height:4px;}
+  .qh-bar.pass{background:var(--success);}
+  .qh-dates{display:flex;gap:5px;margin-top:4px;flex-wrap:wrap;}
+  .qh-date{font-family:'IBM Plex Mono',monospace;font-size:9.5px;color:var(--muted);width:14px;text-align:center;overflow:hidden;white-space:nowrap;}
+  .qh-hint{font-size:11.5px;color:var(--muted);margin-top:8px;}
+
   /* ===== Engagement: header XP / level ===== */
   .xp-chip{display:flex;align-items:center;gap:8px;}
   .level-badge{
@@ -953,6 +963,7 @@ function renderModuleMain(course){
     <div class="quiz-block" id="quiz-block">
       <div class="label">Check your understanding</div>
       <div id="quiz-container"></div>
+      <div id="quiz-history"></div>
     </div>
     <div class="module-actions">
       <button class="btn btn-ghost btn-sm" id="prev-mod" ${activeModuleIndex===0 ? 'disabled':''}>← Previous</button>
@@ -991,6 +1002,7 @@ async function regenerateModule(course){
 /* ---- Quiz ---- */
 function renderQuiz(course, module){
   const container = el('quiz-container');
+  renderQuizHistory(module);
   if(module.quiz){
     renderQuizQuestions(course, module, container);
     return;
@@ -1014,6 +1026,36 @@ function renderQuiz(course, module){
   });
 }
 
+/* ---- Quiz attempt history (Feature 4) ---- */
+function renderQuizHistory(module){
+  const holder = el('quiz-history');
+  if(!holder) return;
+  const attempts = (module && module.quiz_attempts) || [];
+  if(!attempts.length){ holder.innerHTML = ''; return; }   // no data -> no UI
+  const best = attempts.reduce((b,a)=>{
+    const ra = (a.total||0) > 0 ? (a.score||0)/a.total : 0;
+    const rb = (b.total||0) > 0 ? (b.score||0)/b.total : 0;
+    return ra >= rb ? a : b;
+  });
+  const bars = attempts.slice(-12).map(a=>{
+    const ratio = (a.total||0) > 0 ? (a.score||0)/a.total : 0;
+    const passed = (a.score||0)*3 >= (a.total||0)*2;
+    const d = a.date ? new Date(a.date) : null;
+    const label = d && !isNaN(d) ? d.toLocaleDateString(undefined,{month:'short',day:'numeric'}) : '';
+    return `<div style="display:flex;flex-direction:column;align-items:center;gap:3px;">
+      <div class="qh-bar${passed ? ' pass' : ''}" style="height:${Math.max(4, Math.round(ratio*30))}px;" title="${(a.score??'?')}/${(a.total??'?')}${label ? ' · ' + label : ''}"></div>
+      <span class="qh-date">${escapeHTML(label || ('' + (a.score??'?') + '/' + (a.total??'?')))}</span>
+    </div>`;
+  }).join('');
+  holder.innerHTML = `
+    <div class="quiz-history">
+      <span class="qh-best">Best: ${best.score ?? '?'}/${best.total ?? '?'}</span>
+      <span class="qh-hint" style="margin-left:8px;">${attempts.length} attempt${attempts.length===1 ? '' : 's'} · green = passed</span>
+      <div class="qh-bars">${bars}</div>
+    </div>
+  `;
+}
+
 function renderQuizQuestions(course, module, container){
   const quiz = module.quiz;
   let quizSubmitted = false;   // engagement: report the attempt once, when every question is answered
@@ -1088,9 +1130,12 @@ async function submitQuizAttempt(course, quiz){
       method:'POST', body:{ score, total: quiz.questions.length }
     });
     meStats = applyMeStatsSafe(resp);
-    // The response carries the authoritative course state; later features
-    // (attempt history) read it from here.
+    // The response carries the authoritative course state, including the
+    // newly recorded attempt — refresh the history block from it.
     if(resp.course) activeCourse = resp.course;
+    if(activeCourse && activeCourse.modules[activeModuleIndex]){
+      renderQuizHistory(activeCourse.modules[activeModuleIndex]);
+    }
   }catch(e){ /* engagement is best-effort — never break the quiz UI */ }
 }
 
```

</details>

---

## Feature 5 — Inline Flashcards

**Commits:** `F5: Flashcards — ai.generate_flashcards, POST /flashcards endpoint, ModuleOut.flashcards, flip-card UI`

**Files changed/created:**

- `app/ai.py`
- `app/routers/courses.py`
- `app/schemas.py`
- `index.html`

**How it integrates / no-break confirmation:**

New `ai.generate_flashcards(title, notes)` follows the established pattern exactly — same `_call_gemini` client setup, same `responseSchema`, same `_clean_json` parsing, same `AIError` handling — asking for six `{front, back}` cards and filtering malformed entries so one bad card can never break the UI. The new `POST /api/courses/{id}/modules/{i}/flashcards` endpoint stores the result in the new optional `module["flashcards"]` field and returns the standard `CourseOut`, mirroring the quiz-generation endpoint's shape. It also re-evaluates badges (flashcard_fan). The frontend adds a Flashcards section under each lesson with 3-D flip cards, shuffle, and regeneration; modules without the field show only the generate button.

**New env vars / dependencies / migration steps:** No schema change. No env vars or deps.

<details>
<summary>Full diff — F5: Flashcards — ai.generate_flashcards, POST /flashcards endpoint, ModuleOut.flashcards, flip-card UI</summary>

```diff
commit 7e6684f9448dab8d244970b4c3e833e5b60e9e6f
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:48:29 2026 +0000

    F5: Flashcards — ai.generate_flashcards, POST /flashcards endpoint, ModuleOut.flashcards, flip-card UI

diff --git a/app/ai.py b/app/ai.py
index 1e99ae9..6085423 100644
--- a/app/ai.py
+++ b/app/ai.py
@@ -472,3 +472,49 @@ async def generate_quiz(module_title: str, module_notes: str) -> dict:
     if not isinstance(parsed, dict) or not isinstance(parsed.get("questions"), list) or not parsed["questions"]:
         raise AIError("Incomplete quiz data returned by the model.")
     return parsed
+
+
+async def generate_flashcards(module_title: str, module_notes: str) -> dict:
+    """Flashcards for a lesson — same _call_gemini + _clean_json pattern as
+    the other generators (Feature 5)."""
+    prompt = (
+        f'Lesson title: "{module_title}"\n'
+        f'Lesson notes:\n{module_notes}\n\n'
+        "Create exactly 6 flashcards to help a learner memorize the key ideas "
+        "of this lesson. Each card has a short 'front' (a question or term, "
+        "12 words or fewer) and a concise 'back' answer (30 words or fewer)."
+    )
+    fc_schema = {
+        "type": "OBJECT",
+        "properties": {
+            "cards": {
+                "type": "ARRAY",
+                "items": {
+                    "type": "OBJECT",
+                    "properties": {
+                        "front": {"type": "STRING"},
+                        "back": {"type": "STRING"},
+                    },
+                    "required": ["front", "back"],
+                },
+            },
+        },
+        "required": ["cards"],
+    }
+
+    raw = await _call_gemini(prompt, schema=fc_schema, max_tokens=2000)
+    parsed = _clean_json(raw)
+
+    if not isinstance(parsed, dict) or not isinstance(parsed.get("cards"), list) or not parsed["cards"]:
+        raise AIError("Incomplete flashcard data returned by the model.")
+
+    # Keep only well-formed cards so one malformed entry can never break the
+    # flip-card UI downstream.
+    cards = [
+        {"front": str(c.get("front", "")).strip(), "back": str(c.get("back", "")).strip()}
+        for c in parsed["cards"]
+        if isinstance(c, dict) and str(c.get("front", "")).strip() and str(c.get("back", "")).strip()
+    ]
+    if not cards:
+        raise AIError("No usable flashcards returned by the model.")
+    return {"cards": cards}
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 104b0fa..019fde3 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -358,3 +358,33 @@ async def generate_quiz_route(
     db.commit()
     db.refresh(course)
     return course
+
+
+@router.post("/{course_id}/modules/{index}/flashcards", response_model=schemas.CourseOut)
+async def generate_flashcards_route(
+    course_id: int,
+    index: int,
+    db: Session = Depends(get_db),
+    user: models.User = Depends(get_current_user),
+):
+    """Generate flashcards for a lesson and store them on the module's new
+    optional `flashcards` field (list of {front, back}). Existing fields are
+    untouched; modules without the field keep rendering normally."""
+    course = _get_owned_course(course_id, user, db)
+    modules = list(course.modules or [])
+    if index < 0 or index >= len(modules):
+        raise HTTPException(status_code=404, detail="Lesson not found.")
+
+    try:
+        result = await ai.generate_flashcards(modules[index]["title"], modules[index].get("notes", ""))
+    except ai.AIError as e:
+        raise HTTPException(status_code=502, detail=str(e))
+
+    mod = dict(modules[index])
+    mod["flashcards"] = result["cards"]
+    modules[index] = mod
+    course.modules = modules
+    achievements.evaluate(user, db)   # may award flashcard_fan
+    db.commit()
+    db.refresh(course)
+    return course
diff --git a/app/schemas.py b/app/schemas.py
index 9efb34d..722268a 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -45,6 +45,7 @@ class ModuleOut(BaseModel):
     # Optional engagement fields — absent on older data, so they default to
     # None and the frontend simply hides those sections (graceful degrade).
     quiz_attempts: Optional[List[Any]] = None
+    flashcards: Optional[List[Any]] = None
 
 
 class CourseCreateIn(BaseModel):
diff --git a/index.html b/index.html
index 6c9515a..e726acf 100644
--- a/index.html
+++ b/index.html
@@ -263,6 +263,23 @@
   .qh-date{font-family:'IBM Plex Mono',monospace;font-size:9.5px;color:var(--muted);width:14px;text-align:center;overflow:hidden;white-space:nowrap;}
   .qh-hint{font-size:11.5px;color:var(--muted);margin-top:8px;}
 
+  /* ===== Flashcards ===== */
+  .fc-block{margin-top:26px;padding-top:22px;border-top:1px solid var(--line);}
+  .fc-block .label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px;font-weight:600;}
+  .fc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px;}
+  .fc-card{perspective:900px;cursor:pointer;}
+  .fc-inner{position:relative;width:100%;height:160px;transition:transform .5s cubic-bezier(.2,.7,.3,1);transform-style:preserve-3d;}
+  .fc-card.flipped .fc-inner{transform:rotateY(180deg);}
+  .fc-face{
+    position:absolute;inset:0;backface-visibility:hidden;-webkit-backface-visibility:hidden;
+    display:flex;align-items:center;justify-content:center;text-align:center;padding:18px;
+    border-radius:14px;border:1px solid var(--line);font-size:14.5px;line-height:1.5;
+  }
+  .fc-front{background:var(--paper-2);color:var(--ink);font-weight:600;}
+  .fc-back{background:#FBF3E6;color:#2B3358;transform:rotateY(180deg);}
+  .fc-face .fc-corner{position:absolute;bottom:8px;right:11px;font-size:10px;color:var(--muted);font-family:'IBM Plex Mono',monospace;}
+  .fc-actions{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;}
+
   /* ===== Engagement: header XP / level ===== */
   .xp-chip{display:flex;align-items:center;gap:8px;}
   .level-badge{
@@ -965,6 +982,10 @@ function renderModuleMain(course){
       <div id="quiz-container"></div>
       <div id="quiz-history"></div>
     </div>
+    <div class="fc-block" id="fc-block">
+      <div class="label">Flashcards</div>
+      <div id="fc-container"></div>
+    </div>
     <div class="module-actions">
       <button class="btn btn-ghost btn-sm" id="prev-mod" ${activeModuleIndex===0 ? 'disabled':''}>← Previous</button>
       <button class="btn btn-primary btn-sm" id="toggle-complete">${module.completed ? 'Mark as not done' : 'Mark lesson complete'}</button>
@@ -986,6 +1007,7 @@ function renderModuleMain(course){
   el('regen-mod-btn').addEventListener('click', ()=>regenerateModule(course));
 
   renderQuiz(course, module);
+  renderFlashcards(course, module);
 }
 
 async function regenerateModule(course){
@@ -1139,6 +1161,66 @@ async function submitQuizAttempt(course, quiz){
   }catch(e){ /* engagement is best-effort — never break the quiz UI */ }
 }
 
+/* ---- Flashcards (Feature 5) ---- */
+function renderFlashcards(course, module){
+  const c = el('fc-container');
+  if(!c) return;
+  const cards = (module && module.flashcards) || [];
+
+  if(!cards.length){
+    c.innerHTML = `
+      <button class="btn btn-ghost btn-sm" id="gen-fc-btn">🃏 Generate flashcards</button>
+      <div id="fc-status" class="gen-status hidden" style="color:var(--muted);margin-top:10px;"><span class="spinner-dot"></span> Writing flashcards…</div>
+      <div id="fc-error" class="form-error hidden"></div>`;
+    el('gen-fc-btn').addEventListener('click', async ()=>{
+      el('fc-status').classList.remove('hidden');
+      el('fc-error').classList.add('hidden');
+      el('gen-fc-btn').disabled = true;
+      try{
+        activeCourse = await api(`/courses/${course.id}/modules/${activeModuleIndex}/flashcards`, { method:'POST' });
+        renderCourseView();
+        refreshMe();   // flashcard_fan badge may have been earned
+      }catch(err){
+        el('fc-status').classList.add('hidden');
+        el('fc-error').textContent = 'Could not generate flashcards: ' + err.message;
+        el('fc-error').classList.remove('hidden');
+        el('gen-fc-btn').disabled = false;
+      }
+    });
+    return;
+  }
+
+  const cardHtml = cards.map((card,i)=>`
+    <div class="fc-card" data-fc="${i}" title="Click to flip">
+      <div class="fc-inner">
+        <div class="fc-face fc-front">${escapeHTML(card.front || '')}<span class="fc-corner">front</span></div>
+        <div class="fc-face fc-back">${escapeHTML(card.back || '')}<span class="fc-corner">back</span></div>
+      </div>
+    </div>`).join('');
+  c.innerHTML = `<div class="fc-actions">
+      <button class="btn btn-ghost btn-sm" id="fc-shuffle">🔀 Shuffle</button>
+      <button class="btn btn-ghost btn-sm" id="fc-new">✎ New cards</button>
+    </div>
+    <div class="fc-grid" id="fc-grid">${cardHtml}</div>
+    <p class="qh-hint">Click a card to flip it.</p>`;
+
+  c.querySelectorAll('.fc-card').forEach(card=>{
+    card.addEventListener('click', ()=>card.classList.toggle('flipped'));
+  });
+  el('fc-shuffle').addEventListener('click', ()=>{
+    const grid = el('fc-grid');
+    [...grid.children].sort(()=>Math.random()-0.5).forEach(ch=>grid.appendChild(ch));
+  });
+  el('fc-new').addEventListener('click', async ()=>{
+    el('fc-new').disabled = true;
+    try{
+      activeCourse = await api(`/courses/${course.id}/modules/${activeModuleIndex}/flashcards`, { method:'POST' });
+      renderCourseView();
+    }catch(err){ alert('Could not generate new flashcards: ' + err.message); }
+    el('fc-new').disabled = false;
+  });
+}
+
 function applyMeStatsSafe(resp){
   applyMeStats({
     xp: resp.xp, level: resp.level,
```

</details>

---

## Feature 6 — "Ask about this lesson" chat

**Commits:** `F6: Ask-about-this-lesson — ai.answer_question, stateless POST /ask endpoint, chat UI`

**Files changed/created:**

- `app/ai.py`
- `app/routers/courses.py`
- `app/schemas.py`
- `index.html`

**How it integrates / no-break confirmation:**

New `ai.answer_question(title, notes, question)` uses the same Gemini call pattern (JSON schema with a single `answer` string) and the new stateless `POST /api/courses/{id}/modules/{i}/ask` returns `{answer}` without persisting anything — the course row is never touched. The frontend renders a small chat under each lesson: user bubbles right, AI answers left (markdown-rendered), a thinking indicator while the request is in flight, and inline error display. Question length is validated (2–500 chars).

**New env vars / dependencies / migration steps:** No schema change. No env vars or deps.

<details>
<summary>Full diff — F6: Ask-about-this-lesson — ai.answer_question, stateless POST /ask endpoint, chat UI</summary>

```diff
commit 7b793f27b999de8b4275ca3e829013482592fbc3
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:49:54 2026 +0000

    F6: Ask-about-this-lesson — ai.answer_question, stateless POST /ask endpoint, chat UI

diff --git a/app/ai.py b/app/ai.py
index 6085423..4ebc7ae 100644
--- a/app/ai.py
+++ b/app/ai.py
@@ -518,3 +518,29 @@ async def generate_flashcards(module_title: str, module_notes: str) -> dict:
     if not cards:
         raise AIError("No usable flashcards returned by the model.")
     return {"cards": cards}
+
+
+async def answer_question(module_title: str, module_notes: str, question: str) -> str:
+    """Answer a learner's question in the context of a lesson's notes.
+    Stateless — nothing is stored (Feature 6)."""
+    prompt = (
+        f'Lesson title: "{module_title}"\n'
+        f'Lesson notes:\n{module_notes}\n\n'
+        f'A learner asks: "{question}"\n\n'
+        "Answer in at most 120 words of plain text, friendly and direct. Base "
+        "the answer on the lesson notes where possible; if the notes don't "
+        "cover it, say so briefly and answer from general knowledge."
+    )
+    answer_schema = {
+        "type": "OBJECT",
+        "properties": {"answer": {"type": "STRING"}},
+        "required": ["answer"],
+    }
+
+    raw = await _call_gemini(prompt, schema=answer_schema, max_tokens=800)
+    parsed = _clean_json(raw)
+
+    answer = parsed.get("answer") if isinstance(parsed, dict) else None
+    if not answer or not str(answer).strip():
+        raise AIError("No answer returned by the model.")
+    return str(answer).strip()
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 019fde3..63c0ca6 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -388,3 +388,29 @@ async def generate_flashcards_route(
     db.commit()
     db.refresh(course)
     return course
+
+
+@router.post("/{course_id}/modules/{index}/ask", response_model=schemas.AskOut)
+async def ask_lesson_question(
+    course_id: int,
+    index: int,
+    payload: schemas.AskIn,
+    db: Session = Depends(get_db),
+    user: models.User = Depends(get_current_user),
+):
+    """"Ask about this lesson" (Feature 6): sends the lesson notes plus the
+    learner's question to Gemini and returns the answer. Stateless — nothing
+    is persisted, so the course data is never touched."""
+    course = _get_owned_course(course_id, user, db)
+    modules = list(course.modules or [])
+    if index < 0 or index >= len(modules):
+        raise HTTPException(status_code=404, detail="Lesson not found.")
+
+    try:
+        answer = await ai.answer_question(
+            modules[index]["title"], modules[index].get("notes", ""), payload.question.strip()
+        )
+    except ai.AIError as e:
+        raise HTTPException(status_code=502, detail=str(e))
+
+    return schemas.AskOut(answer=answer)
diff --git a/app/schemas.py b/app/schemas.py
index 722268a..a09f9e9 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -83,6 +83,14 @@ class QuizSubmitIn(BaseModel):
     total: int = Field(ge=1)
 
 
+class AskIn(BaseModel):
+    question: str = Field(min_length=2, max_length=500)
+
+
+class AskOut(BaseModel):
+    answer: str
+
+
 class CourseOut(BaseModel):
     model_config = ConfigDict(from_attributes=True)
     id: int
diff --git a/index.html b/index.html
index e726acf..20c7332 100644
--- a/index.html
+++ b/index.html
@@ -280,6 +280,22 @@
   .fc-face .fc-corner{position:absolute;bottom:8px;right:11px;font-size:10px;color:var(--muted);font-family:'IBM Plex Mono',monospace;}
   .fc-actions{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;}
 
+  /* ===== Ask about this lesson ===== */
+  .ask-block{margin-top:26px;padding-top:22px;border-top:1px solid var(--line);}
+  .ask-block .label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px;font-weight:600;}
+  .ask-form{display:flex;gap:10px;flex-wrap:wrap;}
+  .ask-form input{
+    flex:1;min-width:200px;padding:11px 14px;border:1px solid var(--line);border-radius:10px;
+    font-size:14px;background:var(--paper-2);color:var(--ink);
+  }
+  .ask-log{display:flex;flex-direction:column;gap:10px;margin-bottom:12px;}
+  .ask-msg{max-width:88%;padding:11px 14px;border-radius:12px;font-size:14px;line-height:1.55;}
+  .ask-msg.user{align-self:flex-end;background:var(--ink);color:#fff;border-bottom-right-radius:4px;}
+  .ask-msg.ai{align-self:flex-start;background:#F7F7FC;border:1px solid var(--line);border-bottom-left-radius:4px;}
+  .ask-msg.ai p{margin:0 0 8px;}
+  .ask-msg.ai p:last-child{margin-bottom:0;}
+  .ask-thinking{align-self:flex-start;color:var(--muted);font-size:13px;display:flex;align-items:center;gap:8px;}
+
   /* ===== Engagement: header XP / level ===== */
   .xp-chip{display:flex;align-items:center;gap:8px;}
   .level-badge{
@@ -986,6 +1002,14 @@ function renderModuleMain(course){
       <div class="label">Flashcards</div>
       <div id="fc-container"></div>
     </div>
+    <div class="ask-block" id="ask-block">
+      <div class="label">Ask about this lesson</div>
+      <div id="ask-log" class="ask-log"></div>
+      <form id="ask-form" class="ask-form">
+        <input id="ask-input" placeholder="Ask anything about this lesson…" maxlength="500" />
+        <button class="btn btn-primary btn-sm" id="ask-send" type="submit">Ask</button>
+      </form>
+    </div>
     <div class="module-actions">
       <button class="btn btn-ghost btn-sm" id="prev-mod" ${activeModuleIndex===0 ? 'disabled':''}>← Previous</button>
       <button class="btn btn-primary btn-sm" id="toggle-complete">${module.completed ? 'Mark as not done' : 'Mark lesson complete'}</button>
@@ -1008,6 +1032,7 @@ function renderModuleMain(course){
 
   renderQuiz(course, module);
   renderFlashcards(course, module);
+  renderAsk(course, module);
 }
 
 async function regenerateModule(course){
@@ -1221,6 +1246,44 @@ function renderFlashcards(course, module){
   });
 }
 
+/* ---- Ask about this lesson (Feature 6) ---- */
+function renderAsk(course, module){
+  const log = el('ask-log');
+  const form = el('ask-form');
+  const input = el('ask-input');
+  const send = el('ask-send');
+  if(!log || !form) return;
+  log.innerHTML = '';   // stateless chat: fresh per lesson render
+  const busyDot = () => `<div class="ask-thinking" id="ask-thinking"><span class="spinner-dot"></span> Thinking…</div>`;
+
+  form.addEventListener('submit', async (e)=>{
+    e.preventDefault();
+    const q = (input.value || '').trim();
+    if(!q || send.disabled) return;
+    input.value = '';
+    send.disabled = true;
+    log.insertAdjacentHTML('beforeend', `<div class="ask-msg user">${escapeHTML(q)}</div>`);
+    log.insertAdjacentHTML('beforeend', busyDot());
+    log.scrollTop = log.scrollHeight;   // keeps newest in view if log grows
+    try{
+      const resp = await api(`/courses/${course.id}/modules/${activeModuleIndex}/ask`, {
+        method:'POST', body:{ question: q }
+      });
+      const holder = document.createElement('div');
+      holder.className = 'ask-msg ai';
+      const answerHtml = (typeof marked !== 'undefined') ? marked.parse(resp.answer || '') : escapeHTML(resp.answer || '');
+      holder.innerHTML = answerHtml;
+      const t = el('ask-thinking'); if(t) t.remove();
+      log.appendChild(holder);
+    }catch(err){
+      const t = el('ask-thinking'); if(t) t.remove();
+      log.insertAdjacentHTML('beforeend', `<div class="ask-msg ai" style="border-color:var(--danger);color:var(--danger);">${escapeHTML(err.message || 'Something went wrong.')}</div>`);
+    }
+    send.disabled = false;
+    input.focus();
+  });
+}
+
 function applyMeStatsSafe(resp){
   applyMeStats({
     xp: resp.xp, level: resp.level,
```

</details>

---

## Feature 7 — Personal Notes / Highlights

**Commits:** `F7: Personal notes — Note table, POST/GET module notes + DELETE /api/notes/{id}, index-sync on lesson delete/reorder, note_taker badge, notes UI`

**Files changed/created:**

- `app/models.py`
- `app/routers/courses.py`
- `app/schemas.py`
- `app/main.py`
- `index.html`

**How it integrates / no-break confirmation:**

A fully separate `Note` table (`id, user_id, course_id, module_index, text, created_at`) — zero changes to `Course` or `User` rows. New endpoints: `POST`/`GET /api/courses/{id}/modules/{i}/notes` and `DELETE /api/notes/{note_id}` (the delete route lives on a small second router registered in `main.py`). All routes enforce course ownership + user scoping. Because notes are keyed by module index, the existing lesson-delete endpoint now drops the removed lesson's notes and shifts later ones down, lesson reorder swaps the corresponding note indexes, and course delete removes the course's notes first (prevents FK rejection on Postgres) — all additive lines inside the existing handlers with unchanged response shapes. The note_taker badge condition joins `evaluate()` here. Frontend: a textarea + note list with delete under each lesson (text-selection highlighting remains a stretch goal, per spec).

**New env vars / dependencies / migration steps:** New table `notes` — created automatically by `Base.metadata.create_all` on fresh databases AND existing ones (create_all adds missing tables). Postgres manual SQL in the Migration section below. No new env vars or deps.

<details>
<summary>Full diff — F7: Personal notes — Note table, POST/GET module notes + DELETE /api/notes/{id}, index-sync on lesson delete/reorder, note_taker badge, notes UI</summary>

```diff
commit 4a9967591b492e6cc3c17c60912691888b24ad9a
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:52:42 2026 +0000

    F7: Personal notes — Note table, POST/GET module notes + DELETE /api/notes/{id}, index-sync on lesson delete/reorder, note_taker badge, notes UI

diff --git a/app/achievements.py b/app/achievements.py
index 10868b3..8e3098e 100644
--- a/app/achievements.py
+++ b/app/achievements.py
@@ -126,6 +126,10 @@ def evaluate(user, db: Session) -> list:
         (c.modules or []) and all(m.get("completed") for m in c.modules)
         for c in courses
     )
+    has_note = (
+        db.query(models.Note.id).filter(models.Note.user_id == user.id).first()
+        is not None
+    )
 
     conditions = {
         "first_lesson": any(m.get("completed") for m in all_modules),
@@ -135,7 +139,7 @@ def evaluate(user, db: Session) -> list:
         "seven_day_streak": (user.streak_count or 0) >= 7,
         "xp_500": (user.xp or 0) >= 500,
         "flashcard_fan": any(m.get("flashcards") for m in all_modules),
-        # "note_taker" joins here in Feature 7 together with the Note table.
+        "note_taker": has_note,
     }
 
     new_badges = [
diff --git a/app/main.py b/app/main.py
index 5fe767c..1114619 100644
--- a/app/main.py
+++ b/app/main.py
@@ -33,6 +33,7 @@ app.add_middleware(
 
 app.include_router(auth.router)
 app.include_router(courses.router)
+app.include_router(courses.notes_router)   # /api/notes/... (personal notes, Feature 7)
 
 
 @app.get("/api/health")
diff --git a/app/models.py b/app/models.py
index 85db93e..d6e3848 100644
--- a/app/models.py
+++ b/app/models.py
@@ -1,4 +1,4 @@
-from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String
+from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Text
 from sqlalchemy.orm import relationship
 from sqlalchemy.sql import func
 
@@ -40,3 +40,17 @@ class Course(Base):
     created_at = Column(DateTime(timezone=True), server_default=func.now())
 
     owner = relationship("User", back_populates="courses")
+
+
+class Note(Base):
+    """Personal lesson notes (Feature 7) — a fully separate table; Course and
+    User are untouched. Keyed by (user, course, module_index); indexes shift
+    when lessons are removed/reordered, kept in sync by the courses router."""
+    __tablename__ = "notes"
+
+    id = Column(Integer, primary_key=True, index=True)
+    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
+    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
+    module_index = Column(Integer, nullable=False)
+    text = Column(Text, nullable=False)
+    created_at = Column(DateTime(timezone=True), server_default=func.now())
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 63c0ca6..8d2b7cf 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -22,6 +22,10 @@ async def generate_course(topic: str) -> dict:
 
 router = APIRouter(prefix="/api/courses", tags=["courses"])
 
+# Personal notes live under course-scoped paths; only the delete route sits
+# under its own /api/notes prefix (per spec). Registered in main.py.
+notes_router = APIRouter(prefix="/api/notes", tags=["notes"])
+
 # XP awards for engagement actions (spec: +10 per completed lesson, +5 per
 # correct quiz answer). Pure side effects — they never alter any existing
 # endpoint's response shape.
@@ -116,6 +120,9 @@ def delete_course(
     course_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
 ):
     course = _get_owned_course(course_id, user, db)
+    # Personal notes (Feature 7) reference the course; drop them first so
+    # FK-constrained databases (Postgres) don't reject the course delete.
+    db.query(models.Note).filter(models.Note.course_id == course.id).delete()
     db.delete(course)
     db.commit()
     return {"ok": True}
@@ -189,6 +196,14 @@ def delete_module(
         raise HTTPException(status_code=404, detail="Lesson not found.")
 
     modules.pop(index)
+    # Keep personal notes (Feature 7) attached to the right lesson: drop the
+    # removed lesson's notes and shift later ones down by one.
+    db.query(models.Note).filter(
+        models.Note.course_id == course.id, models.Note.module_index == index
+    ).delete(synchronize_session=False)
+    db.query(models.Note).filter(
+        models.Note.course_id == course.id, models.Note.module_index > index
+    ).update({models.Note.module_index: models.Note.module_index - 1}, synchronize_session=False)
     course.modules = modules
     db.commit()
     db.refresh(course)
@@ -210,6 +225,15 @@ def reorder_module(
         raise HTTPException(status_code=400, detail="Cannot move lesson there.")
 
     modules[index], modules[target] = modules[target], modules[index]
+    # Swap personal-note indexes (Feature 7) so notes travel with their
+    # lesson. Uses a temp value because module_index is not unique.
+    notes_q = db.query(models.Note).filter(models.Note.course_id == course.id)
+    notes_q.filter(models.Note.module_index == index).update(
+        {models.Note.module_index: -1}, synchronize_session=False)
+    notes_q.filter(models.Note.module_index == target).update(
+        {models.Note.module_index: index}, synchronize_session=False)
+    notes_q.filter(models.Note.module_index == -1).update(
+        {models.Note.module_index: target}, synchronize_session=False)
     course.modules = modules
     db.commit()
     db.refresh(course)
@@ -414,3 +438,73 @@ async def ask_lesson_question(
         raise HTTPException(status_code=502, detail=str(e))
 
     return schemas.AskOut(answer=answer)
+
+
+@router.post("/{course_id}/modules/{index}/notes", response_model=schemas.NoteOut)
+def create_lesson_note(
+    course_id: int,
+    index: int,
+    payload: schemas.NoteCreateIn,
+    db: Session = Depends(get_db),
+    user: models.User = Depends(get_current_user),
+):
+    """Personal notes (Feature 7): a learner-authored note attached to a
+    specific lesson. Stored in its own table — Course/User are untouched."""
+    course = _get_owned_course(course_id, user, db)
+    modules = list(course.modules or [])
+    if index < 0 or index >= len(modules):
+        raise HTTPException(status_code=404, detail="Lesson not found.")
+
+    note = models.Note(
+        user_id=user.id,
+        course_id=course.id,
+        module_index=index,
+        text=payload.text.strip(),
+    )
+    db.add(note)
+    achievements.evaluate(user, db)   # may award note_taker
+    db.commit()
+    db.refresh(note)
+    return note
+
+
+@router.get("/{course_id}/modules/{index}/notes", response_model=List[schemas.NoteOut])
+def list_lesson_notes(
+    course_id: int,
+    index: int,
+    db: Session = Depends(get_db),
+    user: models.User = Depends(get_current_user),
+):
+    course = _get_owned_course(course_id, user, db)
+    modules = list(course.modules or [])
+    if index < 0 or index >= len(modules):
+        raise HTTPException(status_code=404, detail="Lesson not found.")
+
+    return (
+        db.query(models.Note)
+        .filter(
+            models.Note.user_id == user.id,
+            models.Note.course_id == course.id,
+            models.Note.module_index == index,
+        )
+        .order_by(models.Note.created_at.asc(), models.Note.id.asc())
+        .all()
+    )
+
+
+@notes_router.delete("/{note_id}")
+def delete_lesson_note(
+    note_id: int,
+    db: Session = Depends(get_db),
+    user: models.User = Depends(get_current_user),
+):
+    note = (
+        db.query(models.Note)
+        .filter(models.Note.id == note_id, models.Note.user_id == user.id)
+        .first()
+    )
+    if not note:
+        raise HTTPException(status_code=404, detail="Note not found.")
+    db.delete(note)
+    db.commit()
+    return {"ok": True}
diff --git a/app/schemas.py b/app/schemas.py
index a09f9e9..a0df00e 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -91,6 +91,17 @@ class AskOut(BaseModel):
     answer: str
 
 
+class NoteCreateIn(BaseModel):
+    text: str = Field(min_length=1, max_length=2000)
+
+
+class NoteOut(BaseModel):
+    model_config = ConfigDict(from_attributes=True)
+    id: int
+    text: str
+    created_at: datetime
+
+
 class CourseOut(BaseModel):
     model_config = ConfigDict(from_attributes=True)
     id: int
diff --git a/index.html b/index.html
index 20c7332..616a60d 100644
--- a/index.html
+++ b/index.html
@@ -296,6 +296,22 @@
   .ask-msg.ai p:last-child{margin-bottom:0;}
   .ask-thinking{align-self:flex-start;color:var(--muted);font-size:13px;display:flex;align-items:center;gap:8px;}
 
+  /* ===== Personal notes ===== */
+  .mynotes-block{margin-top:26px;padding-top:22px;border-top:1px solid var(--line);}
+  .mynotes-block .label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px;font-weight:600;}
+  .mynotes-form{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;}
+  .mynotes-form textarea{
+    flex:1;min-width:220px;padding:11px 14px;border:1px solid var(--line);border-radius:10px;
+    font-size:14px;resize:vertical;min-height:64px;background:var(--paper-2);color:var(--ink);
+  }
+  .mynotes-list{display:flex;flex-direction:column;gap:10px;}
+  .mynote-item{padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:#F7F7FC;}
+  .mynote-item p{margin:0;font-size:14px;line-height:1.55;white-space:pre-wrap;word-break:break-word;}
+  .mynote-meta{display:flex;align-items:center;justify-content:space-between;margin-top:8px;}
+  .mynote-meta .when{font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:var(--muted);}
+  .mynote-del{background:none;border:none;color:var(--muted);font-size:11.5px;padding:2px 4px;}
+  .mynote-del:hover{color:var(--danger);}
+
   /* ===== Engagement: header XP / level ===== */
   .xp-chip{display:flex;align-items:center;gap:8px;}
   .level-badge{
@@ -1010,6 +1026,14 @@ function renderModuleMain(course){
         <button class="btn btn-primary btn-sm" id="ask-send" type="submit">Ask</button>
       </form>
     </div>
+    <div class="mynotes-block" id="mynotes-block">
+      <div class="label">My notes</div>
+      <div id="mynotes-list" class="mynotes-list"></div>
+      <div class="mynotes-form" style="margin-top:12px;">
+        <textarea id="mynote-input" rows="2" maxlength="2000" placeholder="Jot down your own thoughts on this lesson…"></textarea>
+        <button class="btn btn-primary btn-sm" id="add-mynote" style="align-self:flex-end;">Add note</button>
+      </div>
+    </div>
     <div class="module-actions">
       <button class="btn btn-ghost btn-sm" id="prev-mod" ${activeModuleIndex===0 ? 'disabled':''}>← Previous</button>
       <button class="btn btn-primary btn-sm" id="toggle-complete">${module.completed ? 'Mark as not done' : 'Mark lesson complete'}</button>
@@ -1033,6 +1057,7 @@ function renderModuleMain(course){
   renderQuiz(course, module);
   renderFlashcards(course, module);
   renderAsk(course, module);
+  renderLessonNotes(course);
 }
 
 async function regenerateModule(course){
@@ -1284,6 +1309,60 @@ function renderAsk(course, module){
   });
 }
 
+/* ---- Personal notes (Feature 7) ---- */
+async function refreshLessonNotes(course){
+  const list = el('mynotes-list');
+  if(!list || !course) return;
+  let notes = [];
+  try{
+    notes = await api(`/courses/${course.id}/modules/${activeModuleIndex}/notes`);
+  }catch(e){ notes = []; }   // notes are optional chrome — degrade silently
+  if(!notes.length){
+    list.innerHTML = `<p class="qh-hint" style="margin:0;">No personal notes on this lesson yet.</p>`;
+    return;
+  }
+  list.innerHTML = '';
+  notes.forEach(n=>{
+    const d = n.created_at ? new Date(n.created_at) : null;
+    const when = d && !isNaN(d) ? d.toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'}) : '';
+    const item = document.createElement('div');
+    item.className = 'mynote-item';
+    item.innerHTML = `<p>${escapeHTML(n.text || '')}</p>
+      <div class="mynote-meta">
+        <span class="when">${escapeHTML(when)}</span>
+        <button class="mynote-del" data-note="${n.id}">Delete</button>
+      </div>`;
+    list.appendChild(item);
+  });
+  list.querySelectorAll('[data-note]').forEach(b=>b.addEventListener('click', async ()=>{
+    try{
+      await api('/notes/' + b.dataset.note, { method:'DELETE' });
+      await refreshLessonNotes(course);
+    }catch(err){ alert('Could not delete that note: ' + err.message); }
+  }));
+}
+
+function renderLessonNotes(course){
+  const addBtn = el('add-mynote');
+  const input = el('mynote-input');
+  if(!addBtn || !input) return;
+  addBtn.addEventListener('click', async ()=>{
+    const text = (input.value || '').trim();
+    if(!text) return;
+    addBtn.disabled = true;
+    try{
+      await api(`/courses/${course.id}/modules/${activeModuleIndex}/notes`, {
+        method:'POST', body:{ text }
+      });
+      input.value = '';
+      await refreshLessonNotes(course);
+      refreshMe();   // note_taker badge may have been earned
+    }catch(err){ alert('Could not save that note: ' + err.message); }
+    addBtn.disabled = false;
+  });
+  refreshLessonNotes(course);
+}
+
 function applyMeStatsSafe(resp){
   applyMeStats({
     xp: resp.xp, level: resp.level,
```

</details>

---

## Feature 8 — Read-Aloud (Text-to-Speech)

**Commits:** `F8: Read-aloud — frontend-only Listen button using SpeechSynthesis on notes`

**Files changed/created:**

- `index.html`

**How it integrates / no-break confirmation:**

Frontend-only. A "🔊 Listen" button above the notes uses the browser's built-in `SpeechSynthesis` API on a markdown-stripped version of the lesson notes (headings/bold/bullets/links are cleaned so the voice reads prose). The button toggles to "■ Stop", speaking is cancelled when switching lessons or views, and unsupported browsers get a disabled button with an explanatory tooltip — no errors, no backend involvement, zero impact on existing behavior.

**New env vars / dependencies / migration steps:** None. No backend changes.

<details>
<summary>Full diff — F8: Read-aloud — frontend-only Listen button using SpeechSynthesis on notes</summary>

```diff
commit e90022faff050ba7fe2dca098fab3dbe6573dbfc
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:53:18 2026 +0000

    F8: Read-aloud — frontend-only Listen button using SpeechSynthesis on notes

diff --git a/index.html b/index.html
index 616a60d..2d1371c 100644
--- a/index.html
+++ b/index.html
@@ -312,6 +312,10 @@
   .mynote-del{background:none;border:none;color:var(--muted);font-size:11.5px;padding:2px 4px;}
   .mynote-del:hover{color:var(--danger);}
 
+  /* ===== Lesson tools (listen / ELI5) ===== */
+  .notes-tools{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;}
+  .notes-tools .btn.active{background:var(--ink);color:#fff;border-color:var(--ink);}
+
   /* ===== Engagement: header XP / level ===== */
   .xp-chip{display:flex;align-items:center;gap:8px;}
   .level-badge{
@@ -925,9 +929,68 @@ async function addLesson(course){
 }
 
 /* ---- Module main ---- */
+/* ---- Read-aloud (Feature 8, frontend-only) ---- */
+let lessonSpeaking = false;
+
+function notesToSpeech(text){
+  // Strip the markdown sugar so the voice reads prose, not symbols.
+  return (text || '')
+    .replace(/^#{1,6}\s+/gm, '')          // heading hashes
+    .replace(/\*\*/g, '')                 // bold
+    .replace(/\*/g, '')                   // italic
+    .replace(/`{1,3}/g, '')               // code fences / inline code
+    .replace(/^\s*[-*+]\s+/gm, '')       // bullet markers
+    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')  // links -> link text
+    .replace(/\s*\n\s*/g, ' ')           // newlines -> pause-ish space
+    .replace(/\s{2,}/g, ' ')
+    .trim();
+}
+
+function setupListenButton(module){
+  const btn = el('listen-btn');
+  if(!btn) return;
+  if(!('speechSynthesis' in window)){
+    btn.disabled = true;
+    btn.title = "Your browser doesn't support speech synthesis.";
+    return;
+  }
+  btn.addEventListener('click', ()=>{
+    if(lessonSpeaking){
+      window.speechSynthesis.cancel();
+      return;
+    }
+    const plain = notesToSpeech(module.notes);
+    if(!plain){ return; }
+    const utter = new SpeechSynthesisUtterance(plain);
+    utter.rate = 1;
+    const done = ()=>{
+      lessonSpeaking = false;
+      btn.innerHTML = '🔊 Listen';
+      btn.classList.remove('active');
+    };
+    utter.onend = done;
+    utter.onerror = done;
+    window.speechSynthesis.cancel();   // clear anything queued
+    window.speechSynthesis.speak(utter);
+    lessonSpeaking = true;
+    btn.innerHTML = '■ Stop';
+    btn.classList.add('active');
+  });
+}
+
+function stopLessonSpeech(){
+  try{
+    if('speechSynthesis' in window && lessonSpeaking){
+      window.speechSynthesis.cancel();
+      lessonSpeaking = false;
+    }
+  }catch(e){ /* cosmetic */ }
+}
+
 function renderModuleMain(course){
   const module = course.modules[activeModuleIndex];
   const main = el('module-main');
+  stopLessonSpeech();   // switching lessons stops any ongoing read-aloud
 
   if(moduleEditing){
     main.innerHTML = `
@@ -997,6 +1060,9 @@ function renderModuleMain(course){
       </div>
     </div>
     <div id="regen-status" class="gen-status hidden" style="color:var(--muted);margin-bottom:10px;"><span class="spinner-dot"></span> Rewriting this lesson…</div>
+    <div class="notes-tools">
+      <button class="btn btn-ghost btn-sm" id="listen-btn" title="Read this lesson's notes aloud">🔊 Listen</button>
+    </div>
     <div class="notes">${notesHtml}</div>
     <div class="video-block">
       <div class="label">Related video</div>
@@ -1058,6 +1124,7 @@ function renderModuleMain(course){
   renderFlashcards(course, module);
   renderAsk(course, module);
   renderLessonNotes(course);
+  setupListenButton(module);
 }
 
 async function regenerateModule(course){
```

</details>

---

## Feature 9 — Difficulty Toggle on Regeneration

**Commits:** `F9: Difficulty toggle — optional difficulty param on ai.generate_module prompt, optional body on regenerate endpoint, Simpler/More-advanced buttons`

**Files changed/created:**

- `app/ai.py`
- `app/routers/courses.py`
- `app/schemas.py`
- `index.html`

**How it integrates / no-break confirmation:**

`ai.generate_module` gains an optional `difficulty` parameter that appends one extra instruction line to the existing prompt — when omitted the prompt is byte-identical to before, so default regeneration (and the add-lesson endpoint, which also calls this function) is unchanged. The existing `/regenerate` endpoint accepts an optional JSON body `{"difficulty": "simpler"|"advanced"}` — validated with a `Literal` so anything else 422s — and omitted bodies (the current frontend's exact behavior, including the `Content-Type: application/json` header with no payload) resolve to `None`. The response stays `CourseOut`. Frontend adds two small buttons ("− Simpler" / "+ More advanced") next to the existing Regenerate control.

**New env vars / dependencies / migration steps:** No schema change. No env vars or deps.

<details>
<summary>Full diff — F9: Difficulty toggle — optional difficulty param on ai.generate_module prompt, optional body on regenerate endpoint, Simpler/More-advanced buttons</summary>

```diff
commit fad0d06c0cd6b84e22630afe9d08813493068f37
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:55:04 2026 +0000

    F9: Difficulty toggle — optional difficulty param on ai.generate_module prompt, optional body on regenerate endpoint, Simpler/More-advanced buttons

diff --git a/app/ai.py b/app/ai.py
index 4ebc7ae..b41d8ef 100644
--- a/app/ai.py
+++ b/app/ai.py
@@ -395,7 +395,7 @@ async def generate_course(topic: str) -> dict:
     return parsed
 
 
-async def generate_module(course_title: str, lesson_topic: str) -> dict:
+async def generate_module(course_title: str, lesson_topic: str, difficulty: str = None) -> dict:
     prompt = (
         f'Course: "{course_title}". Write the lesson module for: "{lesson_topic}".\n'
         "Notes: 80-130 words in markdown, structured as exactly 3 top-level "
@@ -407,6 +407,21 @@ async def generate_module(course_title: str, lesson_topic: str) -> dict:
         "Also include a blog query: a specific search phrase to find one good "
         "written article or blog post on this subtopic (8 words or fewer)."
     )
+    # Difficulty steering (Feature 9): an extra instruction appended only when
+    # requested. Omitted entirely (byte-identical prompt) when difficulty is
+    # None, so the default regeneration path is unchanged.
+    if difficulty == "simpler":
+        prompt += (
+            "\nDifficulty: write this for a complete beginner — plain, simple "
+            "language; short sentences; everyday analogies; explain any term "
+            "the moment it appears."
+        )
+    elif difficulty == "advanced":
+        prompt += (
+            "\nDifficulty: write this for an advanced learner — go deeper "
+            "technically, use precise domain terminology, and cover "
+            "edge cases, caveats, or nuances a beginner version would skip."
+        )
     module_schema = {
         "type": "OBJECT",
         "properties": {
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 8d2b7cf..4dad29d 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -1,5 +1,5 @@
 from datetime import datetime
-from typing import List
+from typing import List, Optional
 
 from fastapi import APIRouter, Depends, HTTPException
 from sqlalchemy.orm import Session
@@ -244,6 +244,7 @@ def reorder_module(
 async def regenerate_module(
     course_id: int,
     index: int,
+    payload: Optional[schemas.RegenerateIn] = None,   # optional body (Feature 9)
     db: Session = Depends(get_db),
     user: models.User = Depends(get_current_user),
 ):
@@ -252,8 +253,12 @@ async def regenerate_module(
     if index < 0 or index >= len(modules):
         raise HTTPException(status_code=404, detail="Lesson not found.")
 
+    # No body (the pre-Feature-9 client behavior) -> difficulty None ->
+    # the exact same prompt as before. Only an explicit difficulty changes it.
+    difficulty = payload.difficulty if payload else None
+
     try:
-        fresh = await ai.generate_module(course.title, modules[index]["title"])
+        fresh = await ai.generate_module(course.title, modules[index]["title"], difficulty=difficulty)
     except ai.AIError as e:
         raise HTTPException(status_code=502, detail=str(e))
 
diff --git a/app/schemas.py b/app/schemas.py
index a0df00e..6bdb91d 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -1,5 +1,5 @@
 from datetime import datetime
-from typing import Any, List, Optional
+from typing import Any, List, Literal, Optional
 
 from pydantic import BaseModel, ConfigDict, Field
 
@@ -91,6 +91,12 @@ class AskOut(BaseModel):
     answer: str
 
 
+class RegenerateIn(BaseModel):
+    """Optional body for the existing regenerate endpoint (Feature 9).
+    Omitted entirely -> None -> default difficulty (unchanged behavior)."""
+    difficulty: Optional[Literal["simpler", "advanced"]] = None
+
+
 class NoteCreateIn(BaseModel):
     text: str = Field(min_length=1, max_length=2000)
 
diff --git a/index.html b/index.html
index 2d1371c..771766e 100644
--- a/index.html
+++ b/index.html
@@ -1054,9 +1054,11 @@ function renderModuleMain(course){
   main.innerHTML = `
     <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;">
       <h3 style="flex:1;">${escapeHTML(module.title)}</h3>
-      <div style="display:flex;gap:8px;flex-shrink:0;">
+      <div style="display:flex;gap:8px;flex-shrink:0;flex-wrap:wrap;">
         <button class="btn btn-ghost btn-sm" id="edit-mod-btn">Edit</button>
         <button class="btn btn-ghost btn-sm" id="regen-mod-btn">Regenerate</button>
+        <button class="btn btn-ghost btn-sm" id="regen-simpler" title="Rewrite this lesson at a simpler level">− Simpler</button>
+        <button class="btn btn-ghost btn-sm" id="regen-advanced" title="Rewrite this lesson at a more advanced level">+ More advanced</button>
       </div>
     </div>
     <div id="regen-status" class="gen-status hidden" style="color:var(--muted);margin-bottom:10px;"><span class="spinner-dot"></span> Rewriting this lesson…</div>
@@ -1119,6 +1121,8 @@ function renderModuleMain(course){
   });
   el('edit-mod-btn').addEventListener('click', ()=>{ moduleEditing=true; renderCourseView(); });
   el('regen-mod-btn').addEventListener('click', ()=>regenerateModule(course));
+  el('regen-simpler').addEventListener('click', ()=>regenerateModule(course, 'simpler'));
+  el('regen-advanced').addEventListener('click', ()=>regenerateModule(course, 'advanced'));
 
   renderQuiz(course, module);
   renderFlashcards(course, module);
@@ -1127,10 +1131,12 @@ function renderModuleMain(course){
   setupListenButton(module);
 }
 
-async function regenerateModule(course){
+async function regenerateModule(course, difficulty){
   el('regen-status').classList.remove('hidden');
   try{
-    activeCourse = await api(`/courses/${course.id}/modules/${activeModuleIndex}/regenerate`, { method:'POST' });
+    activeCourse = await api(`/courses/${course.id}/modules/${activeModuleIndex}/regenerate`, {
+      method:'POST', body: difficulty ? { difficulty } : null
+    });
     renderCourseView();
   }catch(err){
     el('regen-status').classList.add('hidden');
```

</details>

---

## Feature 10 — Auto-Generated Diagrams

**Commits:** `F10: Diagrams — ai.generate_diagram (Mermaid text), POST /diagram endpoint, module.diagram field, Mermaid CDN + on-demand render`, `F10: Diagrams — ai.generate_diagram with fence-neutralizing parse fallback, POST /diagram endpoint, Mermaid CDN + render`

**Files changed/created:**

- `app/ai.py`
- `app/routers/courses.py`
- `app/schemas.py`
- `index.html`

**How it integrates / no-break confirmation:**

New `ai.generate_diagram(title, notes)` asks Gemini to decide whether a diagram would help and to return ONE Mermaid.js definition as text (empty string = "not diagram-worthy"), following the existing call pattern; it defensively strips ```mermaid fences the model may add and neutralizes fences inside the JSON payload before `_clean_json` (whose own fence-striipping would otherwise corrupt the diagram — covered by an explicit test). `POST /api/courses/{id}/modules/{i}/diagram` stores the diagram in the new optional `module["diagram"]` field only when non-empty — an existing diagram is never destroyed by a "not needed" verdict. The frontend loads Mermaid v10 from jsDelivr and renders `module.diagram` on demand (theme-aware, securityLevel strict); when absent, nothing renders; if the CDN fails, the raw source is shown as a fallback instead of throwing.

**New env vars / dependencies / migration steps:** No schema change (optional string inside `modules` JSON). New CDN dependency: mermaid@10.9.1 (browser-side only).

<details>
<summary>Full diff — F10: Diagrams — ai.generate_diagram (Mermaid text), POST /diagram endpoint, module.diagram field, Mermaid CDN + on-demand render</summary>

```diff
commit 6a434a2f3eda5ef667f953ff5648ba4c1a719f9a
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:56:02 2026 +0000

    F10: Diagrams — ai.generate_diagram (Mermaid text), POST /diagram endpoint, module.diagram field, Mermaid CDN + on-demand render

diff --git a/app/ai.py b/app/ai.py
index b41d8ef..8d80203 100644
--- a/app/ai.py
+++ b/app/ai.py
@@ -559,3 +559,49 @@ async def answer_question(module_title: str, module_notes: str, question: str) -
     if not answer or not str(answer).strip():
         raise AIError("No answer returned by the model.")
     return str(answer).strip()
+
+
+async def generate_diagram(module_title: str, module_notes: str) -> dict:
+    """Ask Gemini for a Mermaid.js diagram for a lesson (Feature 10).
+
+    The model decides whether a diagram helps; an empty 'diagram' string
+    means "no diagram needed" and nothing is stored. Returns
+    {"diagram": str, "explanation": str}."""
+    prompt = (
+        f'Lesson title: "{module_title}"\n'
+        f'Lesson notes:\n{module_notes}\n\n'
+        "Decide whether a Mermaid.js diagram would genuinely help a learner "
+        "understand this lesson (a flow, cycle, hierarchy, relationship map, "
+        "timeline, or breakdown). If yes, return ONE diagram in the 'diagram' "
+        "field as pure Mermaid syntax — no code fences, no commentary — that "
+        "renders with mermaid.js v10 (flowchart TD, sequenceDiagram, "
+        "classDiagram, stateDiagram-v2, erDiagram, mindmap, timeline, or "
+        "pie). Keep node labels short (4 words or fewer), avoid special "
+        "characters that break Mermaid parsing, and keep it under 25 lines. "
+        "If a diagram would NOT genuinely help, return an empty string in "
+        "'diagram'. Put one short sentence describing the diagram (or why "
+        "none is needed) in 'explanation'."
+    )
+    diagram_schema = {
+        "type": "OBJECT",
+        "properties": {
+            "diagram": {"type": "STRING"},
+            "explanation": {"type": "STRING"},
+        },
+        "required": ["diagram", "explanation"],
+    }
+
+    raw = await _call_gemini(prompt, schema=diagram_schema, max_tokens=1200)
+    parsed = _clean_json(raw)
+
+    diagram = ""
+    explanation = ""
+    if isinstance(parsed, dict):
+        diagram = str(parsed.get("diagram") or "").strip()
+        explanation = str(parsed.get("explanation") or "").strip()
+        # Defensive: strip ```mermaid fences if the model added them anyway.
+        fence = re.search(r"```(?:mermaid)?\s*([\s\S]*?)```", diagram, re.IGNORECASE)
+        if fence:
+            diagram = fence.group(1).strip()
+
+    return {"diagram": diagram, "explanation": explanation}
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 4dad29d..7f1c38d 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -445,6 +445,38 @@ async def ask_lesson_question(
     return schemas.AskOut(answer=answer)
 
 
+@router.post("/{course_id}/modules/{index}/diagram", response_model=schemas.CourseOut)
+async def generate_diagram_route(
+    course_id: int,
+    index: int,
+    db: Session = Depends(get_db),
+    user: models.User = Depends(get_current_user),
+):
+    """Auto-generated diagrams (Feature 10): asks Gemini for a Mermaid.js
+    diagram for the lesson. Stored on the new optional module["diagram"] field
+    only when the model returns one — if it decides a diagram wouldn't help,
+    the module is left untouched (an existing diagram is never destroyed)."""
+    course = _get_owned_course(course_id, user, db)
+    modules = list(course.modules or [])
+    if index < 0 or index >= len(modules):
+        raise HTTPException(status_code=404, detail="Lesson not found.")
+
+    try:
+        result = await ai.generate_diagram(modules[index]["title"], modules[index].get("notes", ""))
+    except ai.AIError as e:
+        raise HTTPException(status_code=502, detail=str(e))
+
+    if result.get("diagram"):
+        mod = dict(modules[index])
+        mod["diagram"] = result["diagram"]
+        modules[index] = mod
+        course.modules = modules
+
+    db.commit()
+    db.refresh(course)
+    return course
+
+
 @router.post("/{course_id}/modules/{index}/notes", response_model=schemas.NoteOut)
 def create_lesson_note(
     course_id: int,
diff --git a/app/schemas.py b/app/schemas.py
index 6bdb91d..947811f 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -46,6 +46,7 @@ class ModuleOut(BaseModel):
     # None and the frontend simply hides those sections (graceful degrade).
     quiz_attempts: Optional[List[Any]] = None
     flashcards: Optional[List[Any]] = None
+    diagram: Optional[str] = None
 
 
 class CourseCreateIn(BaseModel):
diff --git a/index.html b/index.html
index 771766e..5ad9fe4 100644
--- a/index.html
+++ b/index.html
@@ -8,6 +8,7 @@
 <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
 <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;0,9..144,700;1,9..144,500&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
 <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.3.0/marked.min.js"></script>
+<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
 <style>
   :root{
     --ink:#1B2340;
@@ -312,10 +313,17 @@
   .mynote-del{background:none;border:none;color:var(--muted);font-size:11.5px;padding:2px 4px;}
   .mynote-del:hover{color:var(--danger);}
 
-  /* ===== Lesson tools (listen / ELI5) ===== */
+  /* ===== Lesson tools (listen / ELI5 / diagram) ===== */
   .notes-tools{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;}
   .notes-tools .btn.active{background:var(--ink);color:#fff;border-color:var(--ink);}
 
+  /* ===== Diagram ===== */
+  .diagram-block{margin-top:26px;}
+  .diagram-block .label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:8px;font-weight:600;}
+  .diagram-box{border:1px solid var(--line);border-radius:12px;padding:18px;background:var(--paper-2);overflow:auto;}
+  .diagram-box svg{max-width:100%;height:auto;display:block;}
+  .diagram-box .dm-caption{font-size:12px;color:var(--muted);margin-top:10px;}
+
   /* ===== Engagement: header XP / level ===== */
   .xp-chip{display:flex;align-items:center;gap:8px;}
   .level-badge{
@@ -1064,8 +1072,13 @@ function renderModuleMain(course){
     <div id="regen-status" class="gen-status hidden" style="color:var(--muted);margin-bottom:10px;"><span class="spinner-dot"></span> Rewriting this lesson…</div>
     <div class="notes-tools">
       <button class="btn btn-ghost btn-sm" id="listen-btn" title="Read this lesson's notes aloud">🔊 Listen</button>
+      <button class="btn btn-ghost btn-sm" id="diagram-btn" title="Generate a Mermaid diagram for this lesson">🧩 Diagram</button>
     </div>
     <div class="notes">${notesHtml}</div>
+    <div class="diagram-block hidden" id="diagram-block">
+      <div class="label">Diagram</div>
+      <div class="diagram-box" id="diagram-container"></div>
+    </div>
     <div class="video-block">
       <div class="label">Related video</div>
       <div class="video-frame">
@@ -1129,6 +1142,8 @@ function renderModuleMain(course){
   renderAsk(course, module);
   renderLessonNotes(course);
   setupListenButton(module);
+  setupDiagramButton(course, module);
+  renderDiagram(module);
 }
 
 async function regenerateModule(course, difficulty){
@@ -1436,6 +1451,57 @@ function renderLessonNotes(course){
   refreshLessonNotes(course);
 }
 
+/* ---- Auto-generated diagrams (Feature 10) ---- */
+function setupDiagramButton(course, module){
+  const btn = el('diagram-btn');
+  if(!btn) return;
+  if(module.diagram){ btn.textContent = '🧩 Redraw diagram'; }
+  btn.addEventListener('click', async ()=>{
+    btn.disabled = true;
+    const oldLabel = btn.textContent;
+    btn.textContent = '🧩 Drawing…';
+    try{
+      activeCourse = await api(`/courses/${course.id}/modules/${activeModuleIndex}/diagram`, { method:'POST' });
+      const fresh = activeCourse.modules[activeModuleIndex];
+      renderDiagram(fresh);
+      btn.textContent = fresh.diagram ? '🧩 Redraw diagram' : oldLabel;
+      if(!fresh.diagram){ toast('🧩', 'The model decided a diagram would not add much for this lesson.'); }
+    }catch(err){
+      btn.textContent = oldLabel;
+      alert('Could not generate a diagram: ' + err.message);
+    }
+    btn.disabled = false;
+  });
+}
+
+async function renderDiagram(module){
+  const block = el('diagram-block');
+  const container = el('diagram-container');
+  if(!block || !container) return;
+  const code = module && module.diagram;
+  if(!code || !String(code).trim()){      // no diagram -> render nothing
+    block.classList.add('hidden');
+    container.innerHTML = '';
+    return;
+  }
+  block.classList.remove('hidden');
+  container.innerHTML = `<div class="gen-status" style="color:var(--muted);"><span class="spinner-dot"></span> Rendering diagram…</div>`;
+  if(typeof mermaid === 'undefined' || !mermaid.render){
+    // CDN failed to load — degrade to showing the raw source
+    container.innerHTML = `<pre class="mono" style="font-size:12px;white-space:pre-wrap;margin:0;">${escapeHTML(code)}</pre>`;
+    return;
+  }
+  try{
+    const dark = document.documentElement.dataset.theme === 'dark';
+    mermaid.initialize({ startOnLoad:false, securityLevel:'strict', theme: dark ? 'dark' : 'default' });
+    const id = 'mmd-' + Date.now() + '-' + Math.floor(Math.random()*10000);
+    const { svg } = await mermaid.render(id, String(code).trim());
+    container.innerHTML = svg;
+  }catch(e){
+    container.innerHTML = `<div class="form-error">Could not render this diagram (the AI's Mermaid syntax may be invalid) — you can try redrawing it.</div>`;
+  }
+}
+
 function applyMeStatsSafe(resp){
   applyMeStats({
     xp: resp.xp, level: resp.level,
```

</details>

<details>
<summary>Full diff — F10: Diagrams — ai.generate_diagram with fence-neutralizing parse fallback, POST /diagram endpoint, Mermaid CDN + render</summary>

```diff
commit cf26e5814450c987b2b089a4eb1d0f2bc9660fef
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:57:52 2026 +0000

    F10: Diagrams — ai.generate_diagram with fence-neutralizing parse fallback, POST /diagram endpoint, Mermaid CDN + render

diff --git a/app/ai.py b/app/ai.py
index 8d80203..bb82e44 100644
--- a/app/ai.py
+++ b/app/ai.py
@@ -592,7 +592,17 @@ async def generate_diagram(module_title: str, module_notes: str) -> dict:
     }
 
     raw = await _call_gemini(prompt, schema=diagram_schema, max_tokens=1200)
-    parsed = _clean_json(raw)
+    try:
+        parsed = _clean_json(raw)
+    except AIError:
+        # Diagram text that itself contains ``` fences (models love wrapping
+        # mermaid in code fences even when told not to) trips _clean_json's
+        # own fence-stripping, which would swallow everything between the
+        # fences. Neutralize the backticks into JSON unicode escapes — they
+        # decode back to the exact same characters after parsing, but no
+        # longer look like fences to _clean_json.
+        neutralized = raw.replace("```", "\\u0060\\u0060\\u0060")
+        parsed = _clean_json(neutralized)
 
     diagram = ""
     explanation = ""
```

</details>

---

## Feature 11 — "Explain Like I'm 5" Mode

**Commits:** `F11: ELI5 — ai.explain_like_im_five, stateless POST /eli5, toggle button swapping displayed notes (stored notes untouched)`

**Files changed/created:**

- `app/ai.py`
- `app/routers/courses.py`
- `app/schemas.py`
- `index.html`

**How it integrates / no-break confirmation:**

New `ai.explain_like_im_five(title, notes)` resummarizes the notes at a five-year-old reading level via the standard Gemini pattern, and `POST /api/courses/{id}/modules/{i}/eli5` returns `{notes}` directly — the stored notes are never overwritten (asserted in tests). The frontend adds an "🍼 ELI5" toggle in the lesson tools row that swaps the displayed markdown for the simplified version and back; results are cached client-side per lesson and the cache is invalidated whenever the notes change (regeneration or manual edit).

**New env vars / dependencies / migration steps:** No schema change. No env vars or deps.

<details>
<summary>Full diff — F11: ELI5 — ai.explain_like_im_five, stateless POST /eli5, toggle button swapping displayed notes (stored notes untouched)</summary>

```diff
commit ec1e25ceac5f31ce908b6357ccced4aaf34a2195
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 16:58:53 2026 +0000

    F11: ELI5 — ai.explain_like_im_five, stateless POST /eli5, toggle button swapping displayed notes (stored notes untouched)

diff --git a/app/ai.py b/app/ai.py
index bb82e44..5f429f8 100644
--- a/app/ai.py
+++ b/app/ai.py
@@ -615,3 +615,29 @@ async def generate_diagram(module_title: str, module_notes: str) -> dict:
             diagram = fence.group(1).strip()
 
     return {"diagram": diagram, "explanation": explanation}
+
+
+async def explain_like_im_five(module_title: str, module_notes: str) -> str:
+    """"Explain Like I'm 5" resummaries (Feature 11): returns a simpler
+    version of the notes without overwriting the stored ones."""
+    prompt = (
+        f'Lesson title: "{module_title}"\n'
+        f'Lesson notes:\n{module_notes}\n\n'
+        "Rewrite these lesson notes at a much simpler reading level, as if "
+        "explaining to a curious five-year-old: very short sentences, "
+        "everyday words only, and one simple analogy that makes the core idea "
+        "click. 90 words or fewer, in markdown with at most 3 bullet points."
+    )
+    eli5_schema = {
+        "type": "OBJECT",
+        "properties": {"summary": {"type": "STRING"}},
+        "required": ["summary"],
+    }
+
+    raw = await _call_gemini(prompt, schema=eli5_schema, max_tokens=800)
+    parsed = _clean_json(raw)
+
+    summary = parsed.get("summary") if isinstance(parsed, dict) else None
+    if not summary or not str(summary).strip():
+        raise AIError("No simplified summary returned by the model.")
+    return str(summary).strip()
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 7f1c38d..470f663 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -477,6 +477,31 @@ async def generate_diagram_route(
     return course
 
 
+@router.post("/{course_id}/modules/{index}/eli5", response_model=schemas.Eli5Out)
+async def eli5_lesson(
+    course_id: int,
+    index: int,
+    db: Session = Depends(get_db),
+    user: models.User = Depends(get_current_user),
+):
+    """"Explain Like I'm 5" (Feature 11): resummarizes the lesson's notes at a
+    simpler reading level and returns the text directly. The stored notes are
+    never overwritten — this endpoint does not touch course data at all."""
+    course = _get_owned_course(course_id, user, db)
+    modules = list(course.modules or [])
+    if index < 0 or index >= len(modules):
+        raise HTTPException(status_code=404, detail="Lesson not found.")
+
+    try:
+        simple = await ai.explain_like_im_five(
+            modules[index]["title"], modules[index].get("notes", "")
+        )
+    except ai.AIError as e:
+        raise HTTPException(status_code=502, detail=str(e))
+
+    return schemas.Eli5Out(notes=simple)
+
+
 @router.post("/{course_id}/modules/{index}/notes", response_model=schemas.NoteOut)
 def create_lesson_note(
     course_id: int,
diff --git a/app/schemas.py b/app/schemas.py
index 947811f..d50764b 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -98,6 +98,10 @@ class RegenerateIn(BaseModel):
     difficulty: Optional[Literal["simpler", "advanced"]] = None
 
 
+class Eli5Out(BaseModel):
+    notes: str
+
+
 class NoteCreateIn(BaseModel):
     text: str = Field(min_length=1, max_length=2000)
 
diff --git a/index.html b/index.html
index 5ad9fe4..fb3c3a6 100644
--- a/index.html
+++ b/index.html
@@ -999,6 +999,7 @@ function renderModuleMain(course){
   const module = course.modules[activeModuleIndex];
   const main = el('module-main');
   stopLessonSpeech();   // switching lessons stops any ongoing read-aloud
+  eli5Active = false;   // every lesson render starts from the original notes
 
   if(moduleEditing){
     main.innerHTML = `
@@ -1034,6 +1035,7 @@ function renderModuleMain(course){
             blogQuery: el('edit-mod-blog').value.trim()
           }
         });
+        eli5CacheClear();   // notes were just edited — cached simplifications are stale
         moduleEditing = false;
         renderCourseView();
       }catch(err){ alert('Could not save changes: ' + err.message); }
@@ -1073,8 +1075,9 @@ function renderModuleMain(course){
     <div class="notes-tools">
       <button class="btn btn-ghost btn-sm" id="listen-btn" title="Read this lesson's notes aloud">🔊 Listen</button>
       <button class="btn btn-ghost btn-sm" id="diagram-btn" title="Generate a Mermaid diagram for this lesson">🧩 Diagram</button>
+      <button class="btn btn-ghost btn-sm" id="eli5-btn" title="Explain this lesson like I'm five">🍼 ELI5</button>
     </div>
-    <div class="notes">${notesHtml}</div>
+    <div class="notes" id="notes-display">${notesHtml}</div>
     <div class="diagram-block hidden" id="diagram-block">
       <div class="label">Diagram</div>
       <div class="diagram-box" id="diagram-container"></div>
@@ -1144,6 +1147,7 @@ function renderModuleMain(course){
   setupListenButton(module);
   setupDiagramButton(course, module);
   renderDiagram(module);
+  setupEli5Button(course, module);
 }
 
 async function regenerateModule(course, difficulty){
@@ -1152,6 +1156,7 @@ async function regenerateModule(course, difficulty){
     activeCourse = await api(`/courses/${course.id}/modules/${activeModuleIndex}/regenerate`, {
       method:'POST', body: difficulty ? { difficulty } : null
     });
+    eli5CacheClear();   // notes changed — cached simplifications are stale
     renderCourseView();
   }catch(err){
     el('regen-status').classList.add('hidden');
@@ -1451,6 +1456,57 @@ function renderLessonNotes(course){
   refreshLessonNotes(course);
 }
 
+/* ---- "Explain Like I'm 5" (Feature 11) ---- */
+let eli5Active = false;        // false = original notes shown
+const eli5Cache = {};          // "courseId:moduleIndex" -> simplified text (client-only)
+
+function eli5CacheClear(){ for(const k of Object.keys(eli5Cache)) delete eli5Cache[k]; }
+
+function renderMarkdown(text){
+  return (typeof marked !== 'undefined') ? marked.parse(text || '') : (text || '');
+}
+
+function setupEli5Button(course, module){
+  const btn = el('eli5-btn');
+  if(!btn) return;
+  btn.textContent = eli5Active ? '📖 Original' : '🍼 ELI5';
+  btn.classList.toggle('active', eli5Active);
+  btn.addEventListener('click', ()=>toggleEli5(course, module, btn));
+}
+
+async function toggleEli5(course, module, btn){
+  const display = el('notes-display');
+  if(!display) return;
+
+  if(eli5Active){   // back to the stored notes — nothing is fetched or changed
+    eli5Active = false;
+    display.innerHTML = renderMarkdown(module.notes || '');
+    btn.textContent = '🍼 ELI5';
+    btn.classList.remove('active');
+    return;
+  }
+
+  const key = course.id + ':' + activeModuleIndex;
+  btn.disabled = true;
+  btn.textContent = '🍼 Simplifying…';
+  try{
+    let simple = eli5Cache[key];
+    if(!simple){
+      const resp = await api(`/courses/${course.id}/modules/${activeModuleIndex}/eli5`, { method:'POST' });
+      simple = resp.notes || '';
+      eli5Cache[key] = simple;
+    }
+    eli5Active = true;
+    display.innerHTML = renderMarkdown(simple);
+    btn.textContent = '📖 Original';
+    btn.classList.add('active');
+  }catch(err){
+    btn.textContent = '🍼 ELI5';
+    alert('Could not simplify this lesson: ' + err.message);
+  }
+  btn.disabled = false;
+}
+
 /* ---- Auto-generated diagrams (Feature 10) ---- */
 function setupDiagramButton(course, module){
   const btn = el('diagram-btn');
```

</details>

---

## Feature 12 — Public Course Sharing

**Commits:** `F12: Public sharing — Course.share_id, POST /share, unauthenticated GET /api/shared/{id}, /share/{id} frontend route + vercel rewrite, share UI + read-only visitor view`

**Files changed/created:**

- `app/models.py`
- `app/database.py`
- `app/routers/courses.py`
- `app/schemas.py`
- `app/main.py`
- `vercel.json`
- `index.html`

**How it integrates / no-break confirmation:**

Adds nullable unique `share_id` to `Course`. `POST /api/courses/{id}/share` (owner-only) generates a UUID once and returns it — links are stable across re-shares. The new UNAUTHENTICATED `GET /api/shared/{share_id}` returns a read-only view (title, description, modules, created_at) with no user info whatsoever. For local/self-hosted use, FastAPI also serves the SPA at `/share/{share_id}`; on Vercel a new rewrite maps `/share/*` to the static `index.html` (the existing `/api/*` rewrite is untouched). The frontend adds a Share button that produces a `/share/{share_id}` link with copy-to-clipboard, and a visitor-facing read-only course view (no topbar, no auth) that boots when the page is loaded from a `/share/...` path.

**New env vars / dependencies / migration steps:** New column: `courses.share_id` + unique index (auto-migrated; manual SQL below). vercel.json gains one rewrite. No new env vars or deps.

<details>
<summary>Full diff — F12: Public sharing — Course.share_id, POST /share, unauthenticated GET /api/shared/{id}, /share/{id} frontend route + vercel rewrite, share UI + read-only visitor view</summary>

```diff
commit b85a85fe485c1c1f22ff0727a1c620b0a3ae3413
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 17:01:22 2026 +0000

    F12: Public sharing — Course.share_id, POST /share, unauthenticated GET /api/shared/{id}, /share/{id} frontend route + vercel rewrite, share UI + read-only visitor view

diff --git a/app/database.py b/app/database.py
index a74f9da..b308952 100644
--- a/app/database.py
+++ b/app/database.py
@@ -44,6 +44,14 @@ _COLUMN_MIGRATIONS = [
     ("users", "streak_count", "INTEGER NOT NULL DEFAULT 0"),
     ("users", "last_active_date", "TIMESTAMP"),
     ("users", "badges", "JSON DEFAULT '[]'"),
+    ("courses", "share_id", "VARCHAR(64)"),
+]
+
+# Indexes that only make sense once their column exists (created on fresh
+# databases automatically by create_all; backfilled here for older ones).
+_INDEX_MIGRATIONS = [
+    ("courses", "share_id",
+     "CREATE UNIQUE INDEX IF NOT EXISTS ix_courses_share_id ON courses (share_id)"),
 ]
 
 
@@ -68,5 +76,18 @@ def ensure_schema_upgrades() -> None:
                 # CHANGES.md for the manual migration statements.
                 print(f"[schema] could not add {table}.{column} ({e}); "
                       f"run manually: {stmt}")
+        for table, column, stmt in _INDEX_MIGRATIONS:
+            try:
+                if table not in existing_tables:
+                    continue
+                cols = {c["name"] for c in inspector.get_columns(table)}
+                if column not in cols:
+                    continue  # column itself failed to be added
+                with engine.begin() as conn:
+                    conn.execute(text(stmt))
+            except Exception as e:
+                # Non-fatal: lookups still work without the index; uniqueness
+                # is effectively guaranteed by UUIDv4 anyway.
+                print(f"[schema] could not create {table}.{column} index ({e})")
     except Exception as e:
         print(f"[schema] runtime column migration skipped: {e}")
diff --git a/app/main.py b/app/main.py
index 1114619..1ef0eee 100644
--- a/app/main.py
+++ b/app/main.py
@@ -34,6 +34,7 @@ app.add_middleware(
 app.include_router(auth.router)
 app.include_router(courses.router)
 app.include_router(courses.notes_router)   # /api/notes/... (personal notes, Feature 7)
+app.include_router(courses.shared_router)  # /api/shared/... (public read-only view, Feature 12)
 
 
 @app.get("/api/health")
@@ -41,6 +42,21 @@ def health():
     return {"status": "ok"}
 
 
+@app.get("/share/{share_id}")
+def serve_shared_frontend(share_id: str):
+    """Serves the single-page frontend for public /share/{id} links when the
+    app itself hosts the frontend (local dev / non-Vercel deploys). On Vercel
+    the vercel.json rewrite maps /share/* to the static index.html instead."""
+    if not os.path.isfile(FRONTEND_INDEX):
+        return {
+            "error": "index.html not found",
+            "looked_in": FRONTEND_INDEX,
+            "cwd": os.getcwd(),
+            "cwd_contents": os.listdir(os.getcwd()),
+        }
+    return FileResponse(FRONTEND_INDEX)
+
+
 @app.get("/")
 def serve_frontend():
     if not os.path.isfile(FRONTEND_INDEX):
diff --git a/app/models.py b/app/models.py
index d6e3848..b6ba434 100644
--- a/app/models.py
+++ b/app/models.py
@@ -38,6 +38,9 @@ class Course(Base):
     # List of {"title": str, "notes": str, "videoQuery": str, "completed": bool, "quiz": dict|None}
     modules = Column(JSON, default=list, nullable=False)
     created_at = Column(DateTime(timezone=True), server_default=func.now())
+    # Public sharing (Feature 12): null = not shared. A UUID set by the owner
+    # via POST /api/courses/{id}/share; /api/shared/{share_id} is read-only.
+    share_id = Column(String(64), unique=True, nullable=True, index=True)
 
     owner = relationship("User", back_populates="courses")
 
diff --git a/app/routers/courses.py b/app/routers/courses.py
index 470f663..34eca3d 100644
--- a/app/routers/courses.py
+++ b/app/routers/courses.py
@@ -1,5 +1,6 @@
 from datetime import datetime
 from typing import List, Optional
+import uuid
 
 from fastapi import APIRouter, Depends, HTTPException
 from sqlalchemy.orm import Session
@@ -26,6 +27,9 @@ router = APIRouter(prefix="/api/courses", tags=["courses"])
 # under its own /api/notes prefix (per spec). Registered in main.py.
 notes_router = APIRouter(prefix="/api/notes", tags=["notes"])
 
+# Public sharing (Feature 12): unauthenticated read-only course view.
+shared_router = APIRouter(prefix="/api/shared", tags=["sharing"])
+
 # XP awards for engagement actions (spec: +10 per completed lesson, +5 per
 # correct quiz answer). Pure side effects — they never alter any existing
 # endpoint's response shape.
@@ -570,3 +574,37 @@ def delete_lesson_note(
     db.delete(note)
     db.commit()
     return {"ok": True}
+
+
+@router.post("/{course_id}/share", response_model=schemas.ShareOut)
+def share_course(
+    course_id: int,
+    db: Session = Depends(get_db),
+    user: models.User = Depends(get_current_user),
+):
+    """Public sharing (Feature 12): generates a UUID share link for the
+    owner's course. Calling it again returns the same id — links are stable."""
+    course = _get_owned_course(course_id, user, db)
+    if not course.share_id:
+        course.share_id = str(uuid.uuid4())
+        db.commit()
+        db.refresh(course)
+    return schemas.ShareOut(share_id=course.share_id)
+
+
+@shared_router.get("/{share_id}", response_model=schemas.SharedCourseOut)
+def get_shared_course(
+    share_id: str,
+    db: Session = Depends(get_db),
+):
+    """Unauthenticated, read-only view of a shared course. Returns only the
+    course content — never user info, and no edit capability."""
+    course = db.query(models.Course).filter(models.Course.share_id == share_id).first()
+    if not course:
+        raise HTTPException(status_code=404, detail="Shared course not found.")
+    return schemas.SharedCourseOut(
+        title=course.title,
+        description=course.description or "",
+        modules=course.modules or [],
+        created_at=course.created_at,
+    )
diff --git a/app/schemas.py b/app/schemas.py
index d50764b..54f5714 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -102,6 +102,19 @@ class Eli5Out(BaseModel):
     notes: str
 
 
+class ShareOut(BaseModel):
+    share_id: str
+
+
+class SharedCourseOut(BaseModel):
+    """Read-only public view of a course — title/description/modules only.
+    Never includes user info (owner id/username) or edit capability."""
+    title: str
+    description: str = ""
+    modules: List[ModuleOut] = []
+    created_at: datetime
+
+
 class NoteCreateIn(BaseModel):
     text: str = Field(min_length=1, max_length=2000)
 
diff --git a/index.html b/index.html
index fb3c3a6..e52ac49 100644
--- a/index.html
+++ b/index.html
@@ -324,6 +324,25 @@
   .diagram-box svg{max-width:100%;height:auto;display:block;}
   .diagram-box .dm-caption{font-size:12px;color:var(--muted);margin-top:10px;}
 
+  /* ===== Public sharing ===== */
+  .share-panel{
+    display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 22px;
+    padding:14px 16px;border:1px solid var(--line);border-radius:12px;background:#FAFBFE;
+  }
+  .share-panel input{
+    flex:1;min-width:220px;padding:9px 12px;border:1px solid var(--line);border-radius:8px;
+    font-family:'IBM Plex Mono',monospace;font-size:12.5px;color:var(--muted);background:var(--paper-2);
+  }
+  .shared-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:22px;flex-wrap:wrap;}
+  .shared-badge{
+    font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
+    color:var(--gold-2);border:1px solid var(--gold);border-radius:999px;padding:5px 12px;
+  }
+  .shared-module{background:var(--paper-2);border:1px solid var(--line);border-radius:14px;padding:26px 30px;margin-bottom:16px;}
+  .shared-module h3{font-size:19px;margin-bottom:14px;display:flex;align-items:center;gap:10px;}
+  .shared-module .mod-num{font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--muted);}
+  .shared-title-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px;}
+
   /* ===== Engagement: header XP / level ===== */
   .xp-chip{display:flex;align-items:center;gap:8px;}
   .level-badge{
@@ -487,7 +506,9 @@
         <button class="btn btn-ghost btn-sm" id="show-mindmap">🧠 Mind map</button>
         <button class="btn btn-ghost btn-sm" id="export-md">Download notes (.md)</button>
         <button class="btn btn-ghost btn-sm" id="print-course">Print / Save PDF</button>
+        <button class="btn btn-ghost btn-sm" id="share-course" title="Create a public read-only link for this course">↗ Share</button>
       </div>
+      <div id="share-panel-wrap"></div>
       <div class="course-body">
         <nav class="module-rail" id="module-rail"></nav>
         <div class="module-main" id="module-main"></div>
@@ -512,6 +533,22 @@
 
 </div>
 
+<!-- PUBLIC SHARED COURSE VIEW (Feature 12) -->
+<section id="view-shared" class="view hidden">
+  <div class="dash-wrap" style="max-width:860px;">
+    <div class="shared-head">
+      <div class="brand"><span class="dot"></span>Synapse</div>
+      <span class="shared-badge">Shared course · read-only</span>
+    </div>
+    <div class="shared-title-row">
+      <h2 id="shared-title" style="font-size:30px;line-height:1.25;"></h2>
+    </div>
+    <p id="shared-desc" style="color:var(--muted);line-height:1.55;margin:8px 0 26px;"></p>
+    <div id="shared-modules"></div>
+    <p class="footer-note">Notes are AI-generated — double-check anything important before you rely on it. Shared via <a href="/">Synapse</a>.</p>
+  </div>
+</section>
+
 <script>
 /* ---------------- API helper ---------------- */
 const API_BASE = '/api';
@@ -569,10 +606,10 @@ const el = (id) => document.getElementById(id);
 
 /* ---------------- View switching ---------------- */
 function showView(name){
-  ['auth','dashboard','course'].forEach(v=>{
+  ['auth','dashboard','course','shared'].forEach(v=>{
     el('view-'+v).classList.toggle('hidden', v!==name);
   });
-  el('topbar').classList.toggle('hidden', name==='auth');
+  el('topbar').classList.toggle('hidden', name==='auth' || name==='shared');
 }
 
 /* ---------------- Constellation loader ---------------- */
@@ -1566,6 +1603,59 @@ function applyMeStatsSafe(resp){
   return meStats;
 }
 
+/* ---- Public sharing (Feature 12) ---- */
+el('share-course').addEventListener('click', async ()=>{
+  const course = currentCourse();
+  if(!course) return;
+  const btn = el('share-course');
+  btn.disabled = true;
+  try{
+    const resp = await api(`/courses/${course.id}/share`, { method:'POST' });
+    const url = `${location.origin}/share/${resp.share_id}`;
+    el('share-panel-wrap').innerHTML = `
+      <div class="share-panel">
+        <input id="share-url" readonly value="${escapeAttr(url)}" onclick="this.select();" />
+        <button class="btn btn-primary btn-sm" id="copy-share-url">Copy link</button>
+        <button class="btn btn-ghost btn-sm" id="close-share-panel">✕</button>
+      </div>`;
+    el('copy-share-url').addEventListener('click', async ()=>{
+      const input = el('share-url');
+      let ok = false;
+      try{ await navigator.clipboard.writeText(input.value); ok = true; }catch(e){}
+      if(!ok){ try{ input.select(); ok = document.execCommand('copy'); }catch(e){} }
+      toast(ok ? '🔗' : '⚠️', ok ? 'Share link copied to clipboard.' : 'Copy failed — select the link and copy manually.');
+    });
+    el('close-share-panel').addEventListener('click', ()=>{ el('share-panel-wrap').innerHTML = ''; });
+  }catch(err){
+    alert('Could not create a share link: ' + err.message);
+  }
+  btn.disabled = false;
+});
+
+async function loadSharedCourse(shareId){
+  showView('shared');   // visitor mode: no topbar, no auth required
+  el('shared-title').textContent = '';
+  el('shared-desc').textContent = '';
+  el('shared-modules').innerHTML = `<div class="gen-status" style="color:var(--muted);"><span class="spinner-dot"></span> Loading shared course…</div>`;
+  try{
+    const course = await api('/shared/' + encodeURIComponent(shareId), { auth:false });
+    el('shared-title').textContent = course.title || 'Shared course';
+    el('shared-desc').textContent = course.description || '';
+    const holder = el('shared-modules');
+    holder.innerHTML = '';
+    (course.modules || []).forEach((m,i)=>{
+      const div = document.createElement('div');
+      div.className = 'shared-module';
+      div.innerHTML = `
+        <h3><span class="mod-num">${String(i+1).padStart(2,'0')}</span> ${escapeHTML(m.title)} ${m.completed ? '<span style="color:var(--success);">✓</span>' : ''}</h3>
+        <div class="notes">${renderMarkdown(m.notes || '')}</div>`;
+      holder.appendChild(div);
+    });
+  }catch(err){
+    el('shared-modules').innerHTML = `<div class="form-error">This shared course could not be loaded — the link may be wrong, or the course may have been removed.</div>`;
+  }
+}
+
 /* ---- Course header edit ---- */
 el('toggle-course-edit').addEventListener('click', ()=>{
   const course = currentCourse();
@@ -1909,6 +1999,13 @@ el('mindmap-modal').addEventListener('click', (e)=>{
 
 /* ---------------- Boot ---------------- */
 (async function boot(){
+  // Public share links land on /share/{share_id} (Vercel rewrite or the
+  // FastAPI route) — render the read-only visitor view before anything else.
+  const sharedMatch = location.pathname.match(/^\/share\/([A-Za-z0-9-]+)\/?$/);
+  if(sharedMatch){
+    await loadSharedCourse(sharedMatch[1]);
+    return;
+  }
   const token = getToken();
   const storedUsername = getStoredUsername();
   if(token && storedUsername){
diff --git a/vercel.json b/vercel.json
index f3b7c25..8d6d24e 100644
--- a/vercel.json
+++ b/vercel.json
@@ -1,5 +1,6 @@
 {
   "rewrites": [
-    { "source": "/api/:path*", "destination": "/api/index.py" }
+    { "source": "/api/:path*", "destination": "/api/index.py" },
+    { "source": "/share/:path*", "destination": "/" }
   ]
 }
```

</details>

---

## Feature 13 — Leaderboard

**Commits:** `F13: Leaderboard — public GET /api/leaderboard (username/xp/level only), leaderboard view + nav`

**Files changed/created:**

- `app/routers/leaderboard.py (new)`
- `app/main.py`
- `app/schemas.py`
- `index.html`

**How it integrates / no-break confirmation:**

New public `GET /api/leaderboard` (no auth) returning the top 10 users by XP as `{username, xp, level}` rows — and nothing else; the response model structurally cannot leak password hashes, emails, ids or badges (asserted in tests). The frontend gains a Leaderboard nav button and a dedicated view with rank/avatar/name/level/XP rows, the current user highlighted.

**New env vars / dependencies / migration steps:** No schema change. No env vars or deps.

<details>
<summary>Full diff — F13: Leaderboard — public GET /api/leaderboard (username/xp/level only), leaderboard view + nav</summary>

```diff
commit eefd57c5db06b646ff4c99e496208afd86bd6363
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 17:03:28 2026 +0000

    F13: Leaderboard — public GET /api/leaderboard (username/xp/level only), leaderboard view + nav

diff --git a/app/main.py b/app/main.py
index 1ef0eee..c40e900 100644
--- a/app/main.py
+++ b/app/main.py
@@ -6,7 +6,7 @@ from fastapi.responses import FileResponse
 
 from . import models  # noqa: F401 (ensures models are registered before create_all)
 from .database import Base, engine, ensure_schema_upgrades
-from .routers import auth, courses
+from .routers import auth, courses, leaderboard
 
 # Vercel's docs specifically recommend basing file paths on the working
 # directory (the project root) rather than __file__ for the Python runtime.
@@ -35,6 +35,7 @@ app.include_router(auth.router)
 app.include_router(courses.router)
 app.include_router(courses.notes_router)   # /api/notes/... (personal notes, Feature 7)
 app.include_router(courses.shared_router)  # /api/shared/... (public read-only view, Feature 12)
+app.include_router(leaderboard.router)     # /api/leaderboard (Feature 13)
 
 
 @app.get("/api/health")
diff --git a/app/routers/leaderboard.py b/app/routers/leaderboard.py
new file mode 100644
index 0000000..6ba2242
--- /dev/null
+++ b/app/routers/leaderboard.py
@@ -0,0 +1,29 @@
+from typing import List
+
+from fastapi import APIRouter, Depends
+from sqlalchemy.orm import Session
+
+from .. import models, schemas
+from ..database import get_db
+
+# Public leaderboard (Feature 13). Deliberately exposes ONLY username, xp and
+# the derived level — never password hashes, emails, ids or badge data.
+router = APIRouter(prefix="/api", tags=["leaderboard"])
+
+
+@router.get("/leaderboard", response_model=List[schemas.LeaderboardEntry])
+def leaderboard(db: Session = Depends(get_db)):
+    users = (
+        db.query(models.User)
+        .order_by(models.User.xp.desc(), models.User.id.asc())
+        .limit(10)
+        .all()
+    )
+    return [
+        schemas.LeaderboardEntry(
+            username=u.username,
+            xp=u.xp or 0,
+            level=(u.xp or 0) // 100,
+        )
+        for u in users
+    ]
diff --git a/app/schemas.py b/app/schemas.py
index 54f5714..9303457 100644
--- a/app/schemas.py
+++ b/app/schemas.py
@@ -106,6 +106,14 @@ class ShareOut(BaseModel):
     share_id: str
 
 
+class LeaderboardEntry(BaseModel):
+    """Public leaderboard row — username, xp and derived level ONLY.
+    Never password hashes, emails, ids, or any other user data."""
+    username: str
+    xp: int
+    level: int
+
+
 class SharedCourseOut(BaseModel):
     """Read-only public view of a course — title/description/modules only.
     Never includes user info (owner id/username) or edit capability."""
diff --git a/index.html b/index.html
index e52ac49..d287dfe 100644
--- a/index.html
+++ b/index.html
@@ -343,6 +343,18 @@
   .shared-module .mod-num{font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--muted);}
   .shared-title-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px;}
 
+  /* ===== Leaderboard ===== */
+  .lb-row{
+    display:flex;align-items:center;gap:14px;padding:14px 18px;
+    border:1px solid var(--line);border-radius:12px;background:var(--paper-2);margin-bottom:10px;
+  }
+  .lb-row.me{border-color:var(--gold);background:linear-gradient(135deg,#FFFFFF 0%,#FBF3E6 100%);}
+  .lb-rank{font-family:'IBM Plex Mono',monospace;font-size:13px;color:var(--muted);width:24px;}
+  .lb-name{flex:1;font-weight:600;font-size:15px;}
+  .lb-you{font-size:11px;color:var(--gold-2);font-weight:600;}
+  .lb-level{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;color:#221607;background:var(--gold);border-radius:6px;padding:3px 7px;}
+  .lb-xp{font-family:'IBM Plex Mono',monospace;font-size:12.5px;color:var(--muted);width:64px;text-align:right;}
+
   /* ===== Engagement: header XP / level ===== */
   .xp-chip{display:flex;align-items:center;gap:8px;}
   .level-badge{
@@ -399,6 +411,7 @@
     <div class="brand"><span class="dot"></span>Synapse</div>
     <div class="topbar-right">
       <button class="link-btn" id="nav-dashboard">Dashboard</button>
+      <button class="link-btn" id="nav-leaderboard">Leaderboard</button>
       <div class="streak-chip" id="streak-chip" title="Daily learning streak">
         <span class="flame">🔥</span><span id="streak-count">0</span>
       </div>
@@ -533,6 +546,18 @@
 
 </div>
 
+<!-- LEADERBOARD VIEW (Feature 13) -->
+<section id="view-leaderboard" class="view hidden">
+  <div class="dash-wrap" style="max-width:720px;">
+    <button class="link-btn" id="back-to-dash-2" style="margin-bottom:18px;">← Back to dashboard</button>
+    <div class="section-head">
+      <h3>Leaderboard</h3>
+      <span class="count-badge">top 10 learners</span>
+    </div>
+    <div id="leaderboard-list"></div>
+  </div>
+</section>
+
 <!-- PUBLIC SHARED COURSE VIEW (Feature 12) -->
 <section id="view-shared" class="view hidden">
   <div class="dash-wrap" style="max-width:860px;">
@@ -606,7 +631,7 @@ const el = (id) => document.getElementById(id);
 
 /* ---------------- View switching ---------------- */
 function showView(name){
-  ['auth','dashboard','course','shared'].forEach(v=>{
+  ['auth','dashboard','course','leaderboard','shared'].forEach(v=>{
     el('view-'+v).classList.toggle('hidden', v!==name);
   });
   el('topbar').classList.toggle('hidden', name==='auth' || name==='shared');
@@ -770,6 +795,38 @@ async function loadCourses(){
 el('nav-dashboard').addEventListener('click', async ()=>{ refreshMe(); await loadCourses(); renderCourseGrid(); showView('dashboard'); });
 el('back-to-dash').addEventListener('click', async ()=>{ refreshMe(); await loadCourses(); renderCourseGrid(); showView('dashboard'); });
 
+/* ---------------- Leaderboard (Feature 13) ---------------- */
+el('nav-leaderboard').addEventListener('click', openLeaderboard);
+el('back-to-dash-2').addEventListener('click', async ()=>{ refreshMe(); await loadCourses(); renderCourseGrid(); showView('dashboard'); });
+
+async function openLeaderboard(){
+  showView('leaderboard');
+  const holder = el('leaderboard-list');
+  holder.innerHTML = `<div class="gen-status" style="color:var(--muted);"><span class="spinner-dot"></span> Loading leaderboard…</div>`;
+  try{
+    const rows = await api('/leaderboard', { auth:false });
+    if(!rows.length){
+      holder.innerHTML = `<div class="empty-state"><h4>No learners yet</h4><p>Complete lessons and pass quizzes to earn XP.</p></div>`;
+      return;
+    }
+    holder.innerHTML = '';
+    rows.forEach((u,i)=>{
+      const isMe = currentUser && u.username === currentUser;
+      const row = document.createElement('div');
+      row.className = 'lb-row' + (isMe ? ' me' : '');
+      row.innerHTML = `
+        <span class="lb-rank">${String(i+1).padStart(2,'0')}</span>
+        <div class="avatar" style="${isMe ? '' : 'background:var(--muted);'}">${escapeHTML(u.username.slice(0,1).toUpperCase())}</div>
+        <span class="lb-name">${escapeHTML(u.username)}${isMe ? ' <span class="lb-you">(you)</span>' : ''}</span>
+        <span class="lb-level">L${u.level}</span>
+        <span class="lb-xp">${u.xp} XP</span>`;
+      holder.appendChild(row);
+    });
+  }catch(err){
+    holder.innerHTML = `<div class="form-error">Could not load the leaderboard: ${escapeHTML(err.message)}</div>`;
+  }
+}
+
 /* ---------------- Dashboard rendering ---------------- */
 function renderCourseGrid(){
   el('course-count').textContent = coursesSummary.length + (coursesSummary.length===1 ? ' course' : ' courses');
```

</details>

---

## Feature 14 — Dark Mode Toggle

**Commits:** `F14: Dark mode — data-theme attr, CSS variable overrides + targeted fixes, localStorage persistence, topbar toggle`

**Files changed/created:**

- `index.html`

**How it integrates / no-break confirmation:**

Frontend-only. The existing CSS custom properties are now overridden under `html[data-theme="dark"]`, plus targeted overrides for the handful of rules that hardcode light colors (quiz cards, inputs, flashcard backs, toasts, etc.) and an invert filter for the hardcoded-color mind-map SVG — the light theme's rules are untouched. A tiny inline script in `<head>` applies the stored theme before first paint (localStorage `synapse_theme`, falling back to the OS preference, then light), and a topbar toggle flips and persists the choice, re-rendering any visible Mermaid diagram with the matching theme. No backend changes.

**New env vars / dependencies / migration steps:** None. Browser localStorage key: `synapse_theme`.

<details>
<summary>Full diff — F14: Dark mode — data-theme attr, CSS variable overrides + targeted fixes, localStorage persistence, topbar toggle</summary>

```diff
commit c1971a867b61b1ea8d761758772c1347fb428784
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 17:04:37 2026 +0000

    F14: Dark mode — data-theme attr, CSS variable overrides + targeted fixes, localStorage persistence, topbar toggle

diff --git a/index.html b/index.html
index d287dfe..ba1734f 100644
--- a/index.html
+++ b/index.html
@@ -4,6 +4,18 @@
 <meta charset="UTF-8" />
 <meta name="viewport" content="width=device-width, initial-scale=1.0" />
 <title>Synapse — AI Learning Studio</title>
+<script>
+  // Early theme init (Feature 14): set data-theme before first paint to
+  // avoid a light->dark flash. Stored choice wins; otherwise follow the
+  // system preference; default light.
+  (function(){
+    try{
+      var t = localStorage.getItem('synapse_theme');
+      if(!t && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) t = 'dark';
+      document.documentElement.dataset.theme = (t === 'dark') ? 'dark' : 'light';
+    }catch(e){}
+  })();
+</script>
 <link rel="preconnect" href="https://fonts.googleapis.com">
 <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
 <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;0,9..144,700;1,9..144,500&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
@@ -355,6 +367,52 @@
   .lb-level{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;color:#221607;background:var(--gold);border-radius:6px;padding:3px 7px;}
   .lb-xp{font-family:'IBM Plex Mono',monospace;font-size:12.5px;color:var(--muted);width:64px;text-align:right;}
 
+  /* ===== Dark mode (Feature 14) — variable overrides + targeted fixes for
+         the few rules that hardcode light colors. Light mode is untouched. ===== */
+  html[data-theme="dark"]{
+    --ink:#E8EBF8;
+    --ink-2:#B7BEE2;
+    --paper:#101528;
+    --paper-2:#1A2140;
+    --gold:#E8A33D;
+    --gold-2:#F2B65C;
+    --line:#2C3560;
+    --muted:#8E97C2;
+    --success:#4CC083;
+    --danger:#E0674C;
+    --shadow: 0 1px 2px rgba(0,0,0,0.40), 0 8px 24px rgba(0,0,0,0.35);
+  }
+  html[data-theme="dark"] .notes{color:#C6CDE9;}
+  html[data-theme="dark"] .module-item.active{background:#2A2340;}
+  html[data-theme="dark"] .quiz-q{background:#202848;}
+  html[data-theme="dark"] .quiz-q.correct{background:#173226;}
+  html[data-theme="dark"] .quiz-q.incorrect{background:#3A1F1B;}
+  html[data-theme="dark"] .quiz-history{background:#202848;}
+  html[data-theme="dark"] .blog-link-row{background:#202848;}
+  html[data-theme="dark"] .ask-msg.ai{background:#202848;}
+  html[data-theme="dark"] .ask-msg.user{background:var(--ink);color:#101528;}
+  html[data-theme="dark"] .mynote-item{background:#202848;}
+  html[data-theme="dark"] .fc-back{background:#3A2F16;color:#EFE6CC;}
+  html[data-theme="dark"] .field input{background:#141A31;color:var(--ink);border-color:var(--line);}
+  html[data-theme="dark"] .tabs{background:#141A31;}
+  html[data-theme="dark"] .form-error{background:#3A1F1B;border-color:#5A2F27;color:#F0A08E;}
+  html[data-theme="dark"] .edit-field input,
+  html[data-theme="dark"] .edit-field textarea,
+  html[data-theme="dark"] .edit-title-input,
+  html[data-theme="dark"] .edit-desc-input,
+  html[data-theme="dark"] .add-lesson-form input{background:#141A31;color:var(--ink);}
+  html[data-theme="dark"] .video-frame{background:#202848;}
+  html[data-theme="dark"] .badge-card.earned{background:linear-gradient(135deg,#1A2140 0%,#2A2340 100%);}
+  html[data-theme="dark"] .share-panel{background:#202848;}
+  html[data-theme="dark"] .share-panel input{background:var(--paper-2);color:var(--muted);}
+  html[data-theme="dark"] .lb-row.me{background:linear-gradient(135deg,#1A2140 0%,#2A2340 100%);}
+  html[data-theme="dark"] .ask-form input,
+  html[data-theme="dark"] .mynotes-form textarea{background:var(--paper-2);color:var(--ink);}
+  html[data-theme="dark"] .toast{background:#E8EBF8;color:#101528;}
+  /* The mind map SVG hardcodes light palette colors; inverting keeps it
+     readable in dark mode without touching its generator. */
+  html[data-theme="dark"] .mindmap-container svg{filter:invert(0.88) hue-rotate(180deg);}
+
   /* ===== Engagement: header XP / level ===== */
   .xp-chip{display:flex;align-items:center;gap:8px;}
   .level-badge{
@@ -412,6 +470,7 @@
     <div class="topbar-right">
       <button class="link-btn" id="nav-dashboard">Dashboard</button>
       <button class="link-btn" id="nav-leaderboard">Leaderboard</button>
+      <button class="link-btn" id="theme-toggle" title="Switch theme">🌙</button>
       <div class="streak-chip" id="streak-chip" title="Daily learning streak">
         <span class="flame">🔥</span><span id="streak-count">0</span>
       </div>
@@ -712,6 +771,35 @@ async function enterApp(){
   showView('dashboard');
 }
 
+/* ---------------- Theme toggle (Feature 14) ---------------- */
+const THEME_KEY = 'synapse_theme';
+
+function currentTheme(){
+  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
+}
+
+function applyThemeButton(){
+  const btn = el('theme-toggle');
+  if(!btn) return;
+  btn.textContent = currentTheme() === 'dark' ? '☀️' : '🌙';
+  btn.title = currentTheme() === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
+}
+
+function toggleTheme(){
+  const next = currentTheme() === 'dark' ? 'light' : 'dark';
+  document.documentElement.dataset.theme = next;
+  try{ localStorage.setItem(THEME_KEY, next); }catch(e){ /* private mode */ }
+  applyThemeButton();
+  // Re-render a visible diagram so Mermaid picks up the new theme.
+  const c = currentCourse();
+  if(c && c.modules && c.modules[activeModuleIndex]){
+    renderDiagram(c.modules[activeModuleIndex]);
+  }
+}
+
+el('theme-toggle').addEventListener('click', toggleTheme);
+applyThemeButton();
+
 /* ---------------- Engagement stats (XP / level / streak / badges) ---------------- */
 function updateHeaderStats(){
   const xp = meStats.xp || 0;
```

</details>

---

## Feature 15 — Completion Micro-Animations

**Commits:** `F15: Confetti — canvas-confetti CDN, lesson burst + course-complete cannons, reduced-motion guarded`

**Files changed/created:**

- `index.html`

**How it integrates / no-break confirmation:**

Frontend-only. Loads `canvas-confetti` from jsDelivr and fires a small burst whenever a lesson is marked complete, plus sustained side-cannons and a toast when the toggle completes the last remaining lesson of a course. Every effect is guarded: missing CDN (typeof check), `prefers-reduced-motion` (both via media query and canvas-confetti's own `disableForReducedMotion`), and try/catch so a cosmetic failure can never break the completion flow. Un-completing never celebrates.

**New env vars / dependencies / migration steps:** None. New CDN dependency: canvas-confetti@1.9.3 (browser-side only).

<details>
<summary>Full diff — F15: Confetti — canvas-confetti CDN, lesson burst + course-complete cannons, reduced-motion guarded</summary>

```diff
commit 8567c008c0bfc5946c3b79c946ed3d5b910ef7c8
Author: Synapse Dev <dev@synapse.local>
Date:   Thu Sep 10 17:05:30 2026 +0000

    F15: Confetti — canvas-confetti CDN, lesson burst + course-complete cannons, reduced-motion guarded

diff --git a/index.html b/index.html
index ba1734f..2414af0 100644
--- a/index.html
+++ b/index.html
@@ -21,6 +21,7 @@
 <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;0,9..144,700;1,9..144,500&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
 <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.3.0/marked.min.js"></script>
 <script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
+<script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.3/dist/confetti.browser.min.js"></script>
 <style>
   :root{
     --ink:#1B2340;
@@ -800,6 +801,50 @@ function toggleTheme(){
 el('theme-toggle').addEventListener('click', toggleTheme);
 applyThemeButton();
 
+/* ---------------- Completion micro-animations (Feature 15) ---------------- */
+const CONFETTI_COLORS = ['#E8A33D', '#F2B65C', '#3FA66B', '#FFFFFF'];
+
+function motionAllowed(){
+  try{ return !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }
+  catch(e){ return true; }
+}
+
+function lessonConfetti(){
+  if(typeof confetti !== 'function' || !motionAllowed()) return;   // CDN failed or user prefers no motion
+  try{
+    confetti({
+      particleCount: 70,
+      spread: 70,
+      origin: { y: 0.7 },
+      colors: CONFETTI_COLORS,
+      disableForReducedMotion: true,
+    });
+  }catch(e){ /* cosmetic only */ }
+}
+
+function courseCompleteConfetti(){
+  if(typeof confetti !== 'function' || !motionAllowed()) return;
+  try{
+    const end = Date.now() + 1200;
+    (function frame(){
+      confetti({ particleCount: 4, angle: 60,  spread: 60, origin: { x: 0, y: 0.7 }, colors: CONFETTI_COLORS, disableForReducedMotion: true });
+      confetti({ particleCount: 4, angle: 120, spread: 60, origin: { x: 1, y: 0.7 }, colors: CONFETTI_COLORS, disableForReducedMotion: true });
+      if(Date.now() < end) requestAnimationFrame(frame);
+    })();
+  }catch(e){ /* cosmetic only */ }
+}
+
+function celebrateCompletion(course){
+  if(!course || !Array.isArray(course.modules)) return;
+  const allDone = course.modules.length > 0 && course.modules.every(m=>m.completed);
+  if(allDone){
+    courseCompleteConfetti();
+    toast('🎉', 'Course complete — every lesson done!');
+  } else {
+    lessonConfetti();
+  }
+}
+
 /* ---------------- Engagement stats (XP / level / streak / badges) ---------------- */
 function updateHeaderStats(){
   const xp = meStats.xp || 0;
@@ -1310,11 +1355,13 @@ function renderModuleMain(course){
   el('next-mod').addEventListener('click', ()=>{ activeModuleIndex++; renderCourseView(); });
   el('toggle-complete').addEventListener('click', async ()=>{
     try{
+      const targetState = !module.completed;
       activeCourse = await api(`/courses/${course.id}/modules/${activeModuleIndex}/complete`, {
-        method:'PATCH', body:{ completed: !module.completed }
+        method:'PATCH', body:{ completed: targetState }
       });
       renderCourseView();
       refreshMe();   // XP was awarded server-side — update the header bar
+      if(targetState){ celebrateCompletion(activeCourse); }   // Feature 15
     }catch(err){ alert('Could not update completion: ' + err.message); }
   });
   el('edit-mod-btn').addEventListener('click', ()=>{ moduleEditing=true; renderCourseView(); });
```

</details>

---



---

# Migration / deployment notes

**No new Python dependencies, no new environment variables, no changes to
`.env` keys.** Everything above runs on the existing `requirements.txt`.

Two browser-side CDN scripts were added to `index.html` (with graceful
fallbacks if either fails to load): `mermaid@10.9.1` and
`canvas-confetti@1.9.3` (both via jsDelivr).

`vercel.json` gained one rewrite — `{"source": "/share/:path*",
"destination": "/"}` — so public share links resolve to the SPA on Vercel
(plus the `/api/*` rewrite that was already there, untouched). No other
deployment settings changed.

## Schema changes (5 columns + 1 table)

| Table | Change | Default |
|---|---|---|
| users | `xp INTEGER NOT NULL` | 0 |
| users | `streak_count INTEGER NOT NULL` | 0 |
| users | `last_active_date TIMESTAMP` | NULL |
| users | `badges JSON` | '[]' |
| courses | `share_id VARCHAR(64)` (unique, indexed) | NULL |
| notes | **new table** `(id, user_id, course_id, module_index, text, created_at)` | — |

This project has no alembic setup, so two migration paths are provided:

1. **Automatic (SQLite and Postgres, default):** `app/main.py` now calls
   `database.ensure_schema_upgrades()` before `Base.metadata.create_all()`.
   It inspects the live schema and issues additive `ALTER TABLE ... ADD
   COLUMN` statements for anything missing (plus the unique index on
   `courses.share_id`), idempotently and best-effort — a failure logs the
   exact SQL to run manually instead of crashing the app. `create_all()`
   alone would only create the new `notes` table; the ALTERs are what keep
   an existing `synapse.db` working.

2. **Manual (e.g. restricted managed databases):** run by hand —

```sql
ALTER TABLE users      ADD COLUMN xp INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users      ADD COLUMN streak_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users      ADD COLUMN last_active_date TIMESTAMP;
ALTER TABLE users      ADD COLUMN badges JSON DEFAULT '[]';
ALTER TABLE courses    ADD COLUMN share_id VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS ix_courses_share_id ON courses (share_id);

CREATE TABLE notes (
  id INTEGER NOT NULL PRIMARY KEY,
  user_id INTEGER NOT NULL,
  course_id INTEGER NOT NULL,
  module_index INTEGER NOT NULL,
  text TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_notes_user_id ON notes (user_id);
CREATE INDEX ix_notes_course_id ON notes (course_id);
```

(For SQLite the `notes` table is created by `create_all()` automatically;
the snippet above is the Postgres-shaped equivalent. On Vercel's ephemeral
filesystem the SQLite file resets on each cold start anyway — set
`DATABASE_URL` to Postgres as before.)

## Known simplifications (intentional, per the brief's spirit)

- Quiz grading remains client-side (as it is today), so the submit endpoint
  trusts the reported `{score, total}` — validated (`0 <= score <= total`)
  but not re-derived; a determined user could inflate XP by replaying
  submissions. The brief's design (XP awarded on submission) is implemented
  as specified.
- Completing -> un-completing -> completing a lesson re-awards the +10 XP
  (each not-done -> done transition counts). Un-completing itself never
  awards anything.
- `note_taker` badge ids in the catalog existed from Feature 3 but only
  became earnable when Feature 7's table landed (documented in the F3/F7
  commits).
- Text-selection highlighting for personal notes remains the v1 stretch
  goal the brief allowed — v1 ships a simple textarea.
