"""Tests for agent.memory — per-design persistent memory."""
import json
import os
import pytest

from agent.memory import DesignMemory, get_or_create, design_id_from_config


@pytest.fixture
def mem_dir(tmp_path):
    d = str(tmp_path / "designs")
    os.makedirs(d, exist_ok=True)
    return d


class TestDesignMemory:
    def test_create_blank(self, mem_dir):
        mem = DesignMemory("test_design", memory_dir=mem_dir)
        assert mem.design_id == "test_design"
        assert mem.phase == "idle"
        assert mem.run_count() == 0
        assert not mem.has_been_processed()

    def test_save_and_reload(self, mem_dir):
        mem = DesignMemory("test_design", memory_dir=mem_dir)
        mem.set_metadata(stage="rtl", pack_path="packs/rtl/rules.json")
        mem.save()

        mem2 = DesignMemory("test_design", memory_dir=mem_dir)
        assert mem2.data["stage"] == "rtl"
        assert mem2.data["pack_path"] == "packs/rtl/rules.json"

    def test_list_designs(self, mem_dir):
        assert DesignMemory.list_designs(memory_dir=mem_dir) == []
        DesignMemory("alpha", memory_dir=mem_dir).save()
        DesignMemory("beta", memory_dir=mem_dir).save()
        assert DesignMemory.list_designs(memory_dir=mem_dir) == ["alpha", "beta"]

    def test_record_run_lifecycle(self, mem_dir):
        mem = DesignMemory("my_design", memory_dir=mem_dir)
        run = mem.record_run_start("/tmp/session_123", command="run-all-api")
        assert run["status"] == "running"
        assert mem.phase == "preparing"
        assert mem.run_count() == 1

        mem.set_phase("predicting")
        assert mem.phase == "predicting"

        mem.record_run_end(
            "session_123",
            stats={"n_predicted": 5, "n_right": 4, "n_wrong": 1,
                   "n_refused": 0, "n_kb_edits": 2, "cost_usd": 0.71},
            rules_learned=["r_new_rule_1"],
        )
        assert mem.phase == "idle"
        assert mem.run_count() == 1
        assert mem.has_been_processed()

        last = mem.last_run()
        assert last["status"] == "completed"
        assert last["stats"]["n_right"] == 4
        assert last["rules_learned"] == ["r_new_rule_1"]
        assert mem.data["total_cost_usd"] == 0.71

    def test_record_findings(self, mem_dir):
        mem = DesignMemory("my_design", memory_dir=mem_dir)
        mem.record_finding("rtl.lint.latch.count", 2,
                           session_id="s1", verdict="wrong")
        mem.record_finding("rtl.lint.latch.count", 0,
                           session_id="s2", verdict="right")
        mem.save()

        assert mem.latest_finding("rtl.lint.latch.count") == 0
        trend = mem.finding_trend("rtl.lint.latch.count")
        assert len(trend) == 2
        assert trend[0]["measured"] == 2
        assert trend[1]["measured"] == 0

    def test_finding_trend_empty(self, mem_dir):
        mem = DesignMemory("my_design", memory_dir=mem_dir)
        assert mem.finding_trend("nonexistent") == []
        assert mem.latest_finding("nonexistent") is None

    def test_readiness_verdict(self, mem_dir):
        mem = DesignMemory("my_design", memory_dir=mem_dir)
        mem.record_run_start("/tmp/s1", command="ready", session_id="s1")
        mem.record_run_end("s1", readiness_verdict="NO-GO", blockers_remaining=3)
        assert mem.data["current_state"]["readiness_verdict"] == "NO-GO"
        assert mem.data["best_verdict"] == "NO-GO"

        mem.record_run_start("/tmp/s2", command="ready", session_id="s2")
        mem.record_run_end("s2", readiness_verdict="GO", blockers_remaining=0)
        assert mem.data["best_verdict"] == "GO"

    def test_rules_learned_dedup(self, mem_dir):
        mem = DesignMemory("my_design", memory_dir=mem_dir)
        mem.record_run_start("/tmp/s1", command="run-all-api", session_id="s1")
        mem.record_run_end("s1", rules_learned=["rule_a", "rule_b"])
        mem.record_run_start("/tmp/s2", command="run-all-api", session_id="s2")
        mem.record_run_end("s2", rules_learned=["rule_b", "rule_c"])

        all_rules = mem.all_rules_learned()
        rule_ids = [r["rule_id"] for r in all_rules]
        assert rule_ids == ["rule_a", "rule_b", "rule_c"]

    def test_multiple_runs(self, mem_dir):
        mem = DesignMemory("my_design", memory_dir=mem_dir)
        for i in range(3):
            sid = f"session_{i}"
            mem.record_run_start(f"/tmp/{sid}", session_id=sid)
            mem.record_run_end(sid, stats={"cost_usd": 0.5})
        assert mem.run_count() == 3
        assert mem.data["total_cost_usd"] == pytest.approx(1.5)

    def test_format_summary(self, mem_dir):
        mem = DesignMemory("my_design", memory_dir=mem_dir)
        mem.set_metadata(stage="rtl", pack_path="packs/rtl/rules.json",
                         scenario_dir="scenarios/my_design")
        mem.record_run_start("/tmp/s1", command="run-all-api", session_id="s1")
        mem.record_run_end("s1", stats={"n_right": 3, "n_wrong": 1})
        mem.record_finding("rtl.lint.latch.count", 2, session_id="s1")

        summary = mem.format_summary()
        assert "my_design" in summary
        assert "rtl" in summary
        assert "rtl.lint.latch.count" in summary

    def test_format_history(self, mem_dir):
        mem = DesignMemory("my_design", memory_dir=mem_dir)
        mem.record_run_start("/tmp/s1", command="ready", session_id="s1")
        mem.record_run_end("s1", readiness_verdict="GO")

        history = mem.format_history()
        assert "Run #1" in history
        assert "ready" in history
        assert "GO" in history

    def test_persistence_across_instances(self, mem_dir):
        mem1 = DesignMemory("persist_test", memory_dir=mem_dir)
        mem1.record_run_start("/tmp/s1", session_id="s1")
        mem1.record_run_end("s1", stats={"n_right": 5, "n_wrong": 0})
        mem1.record_finding("rtl.lint.width.count", 1, session_id="s1")
        mem1.save()

        mem2 = DesignMemory("persist_test", memory_dir=mem_dir)
        assert mem2.run_count() == 1
        assert mem2.latest_finding("rtl.lint.width.count") == 1
        assert mem2.last_run()["stats"]["n_right"] == 5

    def test_surprises_recorded(self, mem_dir):
        mem = DesignMemory("my_design", memory_dir=mem_dir)
        mem.record_run_start("/tmp/s1", session_id="s1")
        surprises = [{"measurement_key": "synth.warnings.latch", "measured": 2}]
        mem.record_run_end("s1", surprises=surprises)
        assert mem.last_run()["surprises"] == surprises


class TestHelpers:
    def test_design_id_from_config_name(self):
        assert design_id_from_config({"name": "buggy_demo"}) == "buggy_demo"

    def test_design_id_from_config_dir(self):
        assert design_id_from_config({}, "scenarios/rtl_demo") == "rtl_demo"

    def test_design_id_from_config_fallback(self):
        assert design_id_from_config({}) == "unknown"

    def test_get_or_create(self, mem_dir):
        mem = get_or_create("new_design", memory_dir=mem_dir)
        assert mem.design_id == "new_design"
        assert not mem.has_been_processed()
