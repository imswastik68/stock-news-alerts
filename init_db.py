"""One-shot schema setup for the stock-news-alerts SQLite database."""

from src.config import get_settings
from src.storage.db import init_db

if __name__ == "__main__":
    settings = get_settings()
    init_db()
    print(f"Database initialized at {settings.db_path}")
