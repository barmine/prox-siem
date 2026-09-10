#!/usr/bin/env python3
"""Builds the proxmox-logs-rules OpenSearch ingest pipeline from rules.yaml
and PUTs it to OpenSearch. Run by opensearch/setup.sh; safe to re-run any
time rules.yaml changes -- PUT always overwrites the pipeline definition.

Why an ingest pipeline for this path specifically: Fluent Bit's raw-log
path writes straight to OpenSearch (see
fluentbit/conf.d/output-opensearch.conf's `Pipeline` setting), never
passing through Flask, so it can't reuse app/rules_engine.py the way the
notification-webhook path does. This generates the equivalent match logic
as Painless instead, from the SAME rules.yaml, so a rule is still only
ever written once.

Why the Painless source is fully unrolled (one literal if-block per rule,
generated in Python) rather than one generic script that loops over
`params.rules`: Painless's dynamic-regex options are deliberately narrow.
`Pattern.compile(String)` isn't in its whitelist at all (tried first --
fails with "static method ... not found"), and the `=~`/`==~` operators
that ARE allowed only take a *literal* `/pattern/` known at compile time,
not a runtime string from params. So each rule's pattern gets baked into
the generated source as a real Painless regex literal instead -- this
also requires the `script.painless.regex.enabled` node setting (static;
set in docker-compose.yml, needs a container recreate to take effect, not
just a cluster-settings PUT).
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

PIPELINE_ID = "proxmox-logs-rules"


def _escape_single_quoted(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _escape_regex_literal(pattern):
    # Painless regex literals are delimited by /.../ -- escape a literal
    # slash so it doesn't end the literal early. Everything else (\d, \b,
    # (?i), alternation, ...) passes through unchanged, same as any regex.
    return pattern.replace("/", "\\/")


def _resolve_statements(path, var_prefix):
    """Painless statements that resolve a dotted field path (e.g.
    ["fields", "type"]) off `ctx` into a nullable String named
    `<var_prefix>_s`, without ever throwing on a missing key."""
    stmts = []
    cur = "ctx"
    for i, seg in enumerate(path):
        seg_literal = _escape_single_quoted(seg)
        nxt = f"{var_prefix}_{i}"
        stmts.append(
            f"def {nxt} = ({cur} instanceof Map && {cur}.containsKey('{seg_literal}')) "
            f"? {cur}.get('{seg_literal}') : null;"
        )
        cur = nxt
    sval = f"{var_prefix}_s"
    stmts.append(f"String {sval} = ({cur} == null) ? null : {cur}.toString();")
    return stmts, sval


def _condition_check(cond, sval):
    op, value = cond["op"], cond["value"]
    if op == "equals":
        return f"({sval} != null && {sval}.equals('{_escape_single_quoted(value)}'))"
    if op == "not_equals":
        return f"({sval} != null && !{sval}.equals('{_escape_single_quoted(value)}'))"
    if op == "regex":
        return f"({sval} != null && {sval} =~ /{_escape_regex_literal(value)}/)"
    raise ValueError(f"unknown condition op {op!r}")


def build_pipeline_source(rules):
    lines = ["boolean matched = false;"]
    counter = 0
    for rule in rules:
        lines.append("if (!matched) {")
        lines.append("  boolean allMatch = true;")
        for cond in rule["conditions"]:
            counter += 1
            var_prefix = f"v{counter}"
            resolve_stmts, sval = _resolve_statements(cond["path"], var_prefix)
            lines.append("  if (allMatch) {")
            lines.extend(f"    {stmt}" for stmt in resolve_stmts)
            lines.append(f"    if (!{_condition_check(cond, sval)}) {{ allMatch = false; }}")
            lines.append("  }")
        lines.append("  if (allMatch) {")
        lines.append(f"    ctx['rule_id'] = '{_escape_single_quoted(rule['id'])}';")
        lines.append(f"    ctx['category'] = '{_escape_single_quoted(rule['category'])}';")
        lines.append(f"    ctx['severity_weight'] = {int(rule['weight'])};")
        lines.append("    matched = true;")
        lines.append("  }")
        lines.append("}")
    return "\n".join(lines)


def build_pipeline_body(rules_path):
    with open(rules_path) as f:
        raw = yaml.safe_load(f)

    severities = raw.get("severities", {})
    rules = []
    for entry in raw["rules"]:
        weight = severities[entry["severity"]]
        conditions = []
        for cond in entry["match"]:
            path = cond["field"].split(".")
            if "regex" in cond:
                conditions.append({"path": path, "op": "regex", "value": cond["regex"]})
            elif "equals" in cond:
                conditions.append({"path": path, "op": "equals", "value": cond["equals"]})
            elif "not_equals" in cond:
                conditions.append({"path": path, "op": "not_equals", "value": cond["not_equals"]})
        rules.append(
            {"id": entry["id"], "category": entry["category"], "weight": weight, "conditions": conditions}
        )

    return {
        "description": "Tags proxmox-logs-* docs with rule_id/category/severity_weight from rules.yaml (Phase 3).",
        "processors": [{"script": {"lang": "painless", "source": build_pipeline_source(rules)}}],
    }


def main():
    repo_root = Path(__file__).resolve().parent.parent
    rules_path = os.environ.get("RULES_PATH", str(repo_root / "rules.yaml"))
    opensearch_url = os.environ.get("OPENSEARCH_URL", "http://localhost:9200").rstrip("/")

    body = build_pipeline_body(rules_path)
    url = f"{opensearch_url}/_ingest/pipeline/{PIPELINE_ID}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="PUT", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
