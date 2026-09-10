from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from .ranking import get_cluster_categories, get_cluster_hostnames, get_top_clusters

bp = Blueprint("dashboard", __name__)

SEVERITY_ORDER = ["error", "warning", "notice", "info", "unknown"]
PAGE_SIZE_OPTIONS = [25, 50, 100]

# "all" still caps here rather than being truly unbounded -- this matches
# OpenSearch's own default index.max_result_window, so it can't 500 on a
# large index.
ALL_SIZE_CAP = 10000

# Cap on distinct values shown in a chip-list facet (host/unit/source_type)
# -- generous for a homelab, but keeps the agg bounded.
FACET_SIZE = 50

RANGE_PRESETS = {"1h": 1, "24h": 24, "7d": 24 * 7}
DEFAULT_RANGE = "24h"


def _parse_local_dt(value):
    """Parses a <input type=datetime-local> value ("YYYY-MM-DDTHH:MM").
    Treated as UTC, same as every other timestamp in this app -- there's no
    per-user timezone concept anywhere else, so introducing one just for
    this form would be inconsistent with how every other timestamp in the
    app is displayed (raw UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@bp.route("/")
def overview():
    client = current_app.extensions["opensearch"]
    rules = current_app.extensions["rules"]
    active_hostname = request.args.get("hostname") or None
    active_category = request.args.get("category") or None
    range_key = request.args.get("range") or DEFAULT_RANGE
    custom_since_input = request.args.get("since", "")
    custom_until_input = request.args.get("until", "")

    since = until = None
    if range_key == "custom":
        since = _parse_local_dt(custom_since_input)
        until = _parse_local_dt(custom_until_input)
        if since is None:
            # No usable lower bound to query with -- fall back to the
            # default preset rather than erroring on a half-filled form.
            range_key = DEFAULT_RANGE
    if range_key != "custom":
        since = datetime.now(timezone.utc) - timedelta(hours=RANGE_PRESETS.get(range_key, RANGE_PRESETS[DEFAULT_RANGE]))
        until = None

    error = None
    clusters = []
    hostnames = []
    categories = []
    try:
        clusters = get_top_clusters(
            client,
            current_app.config,
            hostname=active_hostname,
            category=active_category,
            since=since,
            until=until,
        )
        for cluster in clusters:
            cluster["severity_label"] = rules.label_for_weight(cluster.get("severity_weight"))
        hostnames = get_cluster_hostnames(client, current_app.config)
        categories = get_cluster_categories(client, current_app.config)
    except Exception as exc:
        error = str(exc)

    return render_template(
        "overview.html",
        clusters=clusters,
        hostnames=hostnames,
        categories=categories,
        active_hostname=active_hostname,
        active_category=active_category,
        active_view="overview",
        range_key=range_key,
        range_presets=list(RANGE_PRESETS.keys()),
        custom_since=custom_since_input,
        custom_until=custom_until_input,
        error=error,
    )


@bp.route("/top")
def top_redirect():
    return redirect(url_for("dashboard.overview", **request.args))


@bp.route("/explore")
def explore():
    client = current_app.extensions["opensearch"]
    prefix = current_app.config["INDEX_PREFIX"]
    active_severity = request.args.get("severity") or None
    active_cluster = request.args.get("cluster") or None
    active_hostname = request.args.get("hostname") or None
    active_unit = request.args.get("unit") or None
    active_source_type = request.args.get("source_type") or None
    active_query = request.args.get("q", "").strip() or None

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
    units = []
    source_types = []
    error = None
    try:
        severity_filter = {"term": {"severity": active_severity}} if active_severity else None
        hostname_filter = {"term": {"hostname": active_hostname}} if active_hostname else None
        unit_filter = {"term": {"unit": active_unit}} if active_unit else None
        cluster_filter = {"term": {"cluster_key": active_cluster}} if active_cluster else None
        if active_source_type:
            source_type_filter = {"term": {"source_type": active_source_type}}
        else:
            # Heartbeat docs are pipeline plumbing (see Pipeline Health),
            # not something you want cluttering a raw browse -- hidden
            # unless explicitly filtered for.
            source_type_filter = {"bool": {"must_not": [{"term": {"source_type": "heartbeat"}}]}}
        text_clause = (
            {"multi_match": {"query": active_query, "fields": ["title^2", "message"]}} if active_query else None
        )

        main_filters = [
            f for f in [severity_filter, hostname_filter, unit_filter, cluster_filter, source_type_filter] if f
        ]
        bool_body = {}
        if main_filters:
            bool_body["filter"] = main_filters
        if text_clause:
            bool_body["must"] = [text_clause]
        query_clause = {"bool": bool_body} if bool_body else {"match_all": {}}

        body = {
            "size": size,
            "from": (page - 1) * size,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": query_clause,
        }
        # Skip the facet breakdown while drilling into one cluster -- a
        # cluster is already tied to one hostname, so none of this is
        # useful there.
        if not active_cluster:
            # Severity counts respect every OTHER active filter/search
            # (host, unit, source_type, free text) but deliberately exclude
            # severity itself -- picking one severity shouldn't zero out
            # the others, it should show what else is available to switch
            # to. Host/unit/source_type stay plain global chip lists (like
            # the host filter already was) rather than getting the same
            # scoped-count treatment -- simpler, and consistent with how
            # host filtering already shipped.
            severity_scope_filters = [f for f in [hostname_filter, unit_filter, source_type_filter] if f]
            severity_scope_body = {}
            if severity_scope_filters:
                severity_scope_body["filter"] = severity_scope_filters
            if text_clause:
                severity_scope_body["must"] = [text_clause]
            severity_scope = {"bool": severity_scope_body} if severity_scope_body else {"match_all": {}}

            body["aggs"] = {
                "hosts": {
                    "global": {},
                    "aggs": {"by_hostname": {"terms": {"field": "hostname", "size": FACET_SIZE}}},
                },
                "units": {
                    "global": {},
                    "aggs": {"by_unit": {"terms": {"field": "unit", "size": FACET_SIZE}}},
                },
                "source_types": {
                    "global": {},
                    "aggs": {"by_source_type": {"terms": {"field": "source_type", "size": FACET_SIZE}}},
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

            hostnames = sorted(
                b["key"] for b in aggregations.get("hosts", {}).get("by_hostname", {}).get("buckets", []) if b["key"]
            )
            units = sorted(
                b["key"] for b in aggregations.get("units", {}).get("by_unit", {}).get("buckets", []) if b["key"]
            )
            source_types = sorted(
                b["key"]
                for b in aggregations.get("source_types", {}).get("by_source_type", {}).get("buckets", [])
                if b["key"]
            )
    except Exception as exc:
        error = str(exc)

    all_total = sum(s["count"] for s in severity_counts)

    return render_template(
        "explore.html",
        events=events,
        total=total,
        all_total=all_total,
        severity_counts=severity_counts,
        hostnames=hostnames,
        units=units,
        source_types=source_types,
        active_severity=active_severity,
        active_cluster=active_cluster,
        active_hostname=active_hostname,
        active_unit=active_unit,
        active_source_type=active_source_type,
        active_query=active_query or "",
        active_view="explore",
        page=page,
        total_pages=total_pages,
        page_size=page_size,
        page_size_options=PAGE_SIZE_OPTIONS,
        error=error,
    )
