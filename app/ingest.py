from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("ingest", __name__)


def _normalize_timestamp(value):
    """Proxmox sends {{ timestamp }} as UNIX epoch seconds; accept that or
    an already-ISO8601 string so the field always lands as a valid ES/OS date."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return value


@bp.route("/api/events", methods=["POST"])
def create_event():
    secret = request.headers.get("X-Prox-Siem-Secret")
    if not secret or secret != current_app.config["SHARED_SECRET"]:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected a JSON object body"}), 400

    if "timestamp" not in payload or "severity" not in payload:
        return jsonify({"error": "timestamp and severity are required"}), 400

    fields = payload.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}

    doc = {
        "timestamp": _normalize_timestamp(payload["timestamp"]),
        "severity": payload["severity"],
        "title": payload.get("title", ""),
        "message": payload.get("message", ""),
        "source_type": fields.get("type") or "unknown",
        "hostname": fields.get("hostname") or "unknown",
        "fields": fields,
    }

    # Phase 3: tag at write time -- the raw Fluent Bit path gets the same
    # rules.yaml applied via an OpenSearch ingest pipeline instead (see
    # opensearch/generate_rules_pipeline.py), since it never passes through
    # this API.
    rule = current_app.extensions["rules"].match(doc)
    if rule is not None:
        doc["rule_id"] = rule.id
        doc["category"] = rule.category
        doc["severity_weight"] = rule.weight

    index_name = f"{current_app.config['INDEX_PREFIX']}-{datetime.now(timezone.utc):%Y.%m.%d}"
    client = current_app.extensions["opensearch"]

    try:
        resp = client.index(index=index_name, body=doc)
    except Exception as exc:  # surface OpenSearch errors while wiring this up
        return jsonify({"error": "failed to index event", "detail": str(exc)}), 502

    return jsonify({"result": "created", "_id": resp.get("_id"), "index": index_name}), 201
