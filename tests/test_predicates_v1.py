"""Phase 2e tests — predicate evaluator + v1 KB integration.

Covers:
- every predicate op: tag, eq, ne, lt, lte, gt, gte, in, matches, exists,
  all, any, not
- every rule kind: constraint, tendency, heuristic, identity
- v1 prevents/predicts roundtrip on save+load
- code_origin filter through the predicate path
- v0 -> v1 idempotence: loading a v0 pack and saving doesn't corrupt it,
  loading a v1 pack and saving + reloading is idempotent
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from agent.core import Rule, RuleStrength, RuleStatus, WorldModel
from agent.kb import KnowledgeBase
from agent.predicates import evaluate, evaluate_when_unless


# ---------------------------------------------------------------------------
# Fixture: world + reality + target
# ---------------------------------------------------------------------------

def _world() -> WorldModel:
    w = WorldModel(domain="test")
    w.add("core.width", 32, source="t", tags=["rtl_stage"])
    w.add("core.code_origin", "ai_generated", source="t", tags=["ai_generated"])
    w.add("rtl.top", "ibex_core", source="t")
    w.add("pdk.node_nm", 16, source="t")
    w.add("rtl.has_multiplier", True, source="t", tags=["has_multiplier"])
    return w


class _Reality:
    def __init__(self, m): self.measurements = m


def _target() -> dict:
    return {"target.fmax_ghz": 1.0}


# ---------------------------------------------------------------------------
# 1) Every predicate op
# ---------------------------------------------------------------------------

class TestOps:
    def test_tag_hit_and_miss(self):
        w = _world()
        assert evaluate({"op": "tag", "name": "rtl_stage"}, w) is True
        assert evaluate({"op": "tag", "name": "synth_stage"}, w) is False

    def test_eq_ne(self):
        w = _world()
        assert evaluate({"op": "eq", "key": "core.width", "value": 32}, w) is True
        assert evaluate({"op": "eq", "key": "core.width", "value": 64}, w) is False
        assert evaluate({"op": "ne", "key": "core.width", "value": 64}, w) is True
        assert evaluate({"op": "ne", "key": "core.width", "value": 32}, w) is False

    def test_lt_lte_gt_gte(self):
        w = _world()
        assert evaluate({"op": "lt",  "key": "core.width", "value": 64}, w) is True
        assert evaluate({"op": "lt",  "key": "core.width", "value": 32}, w) is False
        assert evaluate({"op": "lte", "key": "core.width", "value": 32}, w) is True
        assert evaluate({"op": "gt",  "key": "core.width", "value": 16}, w) is True
        assert evaluate({"op": "gt",  "key": "core.width", "value": 32}, w) is False
        assert evaluate({"op": "gte", "key": "core.width", "value": 32}, w) is True

    def test_in_membership(self):
        w = _world()
        assert evaluate({"op": "in", "key": "pdk.node_nm",
                          "values": [7, 16, 22]}, w) is True
        assert evaluate({"op": "in", "key": "pdk.node_nm",
                          "values": [7, 22]}, w) is False

    def test_matches_regex(self):
        w = _world()
        assert evaluate({"op": "matches", "key": "rtl.top",
                          "pattern": r"^ibex_"}, w) is True
        assert evaluate({"op": "matches", "key": "rtl.top",
                          "pattern": r"^riscv_"}, w) is False
        # bad regex -> False, no exception
        assert evaluate({"op": "matches", "key": "rtl.top",
                          "pattern": "[unterminated"}, w) is False

    def test_exists(self):
        w = _world()
        assert evaluate({"op": "exists", "key": "core.width"}, w) is True
        assert evaluate({"op": "exists", "key": "rtl.lint.LATCH.count"}, w) is False

    def test_all_any_not(self):
        w = _world()
        all_node = {"op": "all", "preds": [
            {"op": "tag", "name": "rtl_stage"},
            {"op": "eq", "key": "core.width", "value": 32},
        ]}
        assert evaluate(all_node, w) is True

        all_fail = {"op": "all", "preds": [
            {"op": "tag", "name": "rtl_stage"},
            {"op": "eq", "key": "core.width", "value": 64},
        ]}
        assert evaluate(all_fail, w) is False

        any_node = {"op": "any", "preds": [
            {"op": "eq", "key": "core.width", "value": 64},
            {"op": "eq", "key": "core.width", "value": 32},
        ]}
        assert evaluate(any_node, w) is True

        not_node = {"op": "not", "pred": {"op": "tag", "name": "synth_stage"}}
        assert evaluate(not_node, w) is True

    def test_three_tier_lookup(self):
        """key resolves from facts, then measurements, then target."""
        w = _world()
        r = _Reality({"synth.total_cells": 5000})
        t = _target()

        # facts
        assert evaluate({"op": "eq", "key": "core.width", "value": 32}, w, r, t) is True
        # measurements
        assert evaluate({"op": "gt", "key": "synth.total_cells", "value": 100},
                          w, r, t) is True
        # target
        assert evaluate({"op": "gte", "key": "target.fmax_ghz", "value": 1.0},
                          w, r, t) is True

    def test_missing_key_is_false(self):
        w = _world()
        assert evaluate({"op": "eq", "key": "synth.no_such", "value": 0}, w) is False
        assert evaluate({"op": "lt", "key": "synth.no_such", "value": 1}, w) is False

    def test_unknown_op_is_false(self):
        w = _world()
        assert evaluate({"op": "weird_op", "key": "core.width"}, w) is False
        assert evaluate({"not_a_node": True}, w) is False
        assert evaluate({}, w) is False

    def test_when_unless_combined(self):
        w = _world()
        when = [{"op": "tag", "name": "rtl_stage"}]
        unless = [{"op": "tag", "name": "skip_me"}]
        assert evaluate_when_unless(when, unless, w) is True
        # adding a matching unless tag flips it to False
        w.tags.add("skip_me")
        assert evaluate_when_unless(when, unless, w) is False


# ---------------------------------------------------------------------------
# 2) Every rule kind round-trips through a v1 pack
# ---------------------------------------------------------------------------

V1_RULE_TEMPLATE = {
    "version": 1,
    "statement": "test rule",
    "strength": "high",
    "status": "active",
    "applies_to": {"stage": ["rtl"], "tools": [], "pdks": [],
                    "design_class": ["any"], "code_origin": ["any"]},
    "when":   [{"op": "tag", "name": "rtl_stage"}],
    "unless": [],
    "rationale": "test",
    "citations": [{"title": "test", "url": "https://example.com"}],
    "examples": {},
    "authored_by": "test",
    "authored_at": "2026-05-06T00:00:00Z",
    "history": [],
}


def _pack_with(rules):
    return {
        "pack": "test", "version": "1.0.0", "schema": "kb-rule/v1",
        "stages": ["rtl"], "tools": [], "key_index": {}, "rules": rules,
    }


def _save_load(pack):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(pack, f)
        path = f.name
    try:
        return KnowledgeBase.load(path), path
    finally:
        pass  # caller cleans up


class TestKinds:
    def test_constraint(self):
        rule = {**V1_RULE_TEMPLATE, "id": "r_t_constraint", "kind": "constraint",
                "predicts": [{"measurement_key": "rtl.lint.LATCH.count",
                              "channel": "intervals",
                              "value": {"min": 0, "max": 0},
                              "horizon": "rtl"}],
                "prevents": [{"downstream_stage": "synth",
                              "downstream_key": "synth.warnings.LATCH",
                              "mechanism": "early lint catch",
                              "estimated_cost_saved_hours": 4}]}
        kb, _ = _save_load(_pack_with([rule]))
        r = kb.by_id("r_t_constraint")
        assert r.kind == "constraint"
        assert r.strength == RuleStrength.STRONG
        assert r.predicts[0]["measurement_key"] == "rtl.lint.LATCH.count"
        assert r.prevents[0]["downstream_stage"] == "synth"

    def test_tendency(self):
        rule = {**V1_RULE_TEMPLATE, "id": "r_t_tendency", "kind": "tendency",
                "strength": "medium",
                "predicts": [{"measurement_key": "synth.total_cell_area_um2",
                              "channel": "intervals",
                              "value": {"min": 30000, "max": 35000, "unit": "um2"},
                              "horizon": "synth"}],
                "prevents": []}
        kb, _ = _save_load(_pack_with([rule]))
        r = kb.by_id("r_t_tendency")
        assert r.kind == "tendency"
        assert r.strength == RuleStrength.TENDENCY

    def test_heuristic(self):
        rule = {**V1_RULE_TEMPLATE, "id": "r_t_heuristic", "kind": "heuristic",
                "strength": "low",
                "predicts": [{"measurement_key": "synth.top_module_by_cells",
                              "channel": "enum",
                              "value": ["multdiv", "alu", "regfile"],
                              "horizon": "synth"}],
                "prevents": []}
        kb, _ = _save_load(_pack_with([rule]))
        r = kb.by_id("r_t_heuristic")
        assert r.kind == "heuristic"
        assert r.strength == RuleStrength.HEURISTIC

    def test_identity(self):
        rule = {**V1_RULE_TEMPLATE, "id": "r_t_identity", "kind": "identity",
                "predicts": [],
                "prevents": []}
        kb, _ = _save_load(_pack_with([rule]))
        r = kb.by_id("r_t_identity")
        assert r.kind == "identity"
        assert r.predicts == []


# ---------------------------------------------------------------------------
# 3) prevents round-trip
# ---------------------------------------------------------------------------

class TestPreventsRoundtrip:
    def test_prevents_preserved_through_save_load(self, tmp_path):
        rule = {**V1_RULE_TEMPLATE, "id": "r_t_prev", "kind": "constraint",
                "predicts": [{"measurement_key": "rtl.lint.LATCH.count",
                              "channel": "intervals",
                              "value": {"min": 0, "max": 0},
                              "horizon": "rtl"}],
                "prevents": [
                    {"downstream_stage": "synth",
                     "downstream_key": "synth.warnings.LATCH",
                     "mechanism": "early catch",
                     "estimated_cost_saved_hours": 6},
                    {"downstream_stage": "sta",
                     "downstream_key": "sta.failing_endpoints",
                     "mechanism": "no latch in path",
                     "estimated_cost_saved_hours": 3},
                ]}
        path = tmp_path / "p.json"
        path.write_text(json.dumps(_pack_with([rule])))
        kb = KnowledgeBase.load(str(path))
        # save then reload
        out = tmp_path / "p2.json"
        kb.pack_path = str(out)
        kb.save()
        kb2 = KnowledgeBase.load(str(out))
        r2 = kb2.by_id("r_t_prev")
        assert len(r2.prevents) == 2
        assert r2.prevents[0]["downstream_stage"] == "synth"
        assert r2.prevents[1]["estimated_cost_saved_hours"] == 3


# ---------------------------------------------------------------------------
# 4) code_origin filter
# ---------------------------------------------------------------------------

class TestCodeOriginFilter:
    def test_code_origin_predicate_matches_world(self):
        """A rule that fires only on AI-generated RTL uses an `in` predicate
        on core.code_origin. The world reports code_origin=ai_generated;
        the rule's `when` should evaluate true."""
        w = _world()
        when = [
            {"op": "tag", "name": "rtl_stage"},
            {"op": "in", "key": "core.code_origin",
             "values": ["ai_generated", "ai_assisted"]},
        ]
        assert evaluate_when_unless(when, [], w) is True

    def test_code_origin_predicate_misses_human(self):
        w = WorldModel(domain="test")
        w.add("core.code_origin", "human", source="t", tags=["rtl_stage"])
        when = [
            {"op": "tag", "name": "rtl_stage"},
            {"op": "in", "key": "core.code_origin",
             "values": ["ai_generated", "ai_assisted"]},
        ]
        assert evaluate_when_unless(when, [], w) is False

    def test_rule_applies_to_uses_predicates(self, tmp_path):
        """v1 Rule.applies_to() should call the predicate path."""
        rule = {**V1_RULE_TEMPLATE, "id": "r_t_origin",
                "kind": "constraint",
                "applies_to": {"stage": ["rtl"], "tools": [], "pdks": [],
                                "design_class": ["any"],
                                "code_origin": ["ai_generated"]},
                "when": [
                    {"op": "tag", "name": "rtl_stage"},
                    {"op": "in", "key": "core.code_origin",
                     "values": ["ai_generated", "ai_assisted"]},
                ],
                "predicts": [], "prevents": []}
        path = tmp_path / "p.json"
        path.write_text(json.dumps(_pack_with([rule])))
        kb = KnowledgeBase.load(str(path))
        r = kb.by_id("r_t_origin")
        # AI world: should apply
        w_ai = _world()
        assert r.applies_to(w_ai, stage="rtl") is True
        # human world: should not apply
        w_human = WorldModel(domain="test")
        w_human.add("core.code_origin", "human", source="t", tags=["rtl_stage"])
        assert r.applies_to(w_human, stage="rtl") is False


