import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("NEWSCRAWLER_SECRET_KEY", "unsafe-development-key-change-me")
DEBUG = os.environ.get("NEWSCRAWLER_DEBUG", "0") == "1"
ALLOWED_HOSTS = [x.strip() for x in os.environ.get("NEWSCRAWLER_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if x.strip()]
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.environ.get("NEWSCRAWLER_CSRF_TRUSTED_ORIGINS", "").split(",") if x.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "collector.apps.CollectorConfig",
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
ROOT_URLCONF = "newscrawler.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "newscrawler.wsgi.application"
DB_PATH = Path(os.environ.get("NEWSCRAWLER_DB_PATH", BASE_DIR / "data" / "newscrawler.sqlite3")).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": DB_PATH,
    "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"},
}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
NEWSCRAWLER_USER_AGENT = os.environ.get("NEWSCRAWLER_USER_AGENT", "PositiveNewsCrawler/0.1 (+operator@example.invalid)")
NEWSCRAWLER_ROUTER_MCP_URL = os.environ.get("NEWSCRAWLER_ROUTER_MCP_URL", "http://127.0.0.1:8088/mcp")
NEWSCRAWLER_ROUTER_AUTH_TOKEN = os.environ.get("NEWSCRAWLER_ROUTER_AUTH_TOKEN", "")
NEWSCRAWLER_ROUTER_TIMEOUT_SECONDS = float(os.environ.get("NEWSCRAWLER_ROUTER_TIMEOUT_SECONDS", "300"))
NEWSCRAWLER_TRANSLATION_PROVIDER = os.environ.get("NEWSCRAWLER_TRANSLATION_PROVIDER", "deepseek")
NEWSCRAWLER_TRANSLATION_MODEL = os.environ.get("NEWSCRAWLER_TRANSLATION_MODEL", "deepseek-chat")
NEWSCRAWLER_TRANSLATION_TIER = os.environ.get("NEWSCRAWLER_TRANSLATION_TIER", "")
NEWSCRAWLER_TRANSLATION_TEMPERATURE = float(os.environ.get("NEWSCRAWLER_TRANSLATION_TEMPERATURE", "0.2"))
NEWSCRAWLER_TRANSLATION_MAX_TOKENS = int(os.environ.get("NEWSCRAWLER_TRANSLATION_MAX_TOKENS", "8192"))
NEWSCRAWLER_MANUAL_SCORE_SELECTOR = os.environ.get("NEWSCRAWLER_MANUAL_SCORE_SELECTOR", "news-evaluator")
NEWSCRAWLER_BACKUP_DIR = Path(os.environ.get("NEWSCRAWLER_BACKUP_DIR", BASE_DIR / "data" / "backups")).resolve()
NEWSCRAWLER_LOG_DIR = Path(os.environ.get("NEWSCRAWLER_LOG_DIR", BASE_DIR / "data" / "logs")).resolve()
NEWSCRAWLER_LOG_DIR.mkdir(parents=True, exist_ok=True)
SECURE_MODE = os.environ.get("NEWSCRAWLER_SECURE", "0") == "1"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = SECURE_MODE
SESSION_COOKIE_SECURE = SECURE_MODE
CSRF_COOKIE_SECURE = SECURE_MODE
SECURE_HSTS_SECONDS = 31_536_000 if SECURE_MODE else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_MODE
SECURE_HSTS_PRELOAD = SECURE_MODE
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"format": '{{"time":"{asctime}","level":"{levelname}","logger":"{name}","message":"{message}"}}', "style": "{"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
        "file": {"class": "logging.handlers.RotatingFileHandler", "filename": NEWSCRAWLER_LOG_DIR / "newscrawler.log", "maxBytes": 5_000_000, "backupCount": 5, "formatter": "json", "encoding": "utf-8"},
    },
    "root": {"handlers": ["console", "file"], "level": os.environ.get("NEWSCRAWLER_LOG_LEVEL", "INFO")},
}
