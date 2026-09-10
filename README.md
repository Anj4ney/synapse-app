# Synapse — AI Learning Studio

A full-stack rebuild of the Synapse prototype: a FastAPI backend with real
accounts (hashed passwords + JWT), a real database, and a static frontend,
ready to deploy on Vercel — now with a full set of engagement and
interactivity features layered on top (XP, streaks, badges, flashcards, an
AI tutor chat, personal notes, auto-generated diagrams, public sharing, a
leaderboard, dark mode, and more).

## What changed from the prototype

- **Accounts are real.** Passwords are hashed with bcrypt and stored in a
  database, not in browser storage. Sessions use JWTs (`localStorage` holds
  the token, sent as `Authorization: Bearer <token>`).
- **Courses live in a database**, not per-browser storage — so your courses
  follow you across devices/browsers as long as you log into the same account.
- **AI generation happens server-side.** Your Gemini API key never touches
  the browser; the backend calls the Gemini API on the frontend's behalf.
- **Learning is more interactive.** On top of the original course/lesson/quiz
  flow, Synapse now tracks your progress (XP, streaks, badges), gives you
  extra ways to study a lesson (flashcards, an "ask about this lesson" AI
  chat, auto-generated diagrams, an ELI5 mode, read-aloud), lets you keep
  personal notes, and lets you share a course publicly — see
  [Engagement & interactivity features](#engagement--interactivity-features)
  below for the full list.

## Project layout

```
synapse-app/
├── api/
│   └── index.py            # Vercel serverless entrypoint (imports app/main.py)
├── app/
│   ├── main.py               # FastAPI app, CORS, router registration, runtime schema upgrades
│   ├── database.py           # SQLAlchemy engine/session + additive column/index migrations
│   ├── models.py              # User, Course, Note tables
│   ├── schemas.py             # Pydantic request/response models
│   ├── security.py            # Password hashing + JWT
│   ├── ai.py                   # Gemini API calls (course/module/quiz/flashcard/diagram/etc. generation)
│   ├── achievements.py         # Streak rule + badge catalog/evaluation
│   └── routers/
│       ├── auth.py             # /api/auth/signup, /login, /me
│       ├── courses.py          # /api/courses/... CRUD + generation + engagement endpoints,
│       │                       #   plus the /api/notes and /api/shared sub-routers
│       └── leaderboard.py      # /api/leaderboard
├── index.html                  # Frontend (talks to the API above)
├── requirements.txt
├── vercel.json                  # /api/* and /share/* rewrites
└── .env.example
```

## 1. Run it locally

```bash
cd synapse-app
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: at minimum set JWT_SECRET to a random string.
# Add Gemini_API_KEY to enable course/quiz/flashcard/diagram/etc. generation (see step 2).

uvicorn app.main:app --reload --port 8000
```

This uses a local SQLite file (`synapse.db`) by default — nothing else to set up.
On startup, the app also runs a best-effort, additive schema check
(`ensure_schema_upgrades`) that adds any newly-introduced columns to an
existing `synapse.db` automatically — see
[Database schema](#database-schema) below.

Then, in another terminal, serve the frontend (it just needs to be served
over HTTP, not opened as a `file://` URL, so `fetch` calls work):

```bash
python3 -m http.server 5500
```

Open `http://localhost:5500/index.html`. Note: with two different ports,
the frontend's `fetch('/api/...')` calls need the backend on the same
origin. For local testing, either:
- Run FastAPI serve the frontend too — put `index.html` next to `app/` and
  add a `StaticFiles` mount in `app/main.py`, **or** (simpler)
- Use `uvicorn` with `--port 5500` isn't enough since it's a different app —
  easiest is to temporarily change `API_BASE` in `index.html` to
  `http://localhost:8000/api` while developing locally.

On Vercel (step 3), the frontend and API are automatically on the same
origin, so this isn't an issue in production.

## 2. Get a Gemini API key

1. Go to [aistudio.google.com](https://aistudio.google.com) and sign up
   / log in.
2. Create an API key under **Settings → API Keys**.
3. Set it as `GEMINI_API_KEY` in your `.env` (local) or in Vercel's
   environment variables (production).

Without this key, everything else in the app (accounts, saving/editing
courses, marking lessons complete, XP/streaks/badges, personal notes,
sharing, the leaderboard, dark mode) still works — you'll just get an error
if you try to generate a course, lesson, quiz, flashcards, a diagram, an
ELI5 summary, or use the "ask about this lesson" chat.

## 3. Deploy to Vercel

1. Push this project to a GitHub repo.
2. Go to [vercel.com/new](https://vercel.com/new) and import the repo.
3. Vercel will detect the Python function in `api/index.py` automatically —
   no build settings needed.
4. Add environment variables in the Vercel project settings
   (**Settings → Environment Variables**):
   - `DATABASE_URL` — see below, this is the important one.
   - `JWT_SECRET` — a random string (`python3 -c "import secrets; print(secrets.token_hex(32))"`).
   - `GEMINI_API_KEY` — from step 2.
5. Deploy.

### About the database on Vercel

Vercel's serverless functions have an **ephemeral filesystem** — a SQLite
file will not persist between requests in production. For deployment, set
`DATABASE_URL` to a real hosted Postgres database. Free options that work
well with this setup:

- [Neon](https://neon.tech) — serverless Postgres, generous free tier.
- [Supabase](https://supabase.com) — Postgres + free tier.
- Vercel's own Postgres integration (Storage tab in your Vercel project).

Any of these gives you a connection string like:
`postgresql://user:password@host/dbname`

Set that as `DATABASE_URL` and the app will use it automatically — no code
changes needed (SQLAlchemy + `psycopg2-binary` handle both SQLite and
Postgres via the same `DATABASE_URL` setting).

## Engagement & interactivity features

All of the features below were added additively — no existing endpoint's
request/response shape changed, and nothing existing was renamed or
restructured. Each degrades gracefully if its data isn't there yet (e.g. a
lesson with no flashcards generated yet just shows a "Generate" button).

| Feature | What it does |
|---|---|
| **XP & levels** | +10 XP for completing a lesson, +5 XP per correct quiz answer. Level = `xp // 100`, shown as a bar/badge in the header. |
| **Streaks** | A daily streak counter (🔥) that increments once per day you complete a lesson or submit a quiz, and resets if you miss a day. |
| **Badges** | An 8-badge achievement catalog (first lesson, course completed, quiz milestones, streak milestones, XP milestones, flashcards, notes) shown on the dashboard, with a toast when a new one is earned. |
| **Quiz history** | Every quiz attempt is recorded (`score`/`total`/`date`); the quiz block shows your best score and a small bar chart of recent attempts. |
| **Flashcards** | Generates 6 front/back flashcards per lesson on demand, rendered as flippable cards with shuffle. |
| **Ask about this lesson** | A small stateless AI chat under each lesson that answers questions grounded in that lesson's notes. |
| **Personal notes** | Write and delete your own free-text notes on any lesson, stored per user/course/lesson. |
| **Read-aloud** | A "Listen" button that reads the lesson notes aloud using the browser's built-in text-to-speech — no backend involved. |
| **Difficulty toggle** | "− Simpler" / "+ More advanced" buttons that regenerate the current lesson at a different reading level. |
| **Auto-generated diagrams** | Generates a Mermaid.js diagram for a lesson when one would genuinely help (flowchart, sequence, hierarchy, etc.), rendered inline. |
| **ELI5 mode** | Toggles the displayed lesson text to a simplified "explain like I'm 5" version without touching the stored notes. |
| **Public sharing** | Generates a stable `/share/{id}` link to a read-only, unauthenticated view of a course. |
| **Leaderboard** | A public top-10 leaderboard ranked by XP, showing only username/xp/level. |
| **Dark mode** | A header toggle that switches the whole app to a dark theme, persisted in `localStorage`. |
| **Completion animations** | A small confetti burst when you complete a lesson, and a bigger celebration when you finish an entire course (respects `prefers-reduced-motion`). |

## API overview

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/signup` | Create an account, returns a JWT |
| POST | `/api/auth/login` | Log in, returns a JWT |
| GET | `/api/auth/me` | Current user info — id, username, plus `xp`, `level`, `streak_count`, `badges` (requires auth) |
| GET | `/api/courses` | List your courses (summary) |
| POST | `/api/courses` | Generate a new course from a topic (AI) |
| GET | `/api/courses/{id}` | Full course with modules |
| PUT | `/api/courses/{id}` | Edit title/description |
| DELETE | `/api/courses/{id}` | Delete a course (also removes its personal notes) |
| POST | `/api/courses/{id}/modules` | Generate + add a lesson (AI) |
| PUT | `/api/courses/{id}/modules/{i}` | Manually edit a lesson |
| DELETE | `/api/courses/{id}/modules/{i}` | Delete a lesson (shifts personal note indexes down) |
| POST | `/api/courses/{id}/modules/{i}/reorder` | Move a lesson up/down (keeps personal notes attached) |
| POST | `/api/courses/{id}/modules/{i}/regenerate` | Rewrite a lesson (AI). Optional body `{"difficulty": "simpler"\|"advanced"}` |
| PATCH | `/api/courses/{id}/modules/{i}/complete` | Toggle completion — awards XP/streak/badges on completion |
| POST | `/api/courses/{id}/modules/{i}/quiz` | Generate a quiz for a lesson (AI) |
| POST | `/api/courses/{id}/modules/{i}/quiz/submit` | Report a graded quiz attempt — awards XP, updates streak/badges/history |
| POST | `/api/courses/{id}/modules/{i}/flashcards` | Generate flashcards for a lesson (AI) |
| POST | `/api/courses/{id}/modules/{i}/ask` | Ask a question about a lesson (AI, stateless) |
| POST | `/api/courses/{id}/modules/{i}/diagram` | Generate a Mermaid.js diagram for a lesson, if one would help (AI) |
| POST | `/api/courses/{id}/modules/{i}/eli5` | Get a simplified "explain like I'm 5" version of a lesson's notes (AI, stateless) |
| POST | `/api/courses/{id}/modules/{i}/notes` | Add a personal note to a lesson |
| GET | `/api/courses/{id}/modules/{i}/notes` | List your personal notes on a lesson |
| DELETE | `/api/notes/{note_id}` | Delete one of your personal notes |
| POST | `/api/courses/{id}/share` | Generate (or fetch the existing) public share link for a course |
| GET | `/api/shared/{share_id}` | **Unauthenticated**, read-only view of a shared course |
| GET | `/api/leaderboard` | **Unauthenticated** top-10 leaderboard (username/xp/level only) |

Interactive docs are available at `/docs` once the backend is running
(FastAPI's built-in Swagger UI).

## Database schema

| Table | Columns | Notes |
|---|---|---|
| `users` | `id, username, password_hash, created_at, xp, streak_count, last_active_date, badges` | `xp`/`streak_count` default to 0; `badges` is a JSON list of badge ids; `last_active_date` is nullable. |
| `courses` | `id, user_id, title, description, modules, created_at, share_id` | `modules` is a JSON list of lesson dicts (now optionally carrying `quiz_attempts`, `flashcards`, `diagram`); `share_id` is a nullable, unique UUID string set the first time a course is shared. |
| `notes` | `id, user_id, course_id, module_index, text, created_at` | New table for personal per-lesson notes; fully separate from `users`/`courses`. |

New columns are nullable or have defaults, so old rows keep working.
`app/main.py` runs `database.ensure_schema_upgrades()` before
`Base.metadata.create_all()` on every startup — it inspects the live schema
and issues additive `ALTER TABLE ... ADD COLUMN` statements for anything
missing (plus the unique index on `courses.share_id`), so an existing
`synapse.db` (or Postgres database) upgrades itself automatically. If that
best-effort migration can't run against a locked-down managed database
(e.g. missing ALTER privileges), it logs the exact SQL to run by hand:

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

## Frontend dependencies

Two extra CDN scripts were added to `index.html` (both with graceful
fallbacks if they fail to load):

- [`mermaid`](https://mermaid.js.org) `10.9.1` — renders the auto-generated
  lesson diagrams.
- [`canvas-confetti`](https://www.npmjs.com/package/canvas-confetti) `1.9.3`
  — the lesson/course completion animations.

Everything else still runs on plain vanilla JS, no build step. No new
Python dependencies were added — `requirements.txt` is unchanged.

## Notes / next steps you might want

- Passwords require 6+ characters; adjust in `app/schemas.py` if you want stricter rules.
- CORS currently allows all origins (`ALLOWED_ORIGINS=*`) — fine for a same-origin
  Vercel deployment, but lock this down if you split frontend/backend across domains.
- There's no password-reset flow yet — that'd be a good next feature.
- Notes/quizzes/flashcards/diagrams/ELI5 answers are AI-generated — the footer
  note in the app reminds users to double-check anything important, same as
  the original prototype.
- Quiz grading is still done client-side, so `POST /quiz/submit` trusts the
  reported `{score, total}` (validated so `score` can't exceed `total`, but
  not independently re-derived server-side).
- Text-selection highlighting for personal notes is a natural next step —
  v1 ships a simple textarea per lesson instead.
- A password-reset flow and per-course leaderboards would be other good
  additions on top of the current global one.
