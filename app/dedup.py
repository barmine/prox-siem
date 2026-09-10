"""Phase 3 burst dedup: groups rule-matched events into rolling clusters.

Run periodically (see prox-siem-dedup.timer + scripts/run_dedup.py) rather
than in-process inside the Flask app: gunicorn runs multiple worker
processes (-w 2), and an in-process scheduler would fire once per worker,
double-processing everything.

Stateless by design: each run recomputes clusters from scratch over a
rolling lookback window instead of keeping a cursor. That means a
cluster's count/first_seen/last_seen describe "within the last
DEDUP_LOOKBACK_MINUTES", not an ever-growing total -- which is exactly
what "what's actually wrong right now" needs, and it means there's no
cursor state that can drift or double-count if a run is skipped or overlaps.
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone

from opensearchpy.exceptions import NotFoundError

from .alerting import send_alert

_DIGITS = re.compile(r"\d+")


def _normalize_message(message):
    """Collapses the parts of a message that vary occurrence-to-occurrence
    (ports, PIDs, IP octets, ...) so near-identical lines land in the same
    bucket -- e.g. 500 firewall drops that only differ by source port."""
    text = (message or "").strip().lower()
    text = _DIGITS.sub("#", text)
    return text[:160]


def _cluster_key(rule_id, hostname, normalized_message):
    raw = f"{rule_id}|{hostname}|{normalized_message}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def run_once(client, config, rules=None):
    """rules is a RuleEngine (app/rules_engine.py), used only to decide
    whether a cluster is newly critical for Phase 4 alerting -- pass None
    to skip alerting entirely (e.g. one-off/test invocations that don't
    care about it)."""
    prefix = config["INDEX_PREFIX"]
    clusters_index = config["CLUSTERS_INDEX"]
    lookback = timedelta(minutes=config["DEDUP_LOOKBACK_MINUTES"])
    retention = timedelta(minutes=config["DEDUP_RETENTION_MINUTES"])
    now = datetime.now(timezone.utc)
    window_start = now - lookback

    resp = client.search(
        index=f"{prefix}-*",
        body={
            "size": config["DEDUP_MAX_DOCS"],
            "query": {
                "bool": {
                    "filter": [
                        {"exists": {"field": "rule_id"}},
                        {"range": {"timestamp": {"gte": window_start.isoformat()}}},
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "asc"}}],
        },
    )
    hits = resp["hits"]["hits"]

    buckets = {}
    for hit in hits:
        doc = hit["_source"]
        rule_id = doc.get("rule_id")
        hostname = doc.get("hostname") or "unknown"
        normalized = _normalize_message(doc.get("message"))
        key = _cluster_key(rule_id, hostname, normalized)

        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "cluster_key": key,
                "rule_id": rule_id,
                "category": doc.get("category"),
                "severity_weight": doc.get("severity_weight"),
                "hostname": hostname,
                "sample_message": doc.get("message"),
                "count": 0,
                "first_seen": doc["timestamp"],
                "last_seen": doc["timestamp"],
                "doc_ids": [],
            }
            buckets[key] = bucket

        bucket["count"] += 1
        bucket["last_seen"] = doc["timestamp"]  # hits sorted asc by timestamp
        bucket["doc_ids"].append((hit["_index"], hit["_id"]))

    for bucket in buckets.values():
        # Phase 4 alerting: fire once when a cluster crosses into critical,
        # not on every recurrence. `alerted` lives on the cluster doc
        # itself so the throttle survives across runs with no extra
        # infra -- and a cluster that ages out (deleted by the retention
        # cleanup below) and later reappears as a fresh burst naturally
        # gets a new alert, which is correct: that's a new occurrence.
        alerted = False
        if rules is not None and rules.label_for_weight(bucket["severity_weight"]) == "critical":
            try:
                existing = client.get(index=clusters_index, id=bucket["cluster_key"])
                alerted = bool(existing["_source"].get("alerted"))
            except NotFoundError:
                alerted = False

            if not alerted:
                try:
                    send_alert(config, bucket)
                except Exception:
                    pass  # best-effort -- a broken webhook target shouldn't break dedup
            alerted = True

        client.index(
            index=clusters_index,
            id=bucket["cluster_key"],
            body={
                "cluster_key": bucket["cluster_key"],
                "rule_id": bucket["rule_id"],
                "category": bucket["category"],
                "severity_weight": bucket["severity_weight"],
                "hostname": bucket["hostname"],
                "sample_message": bucket["sample_message"],
                "count": bucket["count"],
                "first_seen": bucket["first_seen"],
                "last_seen": bucket["last_seen"],
                "updated_at": now.isoformat(),
                "alerted": alerted,
            },
        )

        if bucket["doc_ids"]:
            bulk_body = []
            for doc_index, doc_id in bucket["doc_ids"]:
                bulk_body.append({"update": {"_index": doc_index, "_id": doc_id}})
                bulk_body.append({"doc": {"cluster_key": bucket["cluster_key"]}})
            client.bulk(body=bulk_body)

    # Tidy up clusters that fell out of the window and are old enough to be
    # clearly resolved rather than just quiet between bursts.
    seen_keys = list(buckets.keys())
    must_not = [{"terms": {"cluster_key": seen_keys}}] if seen_keys else []
    client.delete_by_query(
        index=clusters_index,
        body={
            "query": {
                "bool": {
                    "must_not": must_not,
                    "filter": [{"range": {"last_seen": {"lt": (now - retention).isoformat()}}}],
                }
            }
        },
        ignore_unavailable=True,
        conflicts="proceed",  # best-effort cleanup -- a version race against a
        # concurrent run shouldn't fail the whole job.
    )

    return {"buckets": len(buckets), "docs_processed": len(hits)}
