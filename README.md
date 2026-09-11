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

Production: https://web-production-2b555.up.railway.app (project `tutor-en`). Two services deploy from this repo with `railway up --service <name>`; their build and start commands live in the Railway service settings (Railway deprecated `railway.json`, and its IaC does not carry cron schedules yet):

| Service | Build | Start | Schedule |
|---|---|---|---|
| `web` | `pip install -r requirements.txt` | `collectstatic`, `migrate`, `bootstrap`, then gunicorn on `$PORT` | always on |
| `cron` | same | `python manage.py prepare_next_lesson` | `0 6 * * *` UTC = 03:00 Buenos Aires |

`bootstrap` creates the superuser from env and loads the seed when the database is empty, so a fresh environment needs no shell. The `cron` service shares secrets by reference (`${{web.SECRET_KEY}}`, `${{web.OPENAI_API_KEY}}`, `${{Postgres.DATABASE_URL}}`).

To recreate from scratch:

1. Create a Railway project, add a **PostgreSQL** service, and two empty services `web` and `cron`.
2. Add a **volume** to `web` mounted at `/data` (lesson audio).
3. Set these variables on `web`:

```
SECRET_KEY=<long random string>
DEBUG=False
ALLOWED_HOSTS=<your-app>.up.railway.app,healthcheck.railway.app
CSRF_TRUSTED_ORIGINS=https://<your-app>.up.railway.app
MEDIA_ROOT=/data/media
OPENAI_API_KEY=sk-...
DJANGO_SUPERUSER_USERNAME=lu
DJANGO_SUPERUSER_PASSWORD=<password>
DJANGO_SUPERUSER_EMAIL=you@example.com
TIME_ZONE=America/Argentina/Buenos_Aires
SECURE_SSL_REDIRECT=False
```

   and on `cron`: `SECRET_KEY`, `OPENAI_API_KEY`, `DATABASE_URL` by reference, `DEBUG=False`, `ALLOWED_HOSTS=*`, `SECURE_SSL_REDIRECT=False`, `TIME_ZONE`.
4. Set the commands from the table above in each service's settings (or with `railway api` and `serviceInstanceUpdate`), and the cron schedule on `cron`.
5. Generate a public domain for `web`. The microphone only works over HTTPS, which Railway provides.
6. `railway up --service web` and `railway up --service cron`.

Railway's healthcheck calls the service with the host `healthcheck.railway.app`, so it must be in `ALLOWED_HOSTS` or every deploy fails with HTTP 400; it also calls over plain HTTP, so `SECURE_SSL_REDIRECT=False` on `web` (Railway's edge already redirects HTTP to HTTPS). Static files are served by whitenoise; media is served by Django behind login (single user, small files).

## Following her, not a list

Two jobs read the student file and change what the app offers.

`ai/choose_article.py` picks the reading of the day instead of drawing it at
random. It sends the model titles and sources only — never article bodies —
along with her goal, recent class topics, and her taste: what she has read and
what she pressed "read something else" on. A rejection is the strongest signal
it has. Any failure falls back to the random pick, so reading is never blocked.

`ai/propose_topics.py` writes new lesson subjects out of her file (her
free-text requests first, then the errors that keep coming back) when she is
close to running out. A proposed `Topic` carries the learner it was written
for and a Spanish `proposed_reason` she reads in the picker; seeded topics
have no learner and stay shared. `Topic.objects.visible_to(learner)` is the
one place that knows the difference.

Both run from the cron, before the day's lesson is planned.

## Fluency: the retell round

The wrap-up of a speaking class is not only feedback. The plan carries a
`fluency_retell`: the tutor asks the learner to tell again, from the top, the
thing she explained at most length, first in 60 seconds and then in 40. The
model picks the content; the clock is `planner.RETELL_ROUNDS`, not its choice.
Nothing is corrected during the round.

This is Nation's 4/3/2, scaled to the minutes a 20-minute class can spare, and
it is why the speaking phases deviate from the spec 4.1b table: the wrap-up
went from 1 minute to 3, paid for out of practice and drill.

The same reasoning drives vocabulary: the analyzer and the planner record
**chunks** (`push back on a nitpick`, `what does X mean?`) rather than bare
words, because a speaker retrieves a chunk whole and assembles a sentence one
word at a time. `VocabItem.is_chunk` is what the Vocabulary screen counts.

## Reading from real articles

Reading lessons quote a real piece published in the last few days instead of a
text the model invents. `python manage.py fetch_articles` reads the feeds in
`articles/sources.py` (Hacker News, dev.to and engineering blogs), extracts the
body with trafilatura, and keeps what is long enough, in English, and actually
an article. The cron runs it before planning the day's lesson.

The planner receives an excerpt and builds the glossary and comprehension
questions on it; the text stored in the lesson is the article's own wording,
never the model's paraphrase, and the screen links to the original. An article
is served to a learner once. With an empty pool (every feed down, say) the
planner writes its own text as before, so a reading lesson never fails for
want of an article.

## Install as an app

The site is a progressive web app: `/manifest.webmanifest` and a service
worker at `/sw.js` (both rendered by Django so the icon URLs follow the
hashed static files in production). The worker never caches pages, only the
app's own static files and an offline page; every screen needs the server
and the AI anyway.

- Android (Chrome/Edge): open the site, menu, "Add to Home screen" / "Install app".
- iPhone (Safari): share button, "Add to Home Screen". iOS asks for the
  microphone again on each launch; that is Safari's rule for home-screen apps.
- Desktop (Chrome/Edge): the install icon at the right end of the address bar.

The icons in `static/icons/` are screenshots of a one-file HTML page (the
brand mark in Newsreader on the app background); regenerate them the same
way if the palette changes.

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
| 12 | writing | done |
| 13 | reading | done |
| 14 | listening | done |
| 15 | mini-lesson in text | done |
| 16 | grammar | done |
| 17 | errors + vocab | done |
| 18 | cron | done |
| 19 | checkpoint | done |
| 20 | onboarding | done |
| 21 | level tracking | done |
| 22 | progress | done |
| 23 | polish | done |
