"""
cogni.agent.predicates
======================

Tiny evaluator for v1 rule predicates.

A predicate is a tree of `{op, ...}` nodes. `when` and `unless` are
arrays of predicate nodes (implicitly AND-ed).

Lookup order for `key`:
  1. WorldModel.facts[key]   (perceiver-derived; most common)
  2. Reality.measurements[key] (oracle-derived; only after grading)
  3. scenario_target[key]    (constraints declared in the scenario yaml)

Missing keys evaluate to False for the leaf node — predicates must
positively match.

Tags (`{op:"tag", name:X}`) are looked up in WorldModel.tags.

Boolean ops: all, any, not.

Compare ops: eq, ne, lt, lte, gt, gte, in, matches, exists.

The evaluator is intentionally tiny (~70 lines): no caching, no
short-circuit performance hacks. It runs once per rule per question.
If hot-path matters later, memoize at the call site.
"""
from __future__ import annotations

import re
from typing import Any


def _facts_dict(world) -> dict[str, Any]:
    """Project WorldModel.facts into a {key: value} dict for predicate
    lookups. WorldModel.facts is a dict[str, Fact]; each Fact carries
    .value as the payload. Defensive against missing world or legacy
    list-shaped facts."""
    if world is None:
        return {}
    raw = getattr(world, "facts", None)
    if not raw:
        return {}
    out = {}
    if isinstance(raw, dict):
        for k, f in raw.items():
            val = getattr(f, "value", None)
            if val is None:
                val = getattr(f, "data", f)
            out[k] = val
        return out
    # legacy list-of-Fact shape
    for f in raw:
        val = getattr(f, "value", None)
        if val is None:
            val = getattr(f, "data", None)
        out[getattr(f, "key", "")] = val
    return out


def _tags_set(world) -> set[str]:
    if world is None:
        return set()
    return set(getattr(world, "tags", set()) or set())


def _measurements_dict(reality) -> dict[str, Any]:
    if reality is None:
        return {}
    return dict(getattr(reality, "measurements", {}) or {})


def _lookup(key: str, facts: dict, measurements: dict, target: dict):
    """Three-tier lookup. Returns (found: bool, value)."""
    if key in facts:
        return True, facts[key]
    if key in measurements:
        return True, measurements[key]
    if key in target:
        return True, target[key]
    return False, None


def evaluate(node: dict,
             world,
             reality=None,
             scenario_target: dict | None = None) -> bool:
    """Evaluate a single predicate node against the WorldModel,
    optional Reality, and optional scenario target dict."""
    if not isinstance(node, dict) or "op" not in node:
        return False

    facts = _facts_dict(world)
    tags = _tags_set(world)
    measurements = _measurements_dict(reality)
    target = scenario_target or {}

    op = node["op"]

    # ---- boolean combinators ----
    if op == "all":
        return all(evaluate(p, world, reality, target) for p in node.get("preds", []))
    if op == "any":
        return any(evaluate(p, world, reality, target) for p in node.get("preds", []))
    if op == "not":
        return not evaluate(node.get("pred", {}), world, reality, target)

    # ---- tag lookup ----
    if op == "tag":
        return node.get("name", "") in tags

    # ---- comparison / membership / regex ----
    key = node.get("key", "")

    if op == "exists":
        found, _ = _lookup(key, facts, measurements, target)
        return found

    found, val = _lookup(key, facts, measurements, target)
    if not found:
        return False  # absence ≠ consent

    if op == "eq":
        return val == node.get("value")
    if op == "ne":
        return val != node.get("value")
    if op in ("lt", "lte", "gt", "gte"):
        target_v = node.get("value")
        try:
            if op == "lt":  return val <  target_v
            if op == "lte": return val <= target_v
            if op == "gt":  return val >  target_v
            if op == "gte": return val >= target_v
        except TypeError:
            return False  # non-numeric comparison → not satisfied
    if op == "in":
        try:
            return val in node.get("values", [])
        except TypeError:
            return False
    if op == "matches":
        if not isinstance(val, str):
            return False
        try:
            return re.search(node.get("pattern", ""), val) is not None
        except re.error:
            return False

    return False  # unknown op


def evaluate_when_unless(when: list,
                          unless: list,
                          world,
                          reality=None,
                          scenario_target: dict | None = None) -> bool:
    """All `when` must be true, all `unless` must be false."""
    if when:
        if not all(evaluate(p, world, reality, scenario_target) for p in when):
            return False
    if unless:
        if any(evaluate(p, world, reality, scenario_target) for p in unless):
            return False
    return True
