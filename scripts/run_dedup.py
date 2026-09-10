#!/usr/bin/env python3
"""Standalone entrypoint for the Phase 3 burst-dedup job.

Run every minute or so via prox-siem-dedup.timer (systemd) rather than
in-process inside the Flask app -- gunicorn runs multiple worker
processes, and an in-process scheduler would fire once per worker.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.config import Config  # noqa: E402  (must follow load_dotenv)
from app.dedup import run_once  # noqa: E402
from app.opensearch_client import make_client  # noqa: E402
from app.rules_engine import RuleEngine  # noqa: E402


def main():
    config = {k: v for k, v in vars(Config).items() if not k.startswith("_")}
    client = make_client(config["OPENSEARCH_URL"])
    rules = RuleEngine(config["RULES_PATH"])
    result = run_once(client, config, rules=rules)
    print(f"dedup: processed {result['docs_processed']} docs into {result['buckets']} clusters")


if __name__ == "__main__":
    main()
