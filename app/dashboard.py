from flask import Blueprint, current_app, render_template, request

bp = Blueprint("dashboard", __name__)

SEVERITY_ORDER = ["error", "warning", "notice", "info", "unknown"]


@bp.route("/")
def index():
    client = current_app.extensions["opensearch"]
    prefix = current_app.config["INDEX_PREFIX"]
    size = current_app.config["DASHBOARD_PAGE_SIZE"]
    active_severity = request.args.get("severity") or None

    events = []
    total = 0
    severity_counts = []
    error = None
    try:
        query_clause = (
            {"term": {"severity": active_severity}} if active_severity else {"match_all": {}}
        )
        resp = client.search(
            index=f"{prefix}-*",
            body={
                "size": size,
                "sort": [{"timestamp": {"order": "desc"}}],
                "query": query_clause,
                "aggs": {
                    # "global" ignores the query above, so the severity
                    # breakdown always reflects the full dataset -- the
                    # facets, not just whatever's currently filtered in.
                    "all": {
                        "global": {},
                        "aggs": {"by_severity": {"terms": {"field": "severity", "size": 10}}},
                    }
                },
            },
        )
        events = [hit["_source"] for hit in resp["hits"]["hits"]]
        total = resp["hits"]["total"]["value"]

        buckets = (
            resp.get("aggregations", {})
            .get("all", {})
            .get("by_severity", {})
            .get("buckets", [])
        )
        counts = {b["key"]: b["doc_count"] for b in buckets}
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

    all_total = sum(s["count"] for s in severity_counts)

    return render_template(
        "dashboard.html",
        events=events,
        total=total,
        all_total=all_total,
        severity_counts=severity_counts,
        active_severity=active_severity,
        error=error,
    )
