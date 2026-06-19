"""
Tests for agent.sweep — the deterministic rule classifier.
"""
from __future__ import annotations

from agent.core import WorldModel
from agent.sweep import sweep, _check_intervals, _check_enum, _check_includes, _check_excludes


# ---------------------------------------------------------------------------
# Channel evaluators
# ---------------------------------------------------------------------------

def test_intervals_inside_band_is_clean():
    s, _ = _check_intervals(5, {"min": 0, "max": 10})
    assert s == "clean"


def test_intervals_below_band_is_violation():
    s, r = _check_intervals(-1, {"min": 0, "max": 10})
    assert s == "violation"
    assert "below" in r


def test_intervals_above_band_is_violation():
    s, r = _check_intervals(11, {"min": 0, "max": 10})
    assert s == "violation"
    assert "above" in r


def test_intervals_open_lower_bound():
    # min omitted → only upper bound enforced
    assert _check_intervals(-1000, {"max": 10})[0] == "clean"


def test_intervals_non_numeric_is_na():
    assert _check_intervals("hello", {"min": 0, "max": 10})[0] == "na"


def test_intervals_missing_is_na():
    assert _check_intervals(None, {"min": 0, "max": 10})[0] == "na"


def test_enum_match_is_clean():
    assert _check_enum("multdiv_fast", ["multdiv", "mult"])[0] == "clean"


def test_enum_no_match_is_violation():
    assert _check_enum("regfile", ["multdiv", "mult"])[0] == "violation"


def test_includes_present_is_clean():
    assert _check_includes(["fetch", "decode", "exec"], ["decode"])[0] == "clean"


def test_includes_missing_is_violation():
    assert _check_includes(["fetch", "decode"], ["mul"])[0] == "violation"


def test_excludes_present_is_violation():
    assert _check_excludes(["fetch", "multdiv", "exec"], ["multdiv"])[0] == "violation"


def test_excludes_absent_is_clean():
    assert _check_excludes(["fetch", "decode"], ["multdiv"])[0] == "clean"


# ---------------------------------------------------------------------------
# Full sweep behavior
# ---------------------------------------------------------------------------

class _FakeReality:
    def __init__(self, m): self.measurements = dict(m)


def _world_with(tags=None, facts=None):
    w = WorldModel(domain="vlsi")
    for t in tags or []:
        w.tags.add(t)
    for k, v in (facts or {}).items():
        w.add(k, v, source="test")
    return w


_PACK_BASE = {
    "schema": "kb-rule/v1",
    "rules": [
        {
            "id": "r_test_latch_zero",
            "version": 1,
            "kind": "constraint",
            "strength": "high",
            "statement": "No inferred latches.",
            "applies_to": {"stage": ["rtl"]},
            "when": [{"op": "tag", "name": "rtl_stage"}],
            "unless": [],
            "predicts": [{
                "measurement_key": "rtl.lint.latch.count",
                "channel": "intervals",
                "value": {"min": 0, "max": 0},
            }],
            "citations": [],
        },
        {
            "id": "r_test_pdk_gated",
            "version": 1,
            "kind": "tendency",
            "strength": "medium",
            "statement": "Sky130 area band.",
            "applies_to": {"stage": ["synth"]},
            "when": [
                {"op": "tag", "name": "synth_stage"},
                {"op": "tag", "name": "pdk_sky130"},
            ],
            "unless": [],
            "predicts": [{
                "measurement_key": "synth.total_cell_area_um2",
                "channel": "intervals",
                "value": {"min": 1000, "max": 2000},
            }],
            "citations": [],
        },
    ],
}


def test_sweep_marks_violation():
    world = _world_with(tags=["rtl_stage"])
    reality = _FakeReality({"rtl.lint.latch.count": 2})
    rep = sweep(_PACK_BASE, world, reality, stage_filter="rtl")
    rids = {r.rule_id: r for r in rep.rules}
    assert rids["r_test_latch_zero"].status == "violation"
    assert rep.n_violations == 1


def test_sweep_marks_clean():
    world = _world_with(tags=["rtl_stage"])
    reality = _FakeReality({"rtl.lint.latch.count": 0})
    rep = sweep(_PACK_BASE, world, reality, stage_filter="rtl")
    rids = {r.rule_id: r for r in rep.rules}
    assert rids["r_test_latch_zero"].status == "clean"
    assert rep.n_clean == 1


def test_sweep_pdk_gate_skipped_when_tag_missing():
    """The whole point of PDK gating: if pdk.* tag missing, skip silently."""
    world = _world_with(tags=["synth_stage"])  # no pdk_sky130
    reality = _FakeReality({"synth.total_cell_area_um2": 1500})
    rep = sweep(_PACK_BASE, world, reality, stage_filter="synth")
    rids = {r.rule_id: r for r in rep.rules}
    assert rids["r_test_pdk_gated"].status == "skipped"
    assert "pdk" in rids["r_test_pdk_gated"].reason.lower()
    assert rep.n_violations == 0   # critical: not a false positive
    assert rep.n_skipped == 1


def test_sweep_pdk_gate_active_when_tag_supplied():
    world = _world_with(tags=["synth_stage", "pdk_sky130"])
    reality = _FakeReality({"synth.total_cell_area_um2": 1500})
    rep = sweep(_PACK_BASE, world, reality, stage_filter="synth")
    rids = {r.rule_id: r for r in rep.rules}
    assert rids["r_test_pdk_gated"].status == "clean"


def test_sweep_na_when_measurement_absent():
    world = _world_with(tags=["rtl_stage"])
    reality = _FakeReality({})  # no measurements
    rep = sweep(_PACK_BASE, world, reality, stage_filter="rtl")
    rids = {r.rule_id: r for r in rep.rules}
    assert rids["r_test_latch_zero"].status == "na"


def test_sweep_stage_filter():
    """Synth-stage rules don't appear when filtering for rtl."""
    world = _world_with(tags=["rtl_stage"])
    reality = _FakeReality({"rtl.lint.latch.count": 0})
    rep = sweep(_PACK_BASE, world, reality, stage_filter="rtl")
    ids = [r.rule_id for r in rep.rules]
    assert "r_test_latch_zero" in ids
    assert "r_test_pdk_gated" not in ids


def test_sweep_real_packs_load_and_classify():
    """Smoke: load real packs, ensure sweep doesn't crash."""
    import json
    rtl = json.load(open("packs/rtl/rules.json"))
    synth = json.load(open("packs/synth/rules.json"))
    world = _world_with(tags=["rtl_stage"])
    rep_rtl = sweep(rtl, world, _FakeReality({}), stage_filter="rtl")
    assert rep_rtl.n_rules_total == len(rtl["rules"])
    world2 = _world_with(tags=["synth_stage"])
    rep_syn = sweep(synth, world2, _FakeReality({}), stage_filter="synth")
    assert rep_syn.n_rules_total == len(synth["rules"])
