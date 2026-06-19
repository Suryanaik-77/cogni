"""
cogni.agent.kb
==============
KnowledgeBase: collection of Rules with persistence and lifecycle ops.
"""
from __future__ import annotations
import os
import json
from dataclasses import asdict
from typing import Iterable
from .core import Rule, RuleStrength, RuleStatus, RulePerformance, KBEdit, KBEditKind, _now


_STRENGTH_ORDER = [RuleStrength.HEURISTIC, RuleStrength.SITUATIONAL,
                   RuleStrength.TENDENCY, RuleStrength.STRONG, RuleStrength.LAW]


class KnowledgeBase:
    def __init__(self, rules: list[Rule] = None, pack_path: str = "",
                 envelope: dict = None):
        self.envelope = envelope or {}
        self.rules: dict[str, Rule] = {r.id: r for r in (rules or [])}
        self.pack_path = pack_path  # for save_back

    # ---- recall ----
    def recall(self, world, stage: str | None = None) -> list[Rule]:
        return [r for r in self.rules.values() if r.applies_to(world, stage=stage)]

    def by_id(self, rid: str) -> Rule | None:
        return self.rules.get(rid)

    # ---- lifecycle ops ----
    def apply(self, edit: KBEdit) -> bool:
        if edit.kind == KBEditKind.ADD:
            if edit.new_rule and edit.new_rule.id not in self.rules:
                self.rules[edit.new_rule.id] = edit.new_rule
                edit.applied = True
                return True
            return False
        if edit.target_rule_id is None or edit.target_rule_id not in self.rules:
            return False
        r = self.rules[edit.target_rule_id]
        if edit.kind == KBEditKind.STRENGTHEN and edit.new_strength:
            r.strength = edit.new_strength
        elif edit.kind == KBEditKind.WEAKEN and edit.new_strength:
            r.strength = edit.new_strength
        elif edit.kind == KBEditKind.SCOPE:
            # v0 rules: unless is a list of tag-name strings (hashable, sortable).
            # v1 rules: unless is a list of predicate dicts (unhashable). For v1
            # we deduplicate by JSON serialization and append in stable order.
            existing = list(r.unless)
            added = list(edit.added_unless or [])
            if r.schema_version >= 1:
                seen = {json.dumps(x, sort_keys=True, default=str): x
                        for x in existing}
                for a in added:
                    a_dict = a if isinstance(a, dict) else {"op": "tag", "name": str(a)}
                    key = json.dumps(a_dict, sort_keys=True, default=str)
                    if key not in seen:
                        seen[key] = a_dict
                r.unless = list(seen.values())
            else:
                r.unless = sorted(set(existing) | set(added))
        elif edit.kind == KBEditKind.RETIRE:
            r.status = RuleStatus.RETIRED
        elif edit.kind == KBEditKind.REWRITE and edit.new_rule:
            edit.new_rule.id = r.id
            edit.new_rule.parent_rule_id = r.parent_rule_id or r.id
            edit.new_rule.performance = r.performance
            edit.new_rule.created_at = r.created_at
            self.rules[r.id] = edit.new_rule
        else:
            return False
        r.updated_at = _now()
        edit.applied = True
        return True

    # ---- accounting ----
    # For v1 rules, history events are the source of truth and counters
    # are derived. We still maintain perf counters live so the rest of
    # the system (which queries r.performance directly) keeps working
    # without a second pass over history.
    def record_citation(self, rule_id: str, qid: str = "", session_dir: str = ""):
        r = self.rules.get(rule_id)
        if not r:
            return
        r.performance.times_cited += 1
        r.performance.last_cited_at = _now()
        if r.schema_version >= 1:
            r.history.append({
                "at": _now(),
                "event": "cited",
                "qid": qid,
                "session_dir": session_dir,
                # verdict filled in later by record_outcome's history append
            })

    def record_outcome(self, rule_id: str, status: str, prediction_id: str = "",
                       qid: str = "", session_dir: str = "",
                       evidence_key: str = "", evidence_value=None):
        """status: 'right' | 'wrong' | 'unfalsifiable'"""
        r = self.rules.get(rule_id)
        if not r:
            return
        if status == "right":
            r.performance.times_right += 1
        elif status == "wrong":
            r.performance.times_wrong += 1
            r.performance.last_failed_at = _now()
            if prediction_id:
                r.performance.failures.append(prediction_id)
        elif status == "unfalsifiable":
            r.performance.times_unfalsifiable += 1
        if r.schema_version >= 1:
            r.history.append({
                "at": _now(),
                "event": "outcome",
                "qid": qid,
                "session_dir": session_dir,
                "verdict": status,
                "prediction_id": prediction_id,
                "evidence_key": evidence_key,
                "evidence_value": evidence_value,
            })

    # ---- persistence ----
    def save(self, path: str = ""):
        path = path or self.pack_path
        if not path:
            raise ValueError("no path to save to")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # If we loaded a v1 pack, preserve the envelope and write rules
        # back in v1 shape. Otherwise emit a minimal v0 dump.
        if self.envelope.get("schema") == "kb-rule/v1":
            payload = dict(self.envelope)
            payload["rules"] = [_dump_v1_rule(r) for r in self.rules.values()]
        else:
            payload = {"rules": [r.to_dict() for r in self.rules.values()]}
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)

    @classmethod
    def load(cls, path: str) -> "KnowledgeBase":
        """Load a rule pack. Supports both legacy v0 and current v1
        (kb-rule/v1) formats. v1 detection is by `schema` field at the
        pack level OR `version: 1` at the rule level."""
        with open(path) as f:
            data = json.load(f)

        is_v1 = data.get("schema") == "kb-rule/v1"
        rules = []
        for d in data.get("rules", []):
            if is_v1 or d.get("version") == 1:
                rules.append(_load_v1_rule(d))
            else:
                rules.append(_load_v0_rule(d))
        # Capture the pack envelope (everything except `rules`) so save()
        # round-trips correctly. v0 packs end up with an empty envelope.
        envelope = {k: v for k, v in data.items() if k != "rules"}
        kb = cls(rules=rules, pack_path=path)
        kb.envelope = envelope
        return kb


