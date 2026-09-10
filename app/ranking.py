"""Phase 3 ranking: scores active clusters by
severity_weight x recency x log(frequency + 1), and exposes the result both
as JSON (GET /api/top-important) and as a plain function the dashboard's
"Top Important" view calls directly -- no HTTP round-trip to itself.

log(frequency + 1) rather than log(frequency): a cluster with count=1
(e.g. a single critical backup failure) would otherwise score exactly zero
-- log(1) == 0 -- and get buried under any high-frequency noise regardless
of severity, which is the opposite of what this phase is for.
"""
import math
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("ranking", __name__)

# Cap on distinct hostnames shown in the "Top Important" host facet --
# generous for a homelab, but keeps the agg bounded.
HOSTNAME_FACET_SIZE = 50


def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def score_cluster(cluster, now, half_life_minutes):
    last_seen = _parse_ts(cluster["last_seen"])
    age_minutes = max((now - last_seen).total_seconds() / 60, 0)
    recency = 0.5 ** (age_minutes / half_life_minutes) if half_life_minutes else 1.0
    weight = cluster.get("severity_weight") or 0
    count = cluster.get("count") or 1
    frequency_factor = math.log(count + 1)
    return weight * recency * frequency_factor


def get_top_clusters(client, config, limit=None, hostname=None, category=None, since=None, until=None):
    """since/until are datetimes (tz-aware) bounding cluster.last_seen; since
    defaults to config's RANKING_ACTIVE_WINDOW_HOURS when not given, until is
    open-ended (no upper bound) when not given -- so the default call is
    unchanged from before Phase 4's range picker existed."""
    limit = limit or config["RANKING_DEFAULT_LIMIT"]
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=config["RANKING_ACTIVE_WINDOW_HOURS"])

    last_seen_range = {"gte": since.isoformat()}
    if until is not None:
        last_seen_range["lte"] = until.isoformat()

    filters = [{"range": {"last_seen": last_seen_range}}]
    if hostname:
        filters.append({"term": {"hostname": hostname}})
    if category:
        filters.append({"term": {"category": category}})

    resp = client.search(
        index=config["CLUSTERS_INDEX"],
        body={
            "size": 1000,
            "query": {"bool": {"filter": filters}},
        },
        ignore_unavailable=True,
    )
    clusters = [hit["_source"] for hit in resp.get("hits", {}).get("hits", [])]

    now = datetime.now(timezone.utc)
    for cluster in clusters:
        cluster["score"] = round(score_cluster(cluster, now, config["RANKING_HALF_LIFE_MINUTES"]), 2)

    clusters.sort(key=lambda c: c["score"], reverse=True)
    return clusters[:limit]


def _facet_lookback_query(config):
    """The widest window a cluster could still exist in -- DEDUP_RETENTION_MINUTES,
    not RANKING_ACTIVE_WINDOW_HOURS/the current range picker selection -- so
    host/category filter chips stay stable as you switch time ranges instead
    of hosts disappearing when you narrow to "last hour"."""
    lookback = datetime.now(timezone.utc) - timedelta(minutes=config["DEDUP_RETENTION_MINUTES"])
    return {"range": {"last_seen": {"gte": lookback.isoformat()}}}


def get_cluster_hostnames(client, config):
    """Distinct hostnames among clusters still in the retention window
    (never filtered by hostname itself) -- populates the host filter chips
    regardless of which host, if any, is currently selected."""
    resp = client.search(
        index=config["CLUSTERS_INDEX"],
        body={
            "size": 0,
            "query": _facet_lookback_query(config),
            "aggs": {"by_hostname": {"terms": {"field": "hostname", "size": HOSTNAME_FACET_SIZE}}},
        },
        ignore_unavailable=True,
    )
    buckets = resp.get("aggregations", {}).get("by_hostname", {}).get("buckets", [])
    return sorted(b["key"] for b in buckets if b["key"])


def get_cluster_categories(client, config):
    """Same idea as get_cluster_hostnames but for rules.yaml categories."""
    resp = client.search(
        index=config["CLUSTERS_INDEX"],
        body={
            "size": 0,
            "query": _facet_lookback_query(config),
            "aggs": {"by_category": {"terms": {"field": "category", "size": HOSTNAME_FACET_SIZE}}},
        },
        ignore_unavailable=True,
    )
    buckets = resp.get("aggregations", {}).get("by_category", {}).get("buckets", [])
    return sorted(b["key"] for b in buckets if b["key"])


@bp.route("/api/top-important")
def top_important():
    client = current_app.extensions["opensearch"]
    try:
        limit = int(request.args.get("limit", current_app.config["RANKING_DEFAULT_LIMIT"]))
    except ValueError:
        limit = current_app.config["RANKING_DEFAULT_LIMIT"]
    hostname = request.args.get("hostname") or None
    category = request.args.get("category") or None

    try:
        clusters = get_top_clusters(client, current_app.config, limit=limit, hostname=hostname, category=category)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify({"clusters": clusters})
