#!/usr/bin/env python3
"""Phase 3 end-to-end test: sends synthetic events through both ingestion
paths, runs the dedup job once, and checks the results are tagged,
deduplicated, and ranked the way they should be.

Run from the repo checkout with the same venv as the app:
    .venv/bin/python scripts/test_phase3.py [--cleanup]

Needs OPENSEARCH_URL from .env (same as the app), SHARED_SECRET (same),
and SIEM_BASE_URL (default http://localhost:5000) for the notification-path
check, which goes through the real Flask ingestion API rather than writing
to OpenSearch directly.
"""
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

load_dotenv()

import os  # noqa: E402  (must follow load_dotenv)

from app.config import Config  # noqa: E402
from app.dedup import run_once  # noqa: E402
from app.opensearch_client import make_client  # noqa: E402
from app.ranking import get_top_clusters  # noqa: E402

BASE_URL = os.environ.get("SIEM_BASE_URL", "http://localhost:5000")
PIPELINE = "proxmox-logs-rules"

RUN_ID = uuid.uuid4().hex[:8]
HOSTNAME = f"test-host-{RUN_ID}"

PASS = []
FAIL = []


def check(label, condition, detail=""):
    if condition:
        PASS.append(label)
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}  {detail}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def index_prefix():
    return Config.INDEX_PREFIX


def index_name():
    return f"{index_prefix()}-{datetime.now(timezone.utc):%Y.%m.%d}"


def raw_doc(client, **fields):
    """Writes a Fluent-Bit-shaped document straight to OpenSearch through
    the real ingest pipeline, exercising the Painless-based tagging path
    (not the Python one -- that's what the notification_event() path below
    is for)."""
    body = {
        "timestamp": now_iso(),
        "hostname": HOSTNAME,
        "source_type": "journal",
        "title": "",
        "fields": {"test_run": RUN_ID},
    }
    body.update(fields)
    client.index(index=index_name(), body=body, pipeline=PIPELINE, refresh=True)


