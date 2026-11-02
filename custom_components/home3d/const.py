"""Constants of the Home 3D integration."""

from datetime import timedelta

DOMAIN = "home3d"

CONF_REPOSITORY = "repository"
CONF_HOME = "home"

COOKIE = "home3d_session"
COOKIE_PATH = "/api/home3d/"
SESSION_TTL = 3600  # s; the panel renews its session every 20 minutes while open

UPDATE_INTERVAL = timedelta(hours=6)  # how often the model's repository is asked for a newer release
