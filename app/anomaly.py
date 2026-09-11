"""Phase 5 volume-baseline anomaly detection: a second detection path
alongside rules.yaml's pattern matching, for the thing pattern rules
structurally can't catch -- a normally-quiet log type suddenly spiking,
with no single line matching a known-bad regex.

Stateless and periodic, same shape as app/dedup.py and for the same
reason (gunicorn's multiple workers would double-fire an in-process
scheduler): run via prox-siem-anomaly.timer + scripts/run_anomaly.py,
independently of the dedup job.

Baseline unit is (hostname, unit) -- `unit` is the field
fluentbit/filters.lua already sets on every doc (systemd unit name, task
worker_type, or "pveproxy-access"), so this needs no new tagging.
source_type: heartbeat is excluded -- synthetic, constant-rate, not
meaningful to baseline.

One query per run: an OpenSearch multi_terms agg grouping by
(hostname, unit), each with a date_histogram (1h buckets) sub-agg
spanning ANOMALY_BASELINE_DAYS + 1h up to now. The LAST bucket in each
series is the value being checked (the current, possibly-partial hour);
every earlier bucket is the baseline pool. This sidesteps partial-hour
alignment entirely rather than trying to sync to hour boundaries.

Feeds the same proxmox-clusters index app/dedup.py writes, tagged
detection_method: "anomaly" and rule_id: "anomaly:<unit>" -- the
synthetic rule_id means app/mutes.py's mute mechanism works identically
for both detection methods with no special-casing, and Overview's
existing rule_id-keyed rendering needs no changes to display these.
"""
import statistics
from datetime import datetime, timedelta, timezone

from .alerting import apply_alert_throttle
from .dedup import _cluster_key
from .mutes import get_active_mutes, is_muted

ANOMALY_CATEGORY = "Anomaly"


def _severity_weight(rules, is_critical):
    """Maps an anomaly's warning/critical tier onto the same weight scale
    rules.yaml uses, so it sorts/colors identically to a pattern-rule hit
    of that severity on Overview."""
    label = "critical" if is_critical else "warning"
    if rules is not None:
        weight = rules.severities.get(label)
        if weight is not None:
            return weight
    return 100 if is_critical else 50


def _evaluate(counts, config):
    """counts is the hourly bucket series, oldest first, last = current
    (possibly partial) hour. Returns (is_anomaly, is_critical, current,
    mean, stdev) or None if there's not enough history to judge."""
    if len(counts) < 2:
        return None
    baseline = counts[:-1]
    current = counts[-1]
    if current < config["ANOMALY_MIN_COUNT"]:
        return None

    mean = statistics.mean(baseline)
    stdev = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0

    if stdev > 0:
        z = (current - mean) / stdev
        is_anomaly = z >= config["ANOMALY_ZSCORE_THRESHOLD"]
        is_critical = z >= config["ANOMALY_ZSCORE_CRITICAL"]
    else:
        # Zero-variance baseline (often a normally-silent source) -- z-score
        # is undefined. Fall back to an absolute/multiplier floor so "quiet
        # type suddenly spiking" still fires.
        threshold = max(config["ANOMALY_MIN_COUNT"], mean * config["ANOMALY_MULTIPLIER_FALLBACK"])
        is_anomaly = current > mean and current >= threshold
        is_critical = is_anomaly and current >= threshold * 2

    return is_anomaly, is_critical, current, mean, stdev


def _describe(unit, hostname, current, mean, stdev):
    prefix = f"{unit} on {hostname}: {current} events in the last hour"
    if not mean:
        return f"{prefix}, no prior baseline activity"
    if stdev:
        z = (current - mean) / stdev
        return f"{prefix} vs a baseline of {mean:.1f} (±{stdev:.1f}) -- z={z:.1f}"
    return f"{prefix} vs a baseline of {mean:.1f} -- {current / mean:.1f}x normal"


def run_once(client, config, rules=None):
    """rules is a RuleEngine (app/rules_engine.py) -- used for severity
    weight lookups and Phase 4 alerting, same as app/dedup.py. Pass None
    to skip alerting."""
    prefix = config["INDEX_PREFIX"]
    clusters_index = config["CLUSTERS_INDEX"]
    baseline_days = config["ANOMALY_BASELINE_DAYS"]
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=baseline_days, hours=1)

    resp = client.search(
        index=f"{prefix}-*",
        body={
            "size": 0,
            "query": {
                "bool": {
                    "filter": [{"range": {"timestamp": {"gte": window_start.isoformat()}}}],
                    "must_not": [{"term": {"source_type": "heartbeat"}}],
                }
            },
            "aggs": {
                "sources": {
                    "multi_terms": {
                        "terms": [{"field": "hostname"}, {"field": "unit"}],
                        "size": config["ANOMALY_MAX_SOURCES"],
                    },
                    "aggs": {
                        "hourly": {
                            "date_histogram": {
                                "field": "timestamp",
                                "fixed_interval": "1h",
                                "min_doc_count": 0,
                                "extended_bounds": {"min": window_start.isoformat(), "max": now.isoformat()},
                            }
                        }
                    },
                }
            },
        },
    )

    mutes = get_active_mutes(client, config)
    source_buckets = resp.get("aggregations", {}).get("sources", {}).get("buckets", [])

    evaluated = 0
    anomalies = {}
    muted_keys = []
    for source in source_buckets:
        hostname, unit = source["key"]
        if not hostname or not unit:
            continue
        counts = [b["doc_count"] for b in source.get("hourly", {}).get("buckets", [])]
        result = _evaluate(counts, config)
        evaluated += 1
        if result is None:
            continue
        is_anomaly, is_critical, current, mean, stdev = result
        if not is_anomaly:
            continue

        rule_id = f"anomaly:{unit}"
        key = _cluster_key(rule_id, hostname, "")

        if is_muted(mutes, rule_id, hostname):
            muted_keys.append(key)
            client.delete(index=clusters_index, id=key, ignore=[404])
            continue

        severity_weight = _severity_weight(rules, is_critical)
        sample_message = _describe(unit, hostname, current, mean, stdev)

        anomalies[key] = {
            "cluster_key": key,
            "rule_id": rule_id,
            "category": ANOMALY_CATEGORY,
            "severity_weight": severity_weight,
            "hostname": hostname,
            "sample_message": sample_message,
            "count": current,
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
        }

    for cluster in anomalies.values():
        alerted = apply_alert_throttle(
            client, clusters_index, cluster["cluster_key"], rules, cluster["severity_weight"], cluster, config
        )
        client.index(
            index=clusters_index,
            id=cluster["cluster_key"],
            body={**cluster, "updated_at": now.isoformat(), "alerted": alerted, "detection_method": "anomaly"},
        )

    # Resolved anomalies (no longer firing) age out on their own schedule,
    # scoped strictly to detection_method: anomaly so this never touches
    # app/dedup.py's pattern-rule clusters in the same index.
    seen_keys = list(anomalies.keys())
    must_not = [{"terms": {"cluster_key": seen_keys}}] if seen_keys else []
    retention = timedelta(minutes=config["DEDUP_RETENTION_MINUTES"])
    client.delete_by_query(
        index=clusters_index,
        body={
            "query": {
                "bool": {
                    "must_not": must_not,
                    "filter": [
                        {"term": {"detection_method": "anomaly"}},
                        {"range": {"last_seen": {"lt": (now - retention).isoformat()}}},
                    ],
                }
            }
        },
        ignore_unavailable=True,
        conflicts="proceed",
    )

    return {"sources_evaluated": evaluated, "anomalies": len(anomalies), "muted": len(muted_keys)}
