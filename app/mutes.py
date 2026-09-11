"""Phase 5 noise suppression: mark a (rule_id, hostname) pair -- or a
rule_id for every host -- as muted once you've confirmed a recurring
match is benign, so it stops cluttering Overview instead of accumulating
forever. rule_id here is the same namespace app/dedup.py and
app/anomaly.py both write cluster docs with, including anomaly.py's
synthetic "anomaly:<unit>" ids -- so one mute mechanism covers both
detection methods with no special-casing.

Muting only affects clustering/ranking (app/dedup.py, app/anomaly.py
check is_muted() before upserting a cluster). Raw docs keep whatever
rule_id/category they were tagged with at write time either way --
Explore's full-history search is deliberately unaffected.
"""
from datetime import datetime, timezone

from flask import Blueprint, current_app, redirect, render_template, request, url_for

bp = Blueprint("mutes", __name__)

MUTE_LIST_SIZE = 500


def _mute_id(rule_id, hostname):
    return f"{rule_id}|{hostname or '*'}"


def get_active_mutes(client, config):
    """All mutes, fetched once per dedup/anomaly run rather than
    per-bucket. Returns a list of {rule_id, hostname, reason, created_at}
    dicts (hostname is None for an all-hosts mute)."""
    resp = client.search(
        index=config["MUTES_INDEX"],
        body={"size": MUTE_LIST_SIZE, "sort": [{"created_at": {"order": "desc"}}]},
        ignore_unavailable=True,
    )
    mutes = []
    for hit in resp.get("hits", {}).get("hits", []):
        doc = hit["_source"]
        mutes.append({"mute_id": hit["_id"], **doc})
    return mutes


def is_muted(mutes, rule_id, hostname):
    for mute in mutes:
        if mute["rule_id"] != rule_id:
            continue
        if mute["hostname"] is None or mute["hostname"] == hostname:
            return True
    return False


def add_mute(client, config, rule_id, hostname, reason):
    hostname = hostname or None
    client.index(
        index=config["MUTES_INDEX"],
        id=_mute_id(rule_id, hostname),
        body={
            "rule_id": rule_id,
            "hostname": hostname,
            "reason": reason or "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def remove_mute(client, config, mute_id):
    client.delete(index=config["MUTES_INDEX"], id=mute_id, ignore=[404])


@bp.route("/mutes")
def index():
    client = current_app.extensions["opensearch"]
    rules = current_app.extensions["rules"]
    error = None
    mutes = []
    try:
        mutes = get_active_mutes(client, current_app.config)
    except Exception as exc:
        error = str(exc)

    return render_template(
        "mutes.html",
        mutes=mutes,
        rule_ids=[rule.id for rule in rules.rules],
        active_view="mutes",
        error=error,
    )


@bp.route("/mutes", methods=["POST"])
def create():
    client = current_app.extensions["opensearch"]
    rule_id = (request.form.get("rule_id") or "").strip()
    hostname = (request.form.get("hostname") or "").strip() or None
    reason = (request.form.get("reason") or "").strip()
    if rule_id:
        add_mute(client, current_app.config, rule_id, hostname, reason)
    return redirect(url_for("mutes.index"))


@bp.route("/mutes/<mute_id>/delete", methods=["POST"])
def delete(mute_id):
    client = current_app.extensions["opensearch"]
    remove_mute(client, current_app.config, mute_id)
    redirect_to = request.form.get("next") or url_for("mutes.index")
    return redirect(redirect_to)
