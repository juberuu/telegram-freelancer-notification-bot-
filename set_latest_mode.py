"""Apply latest-project settings without touching credentials or alert history."""
import os
from projectbot.app import load_env
from projectbot.core import ROOT, Store


def main():
    load_env()
    store = Store(os.getenv("BOT_DB_PATH", str(ROOT / "data" / "bot.sqlite3")))
    try:
        with store.lock, store.db:
            cfg = store.get("config")
            cfg.update(max_age_minutes=5, interval_seconds=30)
            store.set("config", cfg)
            store.set("fresh_alerts_v2", True)
        print("Saved: maximum project age 5 minutes; scan interval 30 seconds.")
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
