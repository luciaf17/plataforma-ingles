"""
Django settings for tutor-en.

Every environment-specific value comes from the environment (or a local
`.env` file) through django-environ. See `.env.example` for the full list.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CSRF_TRUSTED_ORIGINS=(list, []),
    TIME_ZONE=(str, "America/Argentina/Buenos_Aires"),
)
# Reads `.env` if present; real environment variables always win.
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Project apps
    "core",
    "learners",
    "lessons",
    "ai",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database: a single DATABASE_URL, e.g. postgres://user:pass@localhost:5432/tutor_en
DATABASES = {"default": env.db("DATABASE_URL")}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# The whole UI is in English by design (see spec §0).
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE")
USE_I18N = True
USE_TZ = True

FIXTURE_DIRS = [BASE_DIR / "fixtures"]

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Audio recordings and generated TTS files live here (a Railway volume in prod).
MEDIA_URL = "media/"
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))

LOGIN_URL = "admin:login"

# OpenAI (spec 2). Models are pinned here so a swap is one line.
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
OPENAI_CHAT_MODEL = env("OPENAI_CHAT_MODEL", default="gpt-4o")
OPENAI_STT_MODEL = env("OPENAI_STT_MODEL", default="gpt-4o-transcribe")
OPENAI_TTS_MODEL = env("OPENAI_TTS_MODEL", default="gpt-4o-mini-tts")
OPENAI_TIMEOUT_SECONDS = env.float("OPENAI_TIMEOUT_SECONDS", default=60.0)
OPENAI_MAX_RETRIES = env.int("OPENAI_MAX_RETRIES", default=3)

# Skills the planner may schedule. Grows as each runner lands (spec 13).
LESSON_SKILLS_ENABLED = env.list("LESSON_SKILLS_ENABLED", default=["speaking"])

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "ai": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
