from flask import Blueprint, current_app, render_template, request

from .ranking import get_top_clusters

bp = Blueprint("dashboard", __name__)

SEVERITY_ORDER = ["error", "warning", "notice", "info", "unknown"]
PAGE_SIZE_OPTIONS = [25, 50, 100]

# "all" still caps here rather than being truly unbounded -- this matches
# OpenSearch's own default index.max_result_window, so it can't 500 on a
# large index.
ALL_SIZE_CAP = 10000

# Cap on distinct hostnames shown in the host facet -- generous for a
# homelab (a handful of Proxmox hosts), but keeps the agg bounded.
HOSTNAME_FACET_SIZE = 50


@bp.route("/")
def index():
    client = current_app.extensions["opensearch"]
    prefix = current_app.config["INDEX_PREFIX"]
    active_severity = request.args.get("severity") or None
    active_cluster = request.args.get("cluster") or None
    active_hostname = request.args.get("hostname") or None

    page_size_arg = request.args.get("page_size", "")
    if page_size_arg == "all":
        page_size = "all"
    else:
        try:
            page_size = int(page_size_arg)
        except ValueError:
            page_size = None
        if page_size not in PAGE_SIZE_OPTIONS:
            default_size = current_app.config["DASHBOARD_PAGE_SIZE"]
            page_size = default_size if default_size in PAGE_SIZE_OPTIONS else PAGE_SIZE_OPTIONS[1]

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    if page_size == "all":
        size = ALL_SIZE_CAP
        page = 1
    else:
        size = page_size

    events = []
    total = 0
    total_pages = 1
    severity_counts = []
    hostnames = []
    error = None
    try:
        filters = []
        if active_severity:
            filters.append({"term": {"severity": active_severity}})
        if active_cluster:
            filters.append({"term": {"cluster_key": active_cluster}})
        if active_hostname:
            filters.append({"term": {"hostname": active_hostname}})
        query_clause = {"bool": {"filter": filters}} if filters else {"match_all": {}}

        body = {
            "size": size,
            "from": (page - 1) * size,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": query_clause,
        }
        # Skip the facet breakdown while drilling into one cluster -- a
        # cluster is already tied to one hostname, so neither facet is
        # useful there.
        if not active_cluster:
            # The severity counts should reflect "how many of each severity
            # for whatever's currently selected" -- so they respect the
            # active hostname filter (drop severity to all-if-selected,
            # all-hosts otherwise) but deliberately exclude the severity
            # filter itself, or picking one severity would zero out the
            # others instead of letting you switch between them.
            severity_scope_filters = []
            if active_hostname:
                severity_scope_filters.append({"term": {"hostname": active_hostname}})
            severity_scope = (
                {"bool": {"filter": severity_scope_filters}} if severity_scope_filters else {"match_all": {}}
            )

            body["aggs"] = {
                # True "global" agg: ignores the query entirely, so every
                # host always appears as a filter option regardless of
                # which severity/host is currently selected.
                "hosts": {
                    "global": {},
                    "aggs": {"by_hostname": {"terms": {"field": "hostname", "size": HOSTNAME_FACET_SIZE}}},
                },
                "severity_scope": {
                    "filter": severity_scope,
                    "aggs": {"by_severity": {"terms": {"field": "severity", "size": 10}}},
                },
            }

        resp = client.search(index=f"{prefix}-*", body=body)
        events = [hit["_source"] for hit in resp["hits"]["hits"]]
        total = resp["hits"]["total"]["value"]
        total_pages = 1 if page_size == "all" else max(1, -(-total // size))  # ceil division

        if not active_cluster:
            aggregations = resp.get("aggregations", {})

            sev_buckets = aggregations.get("severity_scope", {}).get("by_severity", {}).get("buckets", [])
            counts = {b["key"]: b["doc_count"] for b in sev_buckets}
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

            host_buckets = aggregations.get("hosts", {}).get("by_hostname", {}).get("buckets", [])
            hostnames = sorted(b["key"] for b in host_buckets if b["key"])
    except Exception as exc:
        error = str(exc)

    all_total = sum(s["count"] for s in severity_counts)

    return render_template(
        "dashboard.html",
        events=events,
        total=total,
        all_total=all_total,
        severity_counts=severity_counts,
        hostnames=hostnames,
        active_severity=active_severity,
        active_cluster=active_cluster,
        active_hostname=active_hostname,
        page=page,
        total_pages=total_pages,
        page_size=page_size,
        page_size_options=PAGE_SIZE_OPTIONS,
        error=error,
    )


@bp.route("/top")
def top_important():
    client = current_app.extensions["opensearch"]
    rules = current_app.extensions["rules"]
    error = None
    clusters = []
    try:
        clusters = get_top_clusters(client, current_app.config)
        for cluster in clusters:
            cluster["severity_label"] = rules.label_for_weight(cluster.get("severity_weight"))
    except Exception as exc:
        error = str(exc)

    return render_template("top_important.html", clusters=clusters, error=error)