def notification_event(**overrides):
    payload = {
        "timestamp": time.time(),
        "severity": "error",
        "title": "Backup job failed",
        "message": "Backup job for VM 101 failed: no space left on device",
        "fields": {"type": "backup", "hostname": HOSTNAME, "test_run": RUN_ID},
    }
    payload.update(overrides)
    resp = requests.post(
        f"{BASE_URL}/api/events",
        json=payload,
        headers={"X-Prox-Siem-Secret": Config.SHARED_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def wait_for_count(client, query, expected_min, tries=10, delay=0.5):
    count = 0
    for _ in range(tries):
        count = client.count(index=f"{index_prefix()}-*", body={"query": query})["count"]
        if count >= expected_min:
            return count
        time.sleep(delay)
    return count


def main():
    cleanup = "--cleanup" in sys.argv
    client = make_client(Config.OPENSEARCH_URL)

    print(f"Run ID: {RUN_ID}  (hostname tag: {HOSTNAME})")

    print("\n1. Notification path -- fake backup-job-failed event (via Flask API)")
    notification_event()
    time.sleep(1)
    resp = client.search(
        index=f"{index_prefix()}-*",
        body={
            "query": {"term": {"fields.test_run.keyword": RUN_ID}},
            "size": 1,
            "sort": [{"timestamp": "desc"}],
        },
    )
    hit = resp["hits"]["hits"][0]["_source"] if resp["hits"]["hits"] else {}
    check("backup notification tagged rule_id=backup-job-failed", hit.get("rule_id") == "backup-job-failed", hit)
    check("backup notification category=Backup", hit.get("category") == "Backup", hit)
    check("backup notification severity_weight=100", hit.get("severity_weight") == 100, hit)

    print("\n2. Raw journal path -- fake ZFS DEGRADED line (via ingest pipeline)")
    raw_doc(client, unit="kernel", message="WARNING: pool 'tank' state is DEGRADED")

    print("\n3. Raw journal path -- 8 fake PAM auth failures from the same source")
    for i in range(8):
        raw_doc(
            client,
            unit="ssh.service",
            message=f"Failed password for root from 203.0.113.9 port {40000 + i} ssh2",
        )

    print("\n4. Raw journal path -- 50 identical firewall drops")
    for _ in range(50):
        raw_doc(
            client,
            unit="kernel",
            message="proxmox-firewall: DROP: IN=vmbr0 OUT= SRC=198.51.100.7 DST=192.0.2.10 PROTO=TCP DPT=445",
        )

    count = wait_for_count(client, {"term": {"fields.test_run.keyword": RUN_ID}}, expected_min=60)
    print(f"\nIndexed {count} synthetic documents total (expected 60).")
    check("all synthetic docs landed", count >= 60, f"got {count}")

    resp = client.search(
        index=f"{index_prefix()}-*",
        body={
            "query": {
                "bool": {"filter": [{"term": {"fields.test_run.keyword": RUN_ID}}, {"term": {"unit": "kernel"}}]}
            },
            "size": 100,
        },
    )
    kernel_docs = [h["_source"] for h in resp["hits"]["hits"]]
    zfs_hits = [d for d in kernel_docs if d.get("rule_id") == "zfs-pool-degraded"]
    fw_hits = [d for d in kernel_docs if d.get("rule_id") == "firewall-drop-wan"]
    check("ZFS line tagged zfs-pool-degraded", len(zfs_hits) == 1, f"got {len(zfs_hits)}")
    check("all 50 firewall lines tagged firewall-drop-wan", len(fw_hits) == 50, f"got {len(fw_hits)}")

    resp = client.search(
        index=f"{index_prefix()}-*",
        body={
            "query": {
                "bool": {
                    "filter": [{"term": {"fields.test_run.keyword": RUN_ID}}, {"term": {"unit": "ssh.service"}}]
                }
            },
            "size": 20,
        },
    )
    pam_hits = [h["_source"] for h in resp["hits"]["hits"] if h["_source"].get("rule_id") == "pam-auth-failure"]
    check("all 8 auth-failure lines tagged pam-auth-failure", len(pam_hits) == 8, f"got {len(pam_hits)}")

    print("\n5. Running the dedup job once")
    config = {k: v for k, v in vars(Config).items() if not k.startswith("_")}
    result = run_once(client, config)
    print(f"  dedup processed {result['docs_processed']} matched docs cluster-wide into {result['buckets']} clusters")

    client.indices.refresh(index=config["CLUSTERS_INDEX"])
    resp = client.search(
        index=config["CLUSTERS_INDEX"],
        body={"query": {"term": {"hostname": HOSTNAME}}, "size": 20},
    )
    clusters = {c["_source"]["rule_id"]: c["_source"] for c in resp["hits"]["hits"]}

    check(
        "firewall-drop-wan clustered with count=50",
        clusters.get("firewall-drop-wan", {}).get("count") == 50,
        clusters.get("firewall-drop-wan"),
    )
    check(
        "pam-auth-failure clustered with count=8",
        clusters.get("pam-auth-failure", {}).get("count") == 8,
        clusters.get("pam-auth-failure"),
    )
    check(
        "zfs-pool-degraded clustered with count=1",
        clusters.get("zfs-pool-degraded", {}).get("count") == 1,
        clusters.get("zfs-pool-degraded"),
    )

    print("\n6. Checking ranking (severity x recency x log(frequency))")
    ranked = get_top_clusters(client, config, limit=1000)
    ranked_by_rule = {c["rule_id"]: c for c in ranked if c.get("hostname") == HOSTNAME}
    for rule_id, c in sorted(ranked_by_rule.items(), key=lambda kv: -kv[1]["score"]):
        print(f"  {rule_id:<22} score={c['score']:<10} count={c['count']}")

    if "pam-auth-failure" in ranked_by_rule and "firewall-drop-wan" in ranked_by_rule:
        check(
            "critical+frequent (pam-auth-failure) outranks info+frequent (firewall-drop-wan)",
            ranked_by_rule["pam-auth-failure"]["score"] > ranked_by_rule["firewall-drop-wan"]["score"],
        )
    if "zfs-pool-degraded" in ranked_by_rule and "firewall-drop-wan" in ranked_by_rule:
        check(
            "critical+rare (zfs-pool-degraded) still outranks info+frequent (firewall-drop-wan)",
            ranked_by_rule["zfs-pool-degraded"]["score"] > ranked_by_rule["firewall-drop-wan"]["score"],
        )

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed.")

    if cleanup:
        print("\nCleaning up synthetic documents and clusters...")
        client.delete_by_query(
            index=f"{index_prefix()}-*",
            body={"query": {"term": {"fields.test_run.keyword": RUN_ID}}},
            conflicts="proceed",  # docs were just bulk-updated by dedup; a stale
            # read during delete_by_query's own search phase can 409 on
            # individual docs -- best-effort cleanup, don't crash over it.
        )
        client.delete_by_query(
            index=config["CLUSTERS_INDEX"],
            body={"query": {"term": {"hostname": HOSTNAME}}},
            ignore_unavailable=True,
            conflicts="proceed",
        )

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
