"""
tests/test_verdict.py
=====================
Coverage for the verdict layer's six channels and the combiner.

Channels (high-trust to low):
  intervals  -> numeric bounds vs measurements[key]
  enum       -> exact categorical equality
  ranking    -> target lands inside position_band of a set
  includes   -> any required substring present in reality summary
  excludes   -> none of the forbidden substrings present
  legacy    -> contains_verdict on positive_substrings (back-compat)

These tests pin down semantics that have been the source of past bugs:
  - run #1's contains_verdict required substring in BOTH claim and
    reality. We document the relaxed behavior here.
  - includes is SOFT (any-hit), not all-of, because the predictor is
    asked for synonyms.
  - direction-tolerance rescues numeric-wrong to WRONG_BUT_RIGHT_DIRECTION.
"""
from __future__ import annotations

from agent.core import VerdictKind, Confidence
from agent import verdict as V

from tests.conftest import make_prediction, make_reality


# ---------------------------------------------------------------------------
# Channel: intervals
# ---------------------------------------------------------------------------

class TestIntervals:
    def test_inside_band_is_right(self):
        p = make_prediction(structured_claim={"intervals": {"area_um2": [1000, 2000]}})
        r = make_reality({"area_um2": 1500})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON
        assert v.verdict_confidence == "high"
        assert "intervals" in v.channel

    def test_outside_band_close_is_wrong_but_right_direction(self):
        # band [1000, 2000], midpoint 1500, tolerance 0.40 => actual within 600 of 1500.
        p = make_prediction(structured_claim={"intervals": {"area_um2": [1000, 2000]}})
        r = make_reality({"area_um2": 2100})  # 600 from midpoint, just within tol
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.WRONG_BUT_RIGHT_DIRECTION

    def test_outside_band_far_is_wrong_and_wrong(self):
        p = make_prediction(structured_claim={"intervals": {"area_um2": [1000, 2000]}})
        r = make_reality({"area_um2": 50000})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.WRONG_AND_WRONG_REASON

    def test_legacy_quantitative_is_promoted_to_intervals(self):
        # Tests the back-compat path: prediction.quantitative gets folded
        # into structured.intervals automatically.
        p = make_prediction(quantitative={"area_um2": [1000, 2000]})
        r = make_reality({"area_um2": 1500})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON
        assert v.verdict_confidence == "high"

    def test_missing_measurement_key_is_unfalsifiable(self):
        p = make_prediction(structured_claim={"intervals": {"area_um2": [1000, 2000]}})
        r = make_reality({"unrelated_key": 5})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.UNFALSIFIABLE


# ---------------------------------------------------------------------------
# Channel: enum
# ---------------------------------------------------------------------------

class TestEnum:
    def test_exact_match_is_right(self):
        p = make_prediction(structured_claim={"enum": {"weather": "rainy"}})
        r = make_reality({"weather": "rainy"})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON
        assert v.verdict_confidence == "high"

    def test_case_insensitive_match(self):
        p = make_prediction(structured_claim={"enum": {"weather": "RAINY"}})
        r = make_reality({"weather": "rainy"})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON

    def test_mismatch_is_wrong(self):
        p = make_prediction(structured_claim={"enum": {"weather": "rainy"}})
        r = make_reality({"weather": "sunny"})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.WRONG_AND_WRONG_REASON


# ---------------------------------------------------------------------------
# Channel: ranking
# ---------------------------------------------------------------------------

class TestRanking:
    def test_target_in_band_is_right(self):
        p = make_prediction(structured_claim={"ranking": {
            "set_key": "modules_by_size",
            "target": "alu",
            "position_band": [1, 3],
        }})
        r = make_reality({"modules_by_size": ["decoder", "alu", "regfile", "csr"]})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON

    def test_target_outside_band_is_wrong(self):
        p = make_prediction(structured_claim={"ranking": {
            "set_key": "modules_by_size",
            "target": "csr",
            "position_band": [1, 2],
        }})
        r = make_reality({"modules_by_size": ["decoder", "alu", "regfile", "csr"]})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.WRONG_AND_WRONG_REASON

    def test_target_not_in_set_is_wrong(self):
        p = make_prediction(structured_claim={"ranking": {
            "set_key": "modules_by_size",
            "target": "missing_module",
            "position_band": [1, 2],
        }})
        r = make_reality({"modules_by_size": ["a", "b", "c"]})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.WRONG_AND_WRONG_REASON

    def test_set_not_a_list_is_unfalsifiable(self):
        # Documented schema mismatch from run_001 — Yosys oracle returned
        # a dict for module_cell_count_ranking. Engine must not crash.
        p = make_prediction(structured_claim={"ranking": {
            "set_key": "modules_by_size",
            "target": "alu",
            "position_band": [1, 3],
        }})
        r = make_reality({"modules_by_size": {"alu": 1, "decoder": 2}})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.UNFALSIFIABLE

    def test_ranking_finds_module_by_dict_field(self):
        # Real-world set items are often dicts with {name, cells}.
        p = make_prediction(structured_claim={"ranking": {
            "set_key": "modules_by_size",
            "target": "alu",
            "position_band": [1, 3],
        }})
        r = make_reality({"modules_by_size": [
            {"name": "decoder", "cells": 500},
            {"name": "alu",     "cells": 400},
            {"name": "csr",     "cells": 200},
        ]})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON


