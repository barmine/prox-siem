from flask import Blueprint, current_app, render_template

bp = Blueprint("dashboard", __name__)

SEVERITY_ORDER = ["error", "warning", "notice", "info", "unknown"]


@bp.route("/")
def index():
    client = current_app.extensions["opensearch"]
    prefix = current_app.config["INDEX_PREFIX"]
    size = current_app.config["DASHBOARD_PAGE_SIZE"]

    events = []
    total = 0
    severity_counts = []
    error = None
    try:
        resp = client.search(
            index=f"{prefix}-*",
            body={
                "size": size,
                "sort": [{"timestamp": {"order": "desc"}}],
                "aggs": {"by_severity": {"terms": {"field": "severity", "size": 10}}},
            },
        )
        events = [hit["_source"] for hit in resp["hits"]["hits"]]
        total = resp["hits"]["total"]["value"]

        counts = {
            b["key"]: b["doc_count"]
            for b in resp.get("aggregations", {}).get("by_severity", {}).get("buckets", [])
        }
        max_count = max(counts.values()) if counts else 0
        severity_counts = [
            {
                "severity": sev,
                "count": counts.get(sev, 0),
                "pct": round((counts.get(sev, 0) / max_count) * 100) if max_count else 0,
            }
            for sev in SEVERITY_ORDER
            if counts.get(sev, 0) > 0
        ]
    except Exception as exc:
        error = str(exc)

    return render_template(
        "dashboard.html",
        events=events,
        total=total,
        severity_counts=severity_counts,
        error=error,
    )
