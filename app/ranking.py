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


def get_top_clusters(client, config, limit=None):
    limit = limit or config["RANKING_DEFAULT_LIMIT"]
    active_since = datetime.now(timezone.utc) - timedelta(hours=config["RANKING_ACTIVE_WINDOW_HOURS"])

    resp = client.search(
        index=config["CLUSTERS_INDEX"],
        body={
            "size": 1000,
            "query": {"range": {"last_seen": {"gte": active_since.isoformat()}}},
        },
        ignore_unavailable=True,
    )
    clusters = [hit["_source"] for hit in resp.get("hits", {}).get("hits", [])]

    now = datetime.now(timezone.utc)
    for cluster in clusters:
        cluster["score"] = round(score_cluster(cluster, now, config["RANKING_HALF_LIFE_MINUTES"]), 2)

    clusters.sort(key=lambda c: c["score"], reverse=True)
    return clusters[:limit]


@bp.route("/api/top-important")
def top_important():
    client = current_app.extensions["opensearch"]
    try:
        limit = int(request.args.get("limit", current_app.config["RANKING_DEFAULT_LIMIT"]))
    except ValueError:
        limit = current_app.config["RANKING_DEFAULT_LIMIT"]

    try:
        clusters = get_top_clusters(client, current_app.config, limit=limit)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify({"clusters": clusters})
