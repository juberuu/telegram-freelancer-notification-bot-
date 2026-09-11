"""Read-only live check: retrieve projects and print aggregate results only."""
from projectbot.app import load_env
from projectbot.api import APIError, Freelancer
import json
from projectbot.core import ROOT, matches

if __name__ == "__main__":
    load_env()
    try:
        projects, warnings = Freelancer().fetch(60)
        cfg = json.loads((ROOT / "config.example.json").read_text())
        print(json.dumps({"fetched": len(projects), "default_filter_matches": sum(matches(p, cfg) for p in projects), "warnings": warnings}, indent=2))
    except APIError as e:
        raise SystemExit(str(e)) from None
