import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')


def api_key() -> str:
    return os.getenv('TWOGIS_API_KEY', '').strip()


def database_path() -> Path:
    value = Path(os.getenv('DATABASE_PATH', 'data/eknot.sqlite3'))
    return value if value.is_absolute() else ROOT / value
