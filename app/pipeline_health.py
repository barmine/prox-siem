"""Phase 4: pipeline health -- tracks a last-seen timestamp per ingestion
source (each Fluent-Bit-shipping host via a heartbeat, plus the
notification-webhook path as its own source) and flags anything that's
gone quiet longer than a reasonable threshold for that kind of source.

Why a heartbeat rather than inferring liveness from real log volume: a
quiet PBS host doing nothing legitimate is indistinguishable from a dead
Fluent Bit agent if you're just watching for *any* document to show up.
The heartbeat (a `dummy` Fluent Bit input firing every 60s, see
fluentbit/conf.d/common.conf) answers "is the agent alive and can it
reach OpenSearch" directly, independent of whatever else is or isn't
happening on the host. The notification-webhook path gets a much longer
threshold by design -- Proxmox only sends a notification when something
actually happens, so quiet there can be entirely normal.
"""
from datetime import datetime, timezone

from flask import Blueprint, current_app, render_template

bp = Blueprint("pipeline_health", __name__)

HEARTBEAT_FACET_SIZE = 50


def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_duration(seconds):
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _make_source(name, kind, last_seen, now, threshold_seconds):
    quiet_seconds = None if last_seen is None else (now - last_seen).total_seconds()
    return {
        "name": name,
        "kind": kind,
        "last_seen": last_seen,
        "quiet_label": _format_duration(quiet_seconds),
        "threshold_label": _format_duration(threshold_seconds),
        "stale": quiet_seconds is None or quiet_seconds > threshold_seconds,
    }


def get_pipeline_sources(client, config):
    prefix = config["INDEX_PREFIX"]
    now = datetime.now(timezone.utc)
    sources = []

    # Per-host Fluent Bit heartbeat.
    resp = client.search(
        index=f"{prefix}-*",
        body={
            "size": 0,
            "query": {"term": {"source_type": "heartbeat"}},
            "aggs": {
                "by_hostname": {
                    "terms": {"field": "hostname", "size": HEARTBEAT_FACET_SIZE},
                    "aggs": {"last_seen": {"max": {"field": "timestamp"}}},
                }
            },
        },
        ignore_unavailable=True,
    )
    heartbeat_threshold = config["PIPELINE_HEARTBEAT_STALE_MINUTES"] * 60
    for bucket in resp.get("aggregations", {}).get("by_hostname", {}).get("buckets", []):
        last_seen_ms = bucket.get("last_seen", {}).get("value")
        last_seen = datetime.fromtimestamp(last_seen_ms / 1000, tz=timezone.utc) if last_seen_ms else None
        sources.append(_make_source(bucket["key"], "fluent-bit", last_seen, now, heartbeat_threshold))

    # Notification-webhook path: one source, not per-host. Every doc
    # Fluent Bit ships carries `_fb_tag` (see fluentbit/conf.d/*.conf's
    # `Include_Tag_Key On`); webhook docs (app/ingest.py) never do, so
    # "missing _fb_tag" is exactly "came through the webhook path."
    resp = client.search(
        index=f"{prefix}-*",
        body={
            "size": 1,
            "query": {"bool": {"must_not": [{"exists": {"field": "_fb_tag"}}]}},
            "sort": [{"timestamp": {"order": "desc"}}],
        },
        ignore_unavailable=True,
    )
    hits = resp.get("hits", {}).get("hits", [])
    last_seen = _parse_ts(hits[0]["_source"]["timestamp"]) if hits else None
    webhook_threshold = config["PIPELINE_WEBHOOK_STALE_HOURS"] * 3600
    sources.append(_make_source("notification-webhook", "webhook", last_seen, now, webhook_threshold))

    sources.sort(key=lambda s: (not s["stale"], s["name"]))
    return sources


@bp.route("/pipeline-health")
def index():
    client = current_app.extensions["opensearch"]
    error = None
    sources = []
    try:
        sources = get_pipeline_sources(client, current_app.config)
    except Exception as exc:
        error = str(exc)

    return render_template("pipeline_health.html", sources=sources, active_view="pipeline_health", error=error)
