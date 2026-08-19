"""Every path and identity the tools need, resolved once.

Defaults keep everything inside the repo's own `data/` directory so a fresh
clone runs with no setup. Override any of them with an environment variable,
or put them in a `.env` file next to serve.py.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_dotenv():
    """Minimal .env reader. Real values only, no interpolation, no quotes handling
    beyond stripping a matched pair."""
    env = REPO / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


_load_dotenv()


def _path(name, default):
    return Path(os.path.expanduser(os.environ.get(name) or str(default)))


# Where working data lives. Everything below defaults inside it.
DATA = _path("JOBSCOUT_DATA", REPO / "data")

DB = _path("JOBSCOUT_DB", DATA / "state.sqlite")
BOARD = _path("JOBSCOUT_BOARD", DATA / "roles.json")
JD_DIR = _path("JOBSCOUT_JDS", DATA / "jds")
BUILD_DIR = _path("JOBSCOUT_BUILDS", DATA / "jdbuilds")
LOG_DIR = _path("JOBSCOUT_LOGS", DATA / "logs")

# Finished CVs. Point this at a notes vault if you keep one.
CV_DIR = _path("JOBSCOUT_CVS", DATA / "cvs")

# Your profile: the single source of truth the CV builder writes from.
PROFILE = _path("JOBSCOUT_PROFILE", DATA / "profile.md")

# Preferences that decide what counts as a match.
PREFS = _path("JOBSCOUT_PREFS", DATA / "prefs.md")

# Generated filenames look like "<prefix><Company> <Track>.pdf". Recruiters see
# this in ATS uploads, so keep it clean and free of dates or run metadata.
CV_PREFIX = os.environ.get("JOBSCOUT_CV_PREFIX", "CV - ")

# Mailbox to read. Only used by fetch_mail.py.
GMAIL_ADDRESS = os.environ.get("JOBSCOUT_GMAIL", "")
IMAP_HOST = os.environ.get("JOBSCOUT_IMAP_HOST", "imap.gmail.com")
KEYCHAIN_SERVICE = os.environ.get("JOBSCOUT_KEYCHAIN", "jobscout-imap")
LABELS = [s for s in os.environ.get("JOBSCOUT_LABELS", "Jobs/Listings,Jobs/Alerts").split(",") if s]

for _d in (DATA, JD_DIR, BUILD_DIR, LOG_DIR, CV_DIR):
    _d.mkdir(parents=True, exist_ok=True)
