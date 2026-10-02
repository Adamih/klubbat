"""Django settings for the political-intelligence MVP."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = _env_bool("DJANGO_DEBUG", "true")
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "ingestion",
    "search",
    "website",
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
        "DIRS": [],
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

# --- SQLite: single database file under data/ (gitignored, persistent) ---
DATA_DIR = BASE_DIR / "data"
ORIGINALS_DIR = DATA_DIR / "originals"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
DERIVED_DIR = DATA_DIR / "derived"
LOG_DIR = DATA_DIR / "logs"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "sv"
TIME_ZONE = "Europe/Stockholm"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

# --- Ingestion behaviour ---
SOURCE_USER_AGENT = "political-intelligence-mvp/0.1 (public-document research)"
DOWNLOAD_CONCURRENCY = int(os.environ.get("DOWNLOAD_CONCURRENCY", "4"))
DOWNLOAD_TIMEOUT = (15.0, 90.0)  # (connect, read) seconds
DOWNLOAD_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
BACKFILL_YEAR = int(os.environ.get("BACKFILL_YEAR", "2025"))
RECENT_WINDOW_DAYS = int(os.environ.get("RECENT_WINDOW_DAYS", "60"))

# --- OCR toolchain (tesseract, used by xberg for scanned pages) ---
TESSDATA_PREFIX = str(BASE_DIR / "tessdata")
TESSERACT_DIR = os.environ.get("TESSERACT_DIR", r"C:\Program Files\Tesseract-OCR")

# --- Logging: console INFO, rotating file DEBUG ---
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "app.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "simple",
        },
    },
    "root": {"handlers": ["console", "file"], "level": "INFO"},
    "loggers": {
        "ingestion": {"level": "DEBUG"},
        "django.request": {"level": "WARNING"},
    },
}
