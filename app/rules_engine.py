"""Loads rules.yaml once and matches documents against it.

Used by the Flask ingestion API (app/ingest.py) for the notification path.
The Fluent Bit / raw-journal path is matched by an OpenSearch ingest
pipeline generated from the same rules.yaml -- see
opensearch/generate_rules_pipeline.py -- so a rule only has to be written
once and both paths stay in sync.
"""
import re

import yaml


class Rule:
    __slots__ = ("id", "description", "category", "severity", "weight", "conditions")

    def __init__(self, id, description, category, severity, weight, conditions):
        self.id = id
        self.description = description
        self.category = category
        self.severity = severity
        self.weight = weight
        self.conditions = conditions  # list of (path, op, value_or_compiled_pattern)


def _compile_conditions(match):
    conditions = []
    for cond in match:
        path = cond["field"].split(".")
        if "regex" in cond:
            conditions.append((path, "regex", re.compile(cond["regex"])))
        elif "equals" in cond:
            conditions.append((path, "equals", cond["equals"]))
        elif "not_equals" in cond:
            conditions.append((path, "not_equals", cond["not_equals"]))
        else:
            raise ValueError(
                f"match condition on field {cond['field']!r} needs one of: regex, equals, not_equals"
            )
    return conditions


def load_rules(path):
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    severities = raw.get("severities", {})
    rules = []
    for entry in raw.get("rules", []):
        weight = severities.get(entry["severity"])
        if weight is None:
            raise ValueError(f"rule {entry['id']!r} uses unknown severity {entry['severity']!r}")
        rules.append(
            Rule(
                id=entry["id"],
                description=entry.get("description", ""),
                category=entry.get("category", "Uncategorized"),
                severity=entry["severity"],
                weight=weight,
                conditions=_compile_conditions(entry["match"]),
            )
        )
    return rules, severities


def _resolve(doc, path):
    cur = doc
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _condition_matches(doc, path, op, value):
    actual = _resolve(doc, path)
    if op == "regex":
        return actual is not None and value.search(str(actual)) is not None
    if actual is None:
        return False
    if op == "equals":
        return str(actual) == value
    if op == "not_equals":
        return str(actual) != value
    return False


class RuleEngine:
    """Holds the compiled rule set plus the severity-name -> weight map, so
    the reverse lookup (weight -> label, for dashboard badges) has one
    place to live instead of a second hardcoded copy of the weight scale."""

    def __init__(self, path):
        self.path = path
        self.reload()

    def reload(self):
        self.rules, self.severities = load_rules(self.path)
        self._weight_to_label = {weight: name for name, weight in self.severities.items()}

    def match(self, doc):
        for rule in self.rules:
            if all(_condition_matches(doc, path, op, value) for path, op, value in rule.conditions):
                return rule
        return None

    def label_for_weight(self, weight):
        return self._weight_to_label.get(weight, "unknown")
