# tutor-en

An English tutor with a student file. The chat is the interface; the file is the product.

Every lesson produces evidence. Evidence becomes `ErrorItem` and `VocabItem` rows. Those rows decide what tomorrow's lesson contains. See [docs/spec-plataforma-ingles.md](docs/spec-plataforma-ingles.md) for the full brief and [docs/tutor-en-prototype.html](docs/tutor-en-prototype.html) for the visual reference.

## Stack

- Django 5 + PostgreSQL
- Django templates + HTMX + Tailwind (CDN for now)
- OpenAI API (gpt-4o, gpt-4o-transcribe, gpt-4o-mini-tts)
- Railway for deploy

## Local setup

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then edit SECRET_KEY and DATABASE_URL
python manage.py migrate
python manage.py loaddata seed
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000/.

### Local PostgreSQL without an installer

The portable EDB binaries work fine on Windows and need no admin rights. They live outside the repo at `C:\Users\maril\dev\pgsql`:

```
C:\Users\maril\dev\pgsql\pgsql\bin\pg_ctl -D C:\Users\maril\dev\pgsql\data -l C:\Users\maril\dev\pgsql\pg.log start
C:\Users\maril\dev\pgsql\pgsql\bin\pg_ctl -D C:\Users\maril\dev\pgsql\data stop
```

Credentials for local dev: user `postgres`, password `postgres`, database `tutor_en` (matches `.env.example`).

## Deploy (Railway)

Production: https://web-production-2b555.up.railway.app (project `tutor-en`, service `web`). Deploys are pushed from the CLI with `railway up --service web`; static files are collected at start.

The repo carries `railway.json` (build + start commands) and a `Procfile`. On every start the app runs migrations, then `manage.py bootstrap` (creates the superuser from env and loads the seed if the database is empty), then gunicorn.

1. Create a Railway project from this GitHub repo and add a **PostgreSQL** service. Railway injects `DATABASE_URL`.
2. Add a **volume** to the web service mounted at `/data`. Lesson audio is stored there.
3. Set these variables on the web service:

```
SECRET_KEY=<long random string>
DEBUG=False
ALLOWED_HOSTS=<your-app>.up.railway.app
CSRF_TRUSTED_ORIGINS=https://<your-app>.up.railway.app
MEDIA_ROOT=/data/media
OPENAI_API_KEY=sk-...
DJANGO_SUPERUSER_USERNAME=lu
DJANGO_SUPERUSER_PASSWORD=<password>
DJANGO_SUPERUSER_EMAIL=you@example.com
TIME_ZONE=America/Argentina/Buenos_Aires
```

4. Generate a public domain for the service. The microphone only works over HTTPS, which Railway provides.

Static files are served by whitenoise; media is served by Django behind login (single user, small files).

## Layout

```
config/      settings, urls, wsgi
core/        base template, sidebar, shared helpers
learners/    Learner, Track, Topic, GrammarTopic
lessons/     Lesson, Turn, ErrorItem, VocabItem, reports, runners, postprocess
ai/          OpenAI client, planner, analyzer, level assessor, prompts/
fixtures/    seed.json, transcript_sample.md
static/      css, js
templates/   base.html and per-app templates
docs/        spec and prototype
```

## Build sequence

Modules are built strictly in the order of spec §13. Each module ends with something that runs.

| # | Module | Status |
|---|---|---|
| 1 | `core` | done |
| 2 | `learners` | done |
| 3 | `lessons` models | done |
| 4 | `ai/client.py` | done |
| 5 | `ai/analyzer.py` | done |
| 6 | `lessons/postprocess.py` | done |
| 7 | `ai/planner.py` | done |
| 8 | speaking runner | done |
| 9 | lesson report | done |
| 10 | deploy | done |
| 11 | `today` | done |
| 12 | writing | pending |
| 13 | reading | pending |
| 14 | listening | pending |
| 15 | mini-lesson in text | pending |
| 16 | grammar | pending |
| 17 | errors + vocab | pending |
| 18 | cron | pending |
