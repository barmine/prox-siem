#!/usr/bin/env bash
# One-time (idempotent-ish) bootstrap: waits for OpenSearch, then creates
# the ISM policy and index template used by proxmox-logs-* indices.
#
# Re-running is safe for the index template (PUT always overwrites it).
# Re-running the ISM policy PUT will print a version_conflict error if the
# policy already exists unchanged -- that's expected and harmless.
set -euo pipefail

OPENSEARCH_URL="${OPENSEARCH_URL:-http://localhost:9200}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Waiting for OpenSearch at ${OPENSEARCH_URL} ..."
until curl -s -o /dev/null -w '%{http_code}' "${OPENSEARCH_URL}" | grep -q '^200$'; do
  sleep 2
done

echo "Creating ISM policy: proxmox-logs-policy"
curl -s -X PUT "${OPENSEARCH_URL}/_plugins/_ism/policies/proxmox-logs-policy" \
  -H 'Content-Type: application/json' \
  -d @"${DIR}/ism_policy.json"
echo

echo "Creating index template: proxmox-logs-template"
curl -s -X PUT "${OPENSEARCH_URL}/_index_template/proxmox-logs-template" \
  -H 'Content-Type: application/json' \
  -d @"${DIR}/index_template.json"
echo

echo "Done."