# ---------------------------------------------------------------------------
# Channel: includes (medium trust, soft any-hit)
# ---------------------------------------------------------------------------

class TestIncludes:
    def test_any_required_substring_is_right(self):
        # SOFT match: any one substring is enough. "precipitation" hits
        # even if "clear" does not — predictor is asked for synonyms so
        # all-of would be too strict.
        p = make_prediction(structured_claim={"includes": ["precipitation", "clear"]})
        r = make_reality({"summary": "no precipitation observed today"})
        v = V.verdict_for(p, r, summary_key_hints=["summary"])
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON
        assert v.verdict_confidence == "medium"

    def test_no_substring_match_is_wrong(self):
        p = make_prediction(structured_claim={"includes": ["thunderstorm", "hail"]})
        r = make_reality({"summary": "clear sunny skies"})
        v = V.verdict_for(p, r, summary_key_hints=["summary"])
        assert v.kind == VerdictKind.WRONG_AND_WRONG_REASON


# ---------------------------------------------------------------------------
# Channel: excludes (medium trust, all-must-be-absent)
# ---------------------------------------------------------------------------

class TestExcludes:
    def test_no_forbidden_present_is_right(self):
        p = make_prediction(structured_claim={"excludes": ["error", "fail"]})
        r = make_reality({"summary": "all checks passed cleanly"})
        v = V.verdict_for(p, r, summary_key_hints=["summary"])
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON

    def test_any_forbidden_present_is_wrong(self):
        p = make_prediction(structured_claim={"excludes": ["error", "fail"]})
        r = make_reality({"summary": "compilation passed but synthesis hit error"})
        v = V.verdict_for(p, r, summary_key_hints=["summary"])
        assert v.kind == VerdictKind.WRONG_AND_WRONG_REASON


# ---------------------------------------------------------------------------
# Channel: legacy (low trust) + back-compat shims
# ---------------------------------------------------------------------------

class TestLegacy:
    def test_legacy_contains_verdict_relaxed_substring_in_reality_only(self):
        # Run #1 bug: required substring in BOTH claim and reality.
        # Pin down the relaxed behavior — substring only needs to be in reality.
        p = make_prediction(claim="will pass cleanly")
        r = make_reality({"summary": "no precipitation observed"})
        v = V.contains_verdict(p, r, "summary", ["no_precip", "dry"])
        # "no_precip" is not in reality literally, "dry" not either —
        # but the includes channel will fire first because we passed
        # summary_key_hints. Let's use a token that IS in reality.
        v2 = V.contains_verdict(p, r, "summary", ["precipitation"])
        assert v2.kind == VerdictKind.RIGHT_AND_RIGHT_REASON

    def test_numeric_verdict_shim_works(self):
        # Back-compat wrapper: numeric_verdict over prediction.quantitative.
        p = make_prediction(quantitative={"area": [1000, 2000]})
        r = make_reality({"area": 1500})
        v = V.numeric_verdict(p, r, "area")
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON

    def test_numeric_verdict_no_bounds_is_unfalsifiable(self):
        p = make_prediction(quantitative=None)
        r = make_reality({"area": 1500})
        v = V.numeric_verdict(p, r, "area")
        assert v.kind == VerdictKind.UNFALSIFIABLE

    def test_categorical_verdict_shim_works(self):
        p = make_prediction(claim="rainy")
        r = make_reality({"weather": "rainy"})
        v = V.categorical_verdict(p, r, "weather",
                                   lambda actual, claim: str(actual).lower() == claim.lower())
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON


# ---------------------------------------------------------------------------
# Combiner: cross-channel interactions
# ---------------------------------------------------------------------------

class TestCombiner:
    def test_high_tier_overrides_medium_when_consistent(self):
        # Numeric right + includes right -> RIGHT_AND_RIGHT, high.
        p = make_prediction(structured_claim={
            "intervals": {"area": [1000, 2000]},
            "includes":  ["timing", "met"],
        })
        r = make_reality({"area": 1500, "summary": "timing met cleanly"})
        v = V.verdict_for(p, r, summary_key_hints=["summary"])
        assert v.kind == VerdictKind.RIGHT_AND_RIGHT_REASON
        assert v.verdict_confidence == "high"

    def test_high_right_medium_wrong_is_right_but_wrong_reason(self):
        # Numbers say right, prose says wrong -> RIGHT_BUT_WRONG_REASON.
        p = make_prediction(structured_claim={
            "intervals": {"area": [1000, 2000]},
            "includes":  ["timing_met"],
        })
        r = make_reality({"area": 1500, "summary": "violations found"})
        v = V.verdict_for(p, r, summary_key_hints=["summary"])
        assert v.kind == VerdictKind.RIGHT_BUT_WRONG_REASON

    def test_no_channel_decided_is_unfalsifiable(self):
        # No structured_claim, no quantitative, nothing.
        p = make_prediction()
        r = make_reality({"unrelated": 7})
        v = V.verdict_for(p, r)
        assert v.kind == VerdictKind.UNFALSIFIABLE
        assert v.verdict_confidence == "low"

    def test_rule_attribution_carries_status(self):
        # Right verdict -> all cited rules marked supported.
        p = make_prediction(
            structured_claim={"intervals": {"area": [1000, 2000]}},
            cited_rule_ids=["rule_a", "rule_b"],
        )
        r = make_reality({"area": 1500})
        v = V.verdict_for(p, r)
        assert v.rule_attribution == {"rule_a": "supported", "rule_b": "supported"}
