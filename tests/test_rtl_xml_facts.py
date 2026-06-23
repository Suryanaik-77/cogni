"""
tests/test_rtl_xml_facts.py
===========================
Pin the Verilator-AST fact extractor (adapters/rtl/verilator/xml_facts.py).

Runs against a captured Verilator --xml-only fixture (tests/fixtures/
cmd_alu.xml) so it needs NO Verilator install. The fixture is the AST of
scenarios/rtl_demo/rtl/cmd_alu.sv — a hand-written FSM+ALU with known
structure, so every expected number below is ground truth read off the
source, not off the (occasionally wrong) hand-written manifest.
"""
from __future__ import annotations
import os

from adapters.rtl.verilator.xml_facts import facts_from_xml

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "cmd_alu.xml")


def _facts(**kw):
    with open(_FIXTURE) as f:
        xml = f.read()
    return facts_from_xml(xml, source="cmd_alu.sv", **kw)


class TestStructuralCounts:
    def test_module_and_blocks(self):
        facts, _ = _facts()
        assert facts["rtl.module.top"] == "cmd_alu"
        # 3 always_comb + 1 always_ff = 4 total (manifest's 2/5 were wrong).
        assert facts["rtl.always_comb_blocks"] == 3
        assert facts["rtl.always_ff_blocks"] == 1
        assert facts["rtl.always_blocks"] == 4
        assert facts["rtl.case_blocks"] == 2

    def test_fsm(self):
        facts, _ = _facts()
        assert facts["rtl.fsms"] == 1
        assert facts["rtl.fsm.state_count"] == 4      # S_IDLE/LOAD/EXEC/DONE
        assert facts["rtl.fsm.encoding_declared"] is True

    def test_register_bits(self):
        facts, _ = _facts()
        # state_q(2) + op_a_q(32) + op_b_q(32) + opc_q(4) + acc_q(32) = 102.
        assert facts["rtl.register_bits"] == 102

    def test_clock_and_reset(self):
        facts, _ = _facts()
        assert facts["rtl.clock_domains"] == 1
        assert facts["rtl.reset_domains"] == 1
        assert facts["rtl.reset_strategy"] == "async_low"   # negedge rst_ni
        assert facts["rtl.async_controls"] == 1

    def test_parameters(self):
        facts, _ = _facts()
        assert facts["rtl.module.parameters"] == ["WIDTH"]


class TestPassthroughAndTags:
    def test_human_context_passthrough(self):
        facts, tags = _facts(lines_of_code=150, code_origin="human",
                             author_intent="demo")
        assert facts["core.lines_of_code"] == 150
        assert facts["core.code_origin"] == "human"
        assert facts["core.author_intent"] == "demo"
        assert "human_authored" in tags

    def test_default_origin_is_unknown_not_guessed(self):
        facts, tags = _facts()
        assert facts["core.code_origin"] == "unknown"
        assert "human_authored" not in tags

    def test_recall_tags(self):
        _, tags = _facts(lines_of_code=150)
        for t in ("rtl_stage", "fsm_present", "single_clock",
                  "single_reset_domain", "small_module"):
            assert t in tags
