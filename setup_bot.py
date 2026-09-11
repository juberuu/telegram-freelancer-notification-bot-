"""Pair one private Telegram account without a third-party ID lookup bot."""
import getpass
import os
import re
import secrets
import time
from projectbot.api import APIError, Telegram
from projectbot.core import ROOT


def main():
    if (ROOT / ".env").exists():
        raise SystemExit("Setup already exists in .env. Edit it locally, or rename it before pairing a different bot.")
    print("Create a bot with https://t.me/BotFather using /newbot, then paste its token here.")
    token = getpass.getpass("Bot token (hidden): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise SystemExit("That does not look like a Telegram bot token.")
    tg = Telegram(token)
    identity = tg.call("getMe")
    if tg.call("getWebhookInfo").get("url"):
        raise SystemExit("An existing webhook is configured. Create a dedicated bot or remove its webhook yourself.")
    code = secrets.token_hex(8)
    print(f"\nOpen https://t.me/{identity['username']} and send this exact message privately:\n\n/start {code}\n\nWaiting up to 5 minutes...")
    offset = 0
    deadline = time.monotonic() + 300
    owner = None
    while time.monotonic() < deadline and owner is None:
        for update in tg.call("getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": ["message"]}):
            offset = update["update_id"] + 1
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            if (chat.get("type") == "private" and msg.get("text", "").strip() == f"/start {code}"
                    and msg.get("from", {}).get("id") == chat.get("id")):
                owner = chat["id"]
    if owner is None:
        raise SystemExit("Pairing timed out. Run setup again.")
    # Acknowledge setup updates before handing polling to the running application.
    tg.call("getUpdates", {"offset": offset, "timeout": 0, "limit": 1})
    path = ROOT / ".env"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={owner}\nFREELANCER_OAUTH_TOKEN=\n")
    print("\nPaired. Run: python -m projectbot.app\nThen send /start to your bot to activate alerts.")


if __name__ == "__main__":
    try:
        main()
    except APIError as e:
        raise SystemExit(str(e) + ". Check your internet connection and token, then try again.") from None
