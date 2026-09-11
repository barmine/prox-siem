#!/usr/bin/env python3
"""Standalone entrypoint for the Phase 5 volume-anomaly job.

Run hourly via prox-siem-anomaly.timer (systemd) rather than in-process
inside the Flask app -- same reasoning as scripts/run_dedup.py: gunicorn
runs multiple worker processes, and an in-process scheduler would fire
once per worker.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.anomaly import run_once  # noqa: E402  (must follow load_dotenv)
from app.config import Config  # noqa: E402
from app.opensearch_client import make_client  # noqa: E402
from app.rules_engine import RuleEngine  # noqa: E402


def main():
    config = {k: v for k, v in vars(Config).items() if not k.startswith("_")}
    client = make_client(config["OPENSEARCH_URL"])
    rules = RuleEngine(config["RULES_PATH"])
    result = run_once(client, config, rules=rules)
    print(
        f"anomaly: evaluated {result['sources_evaluated']} sources, "
        f"{result['anomalies']} anomalous ({result['muted']} muted)"
    )


if __name__ == "__main__":
    main()