# ---------------------------------------------------------------------------
# 5) v0 -> v1 idempotence
# ---------------------------------------------------------------------------

class TestV0V1Idempotence:
    def test_v0_pack_loads(self, tmp_path):
        """Legacy v0 pack format still loads cleanly."""
        v0 = {"rules": [{
            "id": "r_v0",
            "statement": "legacy rule",
            "when": ["legacy_tag"],
            "unless": [],
            "stage": "synth",
            "strength": "tendency",
            "status": "active",
            "citations": ["http://example.com/old"],
            "rationale": "legacy",
        }]}
        path = tmp_path / "v0.json"
        path.write_text(json.dumps(v0))
        kb = KnowledgeBase.load(str(path))
        r = kb.by_id("r_v0")
        assert r.schema_version == 0
        assert r.kind == "tendency"  # default
        assert r.predicts == []
        assert r.prevents == []
        assert r.history == []

    def test_v1_save_reload_idempotent(self, tmp_path):
        """Loading v1, saving, reloading produces identical rule contents."""
        rule = {**V1_RULE_TEMPLATE, "id": "r_idem", "kind": "constraint",
                "predicts": [{"measurement_key": "rtl.lint.LATCH.count",
                              "channel": "intervals",
                              "value": {"min": 0, "max": 0},
                              "horizon": "rtl"}],
                "prevents": [{"downstream_stage": "synth",
                              "downstream_key": "synth.warnings.LATCH",
                              "mechanism": "x",
                              "estimated_cost_saved_hours": 2}]}
        original_pack = _pack_with([rule])
        path = tmp_path / "p.json"
        path.write_text(json.dumps(original_pack))

        kb1 = KnowledgeBase.load(str(path))
        out = tmp_path / "p2.json"
        kb1.pack_path = str(out)
        kb1.save()

        # second load
        kb2 = KnowledgeBase.load(str(out))
        out3 = tmp_path / "p3.json"
        kb2.pack_path = str(out3)
        kb2.save()

        with open(out) as f:
            saved1 = json.load(f)
        with open(out3) as f:
            saved2 = json.load(f)

        assert saved1["schema"] == "kb-rule/v1"
        assert saved2["schema"] == "kb-rule/v1"
        assert saved1["rules"] == saved2["rules"], "v1 save must be idempotent"

    def test_v1_envelope_preserved(self, tmp_path):
        """Pack envelope (pack/version/schema/stages/tools/key_index)
        survives load -> save round-trip."""
        rule = {**V1_RULE_TEMPLATE, "id": "r_env", "kind": "tendency",
                "predicts": [], "prevents": []}
        original = _pack_with([rule])
        original["tools"] = ["yosys", "abc"]
        original["key_index"] = {"synth.total_cells": {"type": "int"}}

        path = tmp_path / "p.json"
        path.write_text(json.dumps(original))
        kb = KnowledgeBase.load(str(path))
        out = tmp_path / "p2.json"
        kb.pack_path = str(out)
        kb.save()

        with open(out) as f:
            saved = json.load(f)
        assert saved["pack"] == "test"
        assert saved["schema"] == "kb-rule/v1"
        assert saved["tools"] == ["yosys", "abc"]
        assert saved["key_index"] == {"synth.total_cells": {"type": "int"}}

    def test_history_drives_perf_counters(self, tmp_path):
        """Counters are derived from history events at load time."""
        rule = {**V1_RULE_TEMPLATE, "id": "r_hist",
                "kind": "tendency", "predicts": [], "prevents": [],
                "history": [
                    {"event": "outcome", "verdict": "right",
                     "prediction_id": "p1", "at": "2026-05-06T00:00:00Z"},
                    {"event": "outcome", "verdict": "right",
                     "prediction_id": "p2", "at": "2026-05-06T00:00:01Z"},
                    {"event": "outcome", "verdict": "wrong",
                     "prediction_id": "p3", "at": "2026-05-06T00:00:02Z"},
                    {"event": "outcome", "verdict": "unfalsifiable",
                     "prediction_id": "p4", "at": "2026-05-06T00:00:03Z"},
                ]}
        path = tmp_path / "p.json"
        path.write_text(json.dumps(_pack_with([rule])))
        kb = KnowledgeBase.load(str(path))
        r = kb.by_id("r_hist")
        assert r.performance.times_right == 2
        assert r.performance.times_wrong == 1
        assert r.performance.times_unfalsifiable == 1
        assert "p3" in r.performance.failures


