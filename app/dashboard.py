from flask import Blueprint, current_app, render_template

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    client = current_app.extensions["opensearch"]
    prefix = current_app.config["INDEX_PREFIX"]
    size = current_app.config["DASHBOARD_PAGE_SIZE"]

    events = []
    total = 0
    error = None
    try:
        resp = client.search(
            index=f"{prefix}-*",
            body={"size": size, "sort": [{"timestamp": {"order": "desc"}}]},
        )
        events = [hit["_source"] for hit in resp["hits"]["hits"]]
        total = resp["hits"]["total"]["value"]
    except Exception as exc:
        error = str(exc)

    return render_template("dashboard.html", events=events, total=total, error=error)
