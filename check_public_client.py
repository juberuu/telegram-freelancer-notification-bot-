"""Inspect one downloaded public page locally; no API token or Telegram send."""
import json
import sys
from pathlib import Path
from projectbot.public_clients import parse_client, fetch_client

if __name__ == "__main__":
    if sys.argv[1].startswith("https://"):
        result = fetch_client({"url": sys.argv[1], "id": int(sys.argv[2])})
    else:
        result = parse_client(Path(sys.argv[1]).read_text(encoding="utf-8"), int(sys.argv[2]))
    print(json.dumps(result, indent=2))
