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
| 4 | `ai/client.py` | done (smoke pending key) |
| 5 | `ai/analyzer.py` | pending |
| 6 | `lessons/postprocess.py` | pending |
| 7 | `ai/planner.py` | pending |
| 8 | speaking runner | pending |
| 9 | lesson report | pending |
| 10 | deploy | pending |