# ---------------------------------------------------------------------------
# 6) record_citation / record_outcome write history events for v1 rules
# ---------------------------------------------------------------------------

class TestHistoryAppend:
    def test_record_citation_appends_history_for_v1(self, tmp_path):
        rule = {**V1_RULE_TEMPLATE, "id": "r_cite",
                "kind": "tendency", "predicts": [], "prevents": []}
        path = tmp_path / "p.json"
        path.write_text(json.dumps(_pack_with([rule])))
        kb = KnowledgeBase.load(str(path))
        kb.record_citation("r_cite", qid="q1", session_dir="/tmp/sess")
        r = kb.by_id("r_cite")
        assert r.performance.times_cited == 1
        assert len(r.history) == 1
        assert r.history[0]["event"] == "cited"
        assert r.history[0]["qid"] == "q1"

    def test_record_outcome_appends_history_for_v1(self, tmp_path):
        rule = {**V1_RULE_TEMPLATE, "id": "r_out",
                "kind": "tendency", "predicts": [], "prevents": []}
        path = tmp_path / "p.json"
        path.write_text(json.dumps(_pack_with([rule])))
        kb = KnowledgeBase.load(str(path))
        kb.record_outcome("r_out", "right", prediction_id="p1",
                            qid="q1", session_dir="/tmp/sess",
                            evidence_key="rtl.lint.LATCH.count",
                            evidence_value=0)
        r = kb.by_id("r_out")
        assert r.performance.times_right == 1
        assert any(ev.get("event") == "outcome" and ev.get("verdict") == "right"
                    for ev in r.history)

    def test_v0_rules_do_not_get_history(self, tmp_path):
        """v0 rules keep their old mutable counters; we don't fake a
        history list onto them."""
        v0 = {"rules": [{
            "id": "r_v0_no_hist", "statement": "x",
            "when": [], "unless": [], "stage": None,
            "strength": "tendency", "status": "active",
            "citations": [], "rationale": "",
        }]}
        path = tmp_path / "v0.json"
        path.write_text(json.dumps(v0))
        kb = KnowledgeBase.load(str(path))
        kb.record_citation("r_v0_no_hist", qid="q1")
        r = kb.by_id("r_v0_no_hist")
        assert r.performance.times_cited == 1
        assert r.history == []  # untouched
