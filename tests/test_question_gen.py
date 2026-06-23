"""
tests/test_question_gen.py
==========================
Pin the auto question-generator (agent/question_gen.py): questions are
generated where a design FEATURE meets a measurable CONSEQUENCE meets a
RULE that predicts it — tailored to the design, and never reading the
measurement values (the answers).
"""
from __future__ import annotations

from agent.core import WorldModel
from agent.question_gen import generate_questions

# A pack whose rules predict the five RTL measurement keys.
_KEYS = ["rtl.lint.latch.count", "rtl.lint.case_incomplete.count",
         "rtl.lint.blkseq.count", "rtl.lint.width.count",
         "rtl.lint.fsm_no_default.count"]
_PACK = {"rules": [{"predicts": [{"measurement_key": k} for k in _KEYS]}]}


def _world(facts: dict) -> WorldModel:
    w = WorldModel(domain="vlsi", raw_inputs=[])
    for k, v in facts.items():
        w.add(k, v, source="test")
    return w


def _keys(qs) -> set[str]:
    return {q["verdict"]["measurement_key"] for q in qs}


class TestTailoring:
    def test_full_featured_design_gets_all(self):
        w = _world({"rtl.module.top": "m", "rtl.always_comb_blocks": 3,
                    "rtl.case_blocks": 2, "rtl.always_ff_blocks": 1,
                    "rtl.operator_max_bitwidth": 32, "rtl.fsms": 1})
        assert _keys(generate_questions(w, _PACK)) == set(_KEYS)

    def test_fifo_like_skips_irrelevant(self):
        # No always_comb / case / fsm -> no latch/case/fsm questions.
        w = _world({"rtl.module.top": "fifo", "rtl.always_comb_blocks": 0,
                    "rtl.case_blocks": 0, "rtl.always_ff_blocks": 2,
                    "rtl.operator_max_bitwidth": 5, "rtl.fsms": 0})
        keys = _keys(generate_questions(w, _PACK))
        assert keys == {"rtl.lint.blkseq.count", "rtl.lint.width.count"}
        assert "rtl.lint.latch.count" not in keys


class TestBasisThree:
    def test_pack_filter_excludes_unpredicted_keys(self):
        # Features present, but no rule predicts anything -> no questions.
        w = _world({"rtl.module.top": "m", "rtl.always_comb_blocks": 3})
        assert generate_questions(w, {"rules": []}) == []

    def test_no_pack_emits_all_relevant(self):
        w = _world({"rtl.module.top": "m", "rtl.always_comb_blocks": 3})
        assert "rtl.lint.latch.count" in _keys(generate_questions(w, None))


class TestShape:
    def test_question_dict_shape(self):
        w = _world({"rtl.module.top": "mymod", "rtl.always_ff_blocks": 1,
                    "rtl.operator_max_bitwidth": 8})
        q = generate_questions(w, _PACK)[0]
        assert q["auto_generated"] is True
        assert q["stage"] == "rtl"
        assert q["verdict"]["measurement_key"].startswith("rtl.lint.")
        assert "mymod" in q["question"]

    def test_non_rtl_stage_empty(self):
        w = _world({"rtl.module.top": "m", "rtl.always_ff_blocks": 1})
        assert generate_questions(w, _PACK, stage="synth") == []
