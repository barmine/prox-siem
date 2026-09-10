#!/usr/bin/env bash
# One-time (idempotent-ish) bootstrap: waits for OpenSearch, then creates
# the ISM policy and index template used by proxmox-logs-* indices.
#
# Re-running is safe for the index template (PUT always overwrites it).
# Re-running the ISM policy PUT will print a version_conflict error if the
# policy already exists unchanged -- that's expected and harmless.
set -euo pipefail

OPENSEARCH_URL="${OPENSEARCH_URL:-http://localhost:9200}"
# Phase 4: hot-tier retention is a config value now, not hardcoded in the
# policy JSON -- see opensearch/ism_policy.json.template.
ISM_HOT_RETENTION_DAYS="${ISM_HOT_RETENTION_DAYS:-30}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Waiting for OpenSearch at ${OPENSEARCH_URL} ..."
until curl -s -o /dev/null -w '%{http_code}' "${OPENSEARCH_URL}" | grep -q '^200$'; do
  sleep 2
done

echo "Creating ISM policy: proxmox-logs-policy (hot retention: ${ISM_HOT_RETENTION_DAYS}d)"
# NOTE: changing ISM_HOT_RETENTION_DAYS and re-running this against an
# EXISTING policy will fail with a version_conflict -- OpenSearch ISM
# policies use optimistic concurrency (seq_no/primary_term) and a plain
# PUT can't blindly overwrite a policy that already differs. To change
# retention later, delete the policy first:
#   curl -X DELETE "${OPENSEARCH_URL}/_plugins/_ism/policies/proxmox-logs-policy"
# then re-run this script.
sed "s/\${HOT_RETENTION_DAYS}/${ISM_HOT_RETENTION_DAYS}/g" "${DIR}/ism_policy.json.template" \
  | curl -s -X PUT "${OPENSEARCH_URL}/_plugins/_ism/policies/proxmox-logs-policy" \
      -H 'Content-Type: application/json' \
      -d @-
echo

echo "Creating index template: proxmox-logs-template"
curl -s -X PUT "${OPENSEARCH_URL}/_index_template/proxmox-logs-template" \
  -H 'Content-Type: application/json' \
  -d @"${DIR}/index_template.json"
echo

# Phase 3: clusters index (burst-dedup output) + the ingest pipeline that
# tags Fluent Bit's raw-log path from rules.yaml. PUT on an existing index
# errors with resource_already_exists_exception -- expected/harmless on
# re-run, same as the ISM policy above.
echo "Creating index: proxmox-clusters"
curl -s -X PUT "${OPENSEARCH_URL}/proxmox-clusters" \
  -H 'Content-Type: application/json' \
  -d @"${DIR}/clusters_index.json"
echo

echo "Generating ingest pipeline: proxmox-logs-rules (from rules.yaml)"
OPENSEARCH_URL="${OPENSEARCH_URL}" python3 "${DIR}/generate_rules_pipeline.py"
echo

echo "Done."
