"""Settings. Everything can be overridden with environment variables or a local .env file."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

BASE_URL = os.environ.get("BELLHAVEN_BASE_URL", "https://analyst-assessment-production.up.railway.app")
API_URL = BASE_URL + "/api/v1"
API_TOKEN = os.environ.get("BELLHAVEN_TOKEN", "")

# Optional: link from the review app straight to the read-only CRM browser.
CRM_BROWSER_URL = os.environ.get("CRM_BROWSER_URL", "")

DB_PATH = Path(os.environ.get("SYNC_DB_PATH", ROOT / "sync_state.db"))

# We look the parent up by name at runtime instead of hardcoding its id.
OPERATOR_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

# Tag added to every note we write, so changes made by this tool are easy to find.
NOTE_TAG = "[bellhaven-sync]"