# ---------------------------------------------------------------------------
# v0 / v1 loaders (module-private)
# ---------------------------------------------------------------------------

def _load_v0_rule(d: dict) -> Rule:
    """Loader for legacy (pre-Apr 2026) packs. Citations as strings,
    when/unless as tag lists, no `predicts`, mutable `performance` dict."""
    perf_d = d.get("performance", {})
    perf = RulePerformance(**perf_d) if perf_d else RulePerformance()
    return Rule(
        id=d["id"],
        statement=d["statement"],
        when=d.get("when", []),
        unless=d.get("unless", []),
        stage=d.get("stage"),
        strength=RuleStrength(d.get("strength", "tendency")),
        status=RuleStatus(d.get("status", "active")),
        citations=d.get("citations", []),
        rationale=d.get("rationale", ""),
        performance=perf,
        created_at=d.get("created_at", _now()),
        updated_at=d.get("updated_at", _now()),
        parent_rule_id=d.get("parent_rule_id"),
        schema_version=0,
    )


# v1 strength vocabulary -> internal RuleStrength enum.
# v1 uses high|medium|low; the internal enum has finer-grained values.
# Mapping is intentionally lossy: v1 high -> STRONG, medium -> TENDENCY,
# low -> HEURISTIC.
_V1_STRENGTH_MAP = {
    "high":   RuleStrength.STRONG,
    "medium": RuleStrength.TENDENCY,
    "low":    RuleStrength.HEURISTIC,
}


def _v1_strength_back(internal: RuleStrength) -> str:
    for k, v in _V1_STRENGTH_MAP.items():
        if v == internal:
            return k
    return "medium"


def _load_v1_rule(d: dict) -> Rule:
    """Loader for kb-rule/v1 packs. The v1-only fields land on
    Rule.applies_to_v1 / predicts / prevents / examples / history; v0
    fields like `when`/`unless` are reused directly (predicate trees
    instead of tag strings).
    """
    # Map v1 strength -> internal enum.
    strength = _V1_STRENGTH_MAP.get(d.get("strength", "medium"),
                                      RuleStrength.TENDENCY)

    # Derive RulePerformance counters from history. The reflect organ
    # writes append-only events; counters are recomputed at load time.
    perf = RulePerformance()
    last_failed = None
    last_cited = None
    failures = []
    for ev in d.get("history", []):
        e = ev.get("event")
        if e == "cited":
            perf.times_cited += 1
            last_cited = ev.get("at") or last_cited
            # Inline-style v0 events (cited+verdict in one record)
            v = ev.get("verdict", "")
            if v.startswith("right"):
                perf.times_right += 1
            elif v.startswith("wrong"):
                perf.times_wrong += 1
                last_failed = ev.get("at") or last_failed
                qid = ev.get("qid")
                if qid:
                    failures.append(qid)
            elif v == "unfalsifiable":
                perf.times_unfalsifiable += 1
        elif e == "outcome":
            v = ev.get("verdict", "")
            if v == "right":
                perf.times_right += 1
            elif v == "wrong":
                perf.times_wrong += 1
                last_failed = ev.get("at") or last_failed
                pid = ev.get("prediction_id") or ev.get("qid")
                if pid:
                    failures.append(pid)
            elif v == "unfalsifiable":
                perf.times_unfalsifiable += 1
    perf.last_cited_at = last_cited
    perf.last_failed_at = last_failed
    perf.failures = failures

    return Rule(
        id=d["id"],
        statement=d["statement"],
        when=d.get("when", []),         # predicate tree, not tag list
        unless=d.get("unless", []),     # predicate tree, not tag list
        stage=None,                     # v1 uses applies_to_v1.stage
        strength=strength,
        status=RuleStatus(d.get("status", "active")),
        citations=d.get("citations", []),
        rationale=d.get("rationale", ""),
        performance=perf,
        created_at=d.get("authored_at", _now()),
        updated_at=d.get("authored_at", _now()),
        parent_rule_id=None,
        # v1 extras
        schema_version=1,
        kind=d.get("kind", "tendency"),
        applies_to_v1=d.get("applies_to", {}),
        predicts=d.get("predicts", []),
        prevents=d.get("prevents", []),
        examples=d.get("examples", {}),
        history=d.get("history", []),
        authored_by=d.get("authored_by", ""),
        authored_at=d.get("authored_at", ""),
    )


def _dump_v1_rule(r: Rule) -> dict:
    """Serialize a v1 Rule back to its on-disk shape. Counters are NOT
    written (they're derived from history); the full append-only history
    is the source of truth.
    """
    return {
        "id":         r.id,
        "version":    1,
        "statement":  r.statement,
        "kind":       r.kind,
        "strength":   _v1_strength_back(r.strength),
        "status":     r.status.value,
        "applies_to": r.applies_to_v1,
        "when":       r.when,
        "unless":     r.unless,
        "predicts":   r.predicts,
        "prevents":   r.prevents,
        "rationale":  r.rationale,
        "citations":  r.citations,
        "examples":   r.examples,
        "authored_by": r.authored_by,
        "authored_at": r.authored_at,
        "history":    r.history,
    }
