import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.getenv("CRASHCUSTOMER_DATA_DIR", str(ROOT / "data")))
BASE_URL = os.getenv("CRASHCUSTOMER_BASE_URL", "http://127.0.0.1:8710").rstrip("/")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
DEFAULT_BACKEND = os.getenv("CRASHCUSTOMER_BACKEND", "rule")


def browser_options() -> dict:
    options = {"headless": os.getenv("CRASHCUSTOMER_HEADLESS", "true").lower() != "false"}
    executable = os.getenv("CRASHCUSTOMER_BROWSER_EXECUTABLE")
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if executable:
        options["executable_path"] = executable
    elif chrome.exists():
        options["executable_path"] = str(chrome)
    return options
