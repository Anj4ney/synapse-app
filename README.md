# Synapse — AI Learning Studio

A full-stack rebuild of the Synapse prototype: a FastAPI backend with real
accounts (hashed passwords + JWT), a real database, and a static frontend,
ready to deploy on Vercel.

## What changed from the prototype

- **Accounts are real.** Passwords are hashed with bcrypt and stored in a
  database, not in browser storage. Sessions use JWTs (`localStorage` holds
  the token, sent as `Authorization: Bearer <token>`).
- **Courses live in a database**, not per-browser storage — so your courses
  follow you across devices/browsers as long as you log into the same account.
- **AI generation happens server-side.** Your Anthropic API key never touches
  the browser; the backend calls the Anthropic API on the frontend's behalf.

## Project layout

```
synapse-app/
├── api/
│   └── index.py          # Vercel serverless entrypoint (imports app/main.py)
├── app/
│   ├── main.py            # FastAPI app, CORS, router registration
│   ├── database.py        # SQLAlchemy engine/session
│   ├── models.py          # User, Course tables
│   ├── schemas.py         # Pydantic request/response models
│   ├── security.py        # Password hashing + JWT
│   ├── ai.py               # Gemini API calls (course/module/quiz generation)
│   └── routers/
│       ├── auth.py         # /api/auth/signup, /login, /me
│       └── courses.py      # /api/courses/... CRUD + generation endpoints
├── index.html              # Frontend (talks to the API above)
├── requirements.txt
├── vercel.json
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
# Add Gemini_API_KEY to enable course/quiz generation (see step 2).

uvicorn app.main:app --reload --port 8000
```

This uses a local SQLite file (`synapse.db`) by default — nothing else to set up.

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

## 2. Get an Gemini API key

1. Go to [aistudio.google.com]([https://aistudio.google.com]) and sign up
   / log in.
2. Create an API key under **Settings → API Keys**.
3. Set it as `GEMINI_API_KEY` in your `.env` (local) or in Vercel's
   environment variables (production).

Without this key, everything else in the app (accounts, saving/editing
courses, marking lessons complete) still works — you'll just get an error
if you try to generate a course, lesson, or quiz.

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

## API overview

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/signup` | Create an account, returns a JWT |
| POST | `/api/auth/login` | Log in, returns a JWT |
| GET | `/api/auth/me` | Current user info (requires auth) |
| GET | `/api/courses` | List your courses (summary) |
| POST | `/api/courses` | Generate a new course from a topic (AI) |
| GET | `/api/courses/{id}` | Full course with modules |
| PUT | `/api/courses/{id}` | Edit title/description |
| DELETE | `/api/courses/{id}` | Delete a course |
| POST | `/api/courses/{id}/modules` | Generate + add a lesson (AI) |
| PUT | `/api/courses/{id}/modules/{i}` | Manually edit a lesson |
| DELETE | `/api/courses/{id}/modules/{i}` | Delete a lesson |
| POST | `/api/courses/{id}/modules/{i}/reorder` | Move a lesson up/down |
| POST | `/api/courses/{id}/modules/{i}/regenerate` | Rewrite a lesson (AI) |
| PATCH | `/api/courses/{id}/modules/{i}/complete` | Toggle completion |
| POST | `/api/courses/{id}/modules/{i}/quiz` | Generate a quiz for a lesson (AI) |

Interactive docs are available at `/docs` once the backend is running
(FastAPI's built-in Swagger UI).

## Notes / next steps you might want

- Passwords require 6+ characters; adjust in `app/schemas.py` if you want stricter rules.
- CORS currently allows all origins (`ALLOWED_ORIGINS=*`) — fine for a same-origin
  Vercel deployment, but lock this down if you split frontend/backend across domains.
- There's no password-reset flow yet — that'd be a good next feature.
- Notes/quizzes are AI-generated — the footer note in the app reminds users to
  double-check anything important, same as the original prototype.
