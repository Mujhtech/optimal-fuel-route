import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "var"))
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-development-only-change-before-public-deployment")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",")
INSTALLED_APPS = ["django.contrib.staticfiles", "planner"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True, "OPTIONS": {}}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": DATA_DIR / "db.sqlite3", "OPTIONS": {"timeout": 30}}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache", "LOCATION": str(CACHE_DIR / "responses"), "OPTIONS": {"MAX_ENTRIES": 5000}}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "/static/"
STATIC_ROOT = DATA_DIR / "static"
DATA_UPLOAD_MAX_MEMORY_SIZE = 16_384
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
GEOCODING_BASE_URL = os.environ.get("GEOCODING_BASE_URL", "https://photon.komoot.io")
PROVIDER_USER_AGENT = os.environ.get("PROVIDER_USER_AGENT", "OptimalFuelPlanner/1.0 (route planning assignment)")
PROVIDER_TIMEOUT_SECONDS = float(os.environ.get("PROVIDER_TIMEOUT_SECONDS", "15"))
PROVIDER_RATE_INTERVAL_SECONDS = float(os.environ.get("PROVIDER_RATE_INTERVAL_SECONDS", "1"))
PROVIDER_RATE_DB = str(CACHE_DIR / "provider_rate.sqlite3")
GEOCODE_CACHE_TIMEOUT = 60 * 60 * 24 * 30
ROUTE_CACHE_TIMEOUT = 60 * 60 * 24
PLAN_CACHE_TIMEOUT = 60 * 60 * 24
STATION_CORRIDOR_MILES = float(os.environ.get("STATION_CORRIDOR_MILES", "1"))
MAX_FUEL_STOPS = 40
MAX_ROUTE_SNAP_METERS = float(os.environ.get("MAX_ROUTE_SNAP_METERS", "1000"))
MAX_STATION_SNAP_MILES = float(os.environ.get("MAX_STATION_SNAP_MILES", "0.25"))
LOGGING = {"version": 1, "disable_existing_loggers": False, "handlers": {"console": {"class": "logging.StreamHandler"}}, "root": {"handlers": ["console"], "level": "INFO"}}
