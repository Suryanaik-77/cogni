"""
agent.memory
============
Per-design persistent memory for the cogni agent.

Each RTL design gets a JSON file under ``memory/designs/<design_id>.json``
that tracks every run the agent has done on it, what it found, what rules
it learned, and the current flow state. This lets the agent know:

  * what it has done before for a given design
  * where it is in the current pipeline
  * how findings have evolved across runs

The memory directory lives at the repo root (next to runs/, packs/, etc.).
All writes are atomic (temp + rename) so a crash never corrupts the store.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(_REPO_ROOT, "memory", "designs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: str, data: dict) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mem.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class DesignMemory:
    """Persistent per-design memory.

    Usage::

        mem = DesignMemory("buggy_demo")
        mem.record_run_start(session_dir, command="run-all-api")
        ...
        mem.record_run_end(session_id, stats={...})
        mem.save()

    Or load an existing design's memory::

        mem = DesignMemory.load("buggy_demo")
        print(mem.data["runs"])
    """

    def __init__(self, design_id: str, *, memory_dir: str | None = None):
        self.design_id = design_id
        self._dir = memory_dir or MEMORY_DIR
        self._path = os.path.join(self._dir, f"{design_id}.json")
        if os.path.exists(self._path):
            with open(self._path, encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = self._blank(design_id)

    @staticmethod
    def _blank(design_id: str) -> dict:
        now = _now()
        return {
            "design_id": design_id,
            "scenario_dir": None,
            "pack_path": None,
            "stage": None,
            "rtl_files": [],
            "created_at": now,
            "updated_at": now,
            "current_state": {
                "phase": "idle",
                "session_dir": None,
                "last_command": None,
                "last_command_at": None,
                "readiness_verdict": None,
            },
            "runs": [],
            "findings": {},
            "rules_learned": [],
            "total_runs": 0,
            "total_cost_usd": 0.0,
            "best_verdict": None,
            "last_run_at": None,
        }

    @classmethod
    def load(cls, design_id: str, *, memory_dir: str | None = None) -> DesignMemory:
        return cls(design_id, memory_dir=memory_dir)

    @classmethod
    def list_designs(cls, *, memory_dir: str | None = None) -> list[str]:
        d = memory_dir or MEMORY_DIR
        if not os.path.isdir(d):
            return []
        return sorted(
            f.removesuffix(".json")
            for f in os.listdir(d)
            if f.endswith(".json") and not f.startswith(".")
        )

    def save(self) -> str:
        self.data["updated_at"] = _now()
        _atomic_write(self._path, self.data)
        return self._path

    # ------------------------------------------------------------------
    # Metadata setters (called once on first run for a design)
    # ------------------------------------------------------------------

    def set_metadata(self, *, scenario_dir: str | None = None,
                     pack_path: str | None = None,
                     stage: str | None = None,
                     rtl_files: list[str] | None = None) -> None:
        if scenario_dir is not None:
            self.data["scenario_dir"] = scenario_dir
        if pack_path is not None:
            self.data["pack_path"] = pack_path
        if stage is not None:
            self.data["stage"] = stage
        if rtl_files is not None:
            self.data["rtl_files"] = rtl_files

    # ------------------------------------------------------------------
    # Flow state
    # ------------------------------------------------------------------

    def set_phase(self, phase: str, *, session_dir: str | None = None,
                  command: str | None = None) -> None:
        st = self.data["current_state"]
        st["phase"] = phase
        if session_dir is not None:
            st["session_dir"] = session_dir
        if command is not None:
            st["last_command"] = command
            st["last_command_at"] = _now()
        self.save()

    def set_readiness_verdict(self, verdict: str) -> None:
        self.data["current_state"]["readiness_verdict"] = verdict
        self.save()

    @property
    def phase(self) -> str:
        return self.data["current_state"]["phase"]

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def record_run_start(self, session_dir: str, *,
                         command: str = "run-all-api",
                         session_id: str | None = None) -> dict:
        if session_id is None:
            session_id = os.path.basename(os.path.normpath(session_dir))
        run = {
            "session_id": session_id,
            "session_dir": session_dir,
            "command": command,
            "started_at": _now(),
            "completed_at": None,
            "status": "running",
            "stats": {},
            "readiness_verdict": None,
            "blockers_remaining": None,
            "rules_learned": [],
            "fixes_applied": 0,
            "surprises": [],
        }
        self.data["runs"].append(run)
        self.data["total_runs"] = len(self.data["runs"])
        self.data["last_run_at"] = run["started_at"]
        self.set_phase("preparing", session_dir=session_dir, command=command)
        return run

    def record_run_end(self, session_id: str, *,
                       stats: dict | None = None,
                       readiness_verdict: str | None = None,
                       blockers_remaining: int | None = None,
                       rules_learned: list[str] | None = None,
                       fixes_applied: int = 0,
                       surprises: list[dict] | None = None,
                       status: str = "completed") -> None:
        run = self._find_run(session_id)
        if run is None:
            return
        run["completed_at"] = _now()
        run["status"] = status
        if stats:
            run["stats"] = stats
        if readiness_verdict is not None:
            run["readiness_verdict"] = readiness_verdict
            self.data["current_state"]["readiness_verdict"] = readiness_verdict
            if readiness_verdict == "GO":
                self.data["best_verdict"] = "GO"
            elif self.data["best_verdict"] is None:
                self.data["best_verdict"] = readiness_verdict
        if blockers_remaining is not None:
            run["blockers_remaining"] = blockers_remaining
        if rules_learned:
            run["rules_learned"] = rules_learned
            for rid in rules_learned:
                if not any(r["rule_id"] == rid for r in self.data["rules_learned"]):
                    self.data["rules_learned"].append({
                        "rule_id": rid,
                        "learned_at": _now(),
                        "session_id": session_id,
                    })
        if fixes_applied:
            run["fixes_applied"] = fixes_applied
        if surprises:
            run["surprises"] = surprises

        cost = (stats or {}).get("cost_usd", 0)
        if cost:
            self.data["total_cost_usd"] = round(
                self.data.get("total_cost_usd", 0) + cost, 6)

        self.set_phase("idle")

    def _find_run(self, session_id: str) -> dict | None:
        for r in self.data["runs"]:
            if r["session_id"] == session_id:
                return r
        return None

    # ------------------------------------------------------------------
    # Findings tracking
    # ------------------------------------------------------------------

    def record_finding(self, measurement_key: str, measured: Any,
                       *, session_id: str, predicted: Any = None,
                       verdict: str | None = None) -> None:
        findings = self.data["findings"]
        if measurement_key not in findings:
            findings[measurement_key] = {"history": [], "latest": None}
        entry = {
            "session_id": session_id,
            "measured": measured,
            "at": _now(),
        }
        if predicted is not None:
            entry["predicted"] = predicted
        if verdict is not None:
            entry["verdict"] = verdict
        findings[measurement_key]["history"].append(entry)
        findings[measurement_key]["latest"] = measured

    # ------------------------------------------------------------------
    # Fix-attempt tracking
    # ------------------------------------------------------------------

    def record_fix_attempt(self, rule_id: str, target_file: str,
                           outcome: str, *, session_id: str = "",
                           round_label: str = "",
                           detail: str = "") -> None:
        """Track an individual fix attempt (ACCEPTED / REVERTED / FAILED)."""
        attempts = self.data.setdefault("fix_attempts", [])
        attempts.append({
            "rule_id": rule_id,
            "target_file": target_file,
            "outcome": outcome,
            "session_id": session_id,
            "round_label": round_label,
            "detail": detail,
            "at": _now(),
        })
        self.save()

    def failed_fixes(self, *, rule_id: str | None = None) -> list[dict]:
        """Return fix attempts that were REVERTED or FAILED."""
        all_attempts = self.data.get("fix_attempts", [])
        bad = [a for a in all_attempts if a["outcome"] in ("REVERTED", "FAILED")]
        if rule_id:
            bad = [a for a in bad if a["rule_id"] == rule_id]
        return bad

    def accepted_fixes(self) -> list[dict]:
        all_attempts = self.data.get("fix_attempts", [])
        return [a for a in all_attempts if a["outcome"] == "ACCEPTED"]

    def format_fixer_context(self) -> str:
        """Build a context block for the fixer LLM prompt summarising what
        the agent already knows about this design."""
        parts: list[str] = []

        # Prior readiness runs
        ready_runs = [r for r in self.data["runs"]
                      if r.get("command") == "ready"]
        if ready_runs:
            last = ready_runs[-1]
            parts.append(
                f"Prior runs: {len(ready_runs)} readiness check(s).  "
                f"Last verdict: {last.get('readiness_verdict', '?')}  "
                f"Blockers remaining: {last.get('blockers_remaining', '?')}")

        # Finding trends
        trends: list[str] = []
        for key, val in sorted(self.data.get("findings", {}).items()):
            hist = val.get("history", [])
            if len(hist) >= 2:
                vals = [h["measured"] for h in hist[-4:]]
                trends.append(f"  {key}: {' -> '.join(str(v) for v in vals)}")
        if trends:
            parts.append("Finding trends (recent):\n" + "\n".join(trends))

        # Failed fixes
        failed = self.failed_fixes()
        if failed:
            lines = []
            for a in failed[-10:]:
                lines.append(
                    f"  [REVERTED] {a['rule_id']} on {a['target_file']}"
                    + (f": {a['detail']}" if a.get("detail") else ""))
            parts.append(
                "Previously REVERTED fixes (did not reduce warnings):\n"
                + "\n".join(lines)
                + "\nDo NOT repeat the same approach. Try a DIFFERENT "
                "strategy for these rules.")

        # Accepted fixes
        good = self.accepted_fixes()
        if good:
            lines = []
            for a in good[-10:]:
                lines.append(
                    f"  [ACCEPTED] {a['rule_id']} on {a['target_file']}"
                    + (f": {a['detail']}" if a.get("detail") else ""))
            parts.append(
                "Previously ACCEPTED fixes (confirmed by Verilator):\n"
                + "\n".join(lines))

        if not parts:
            return ""
        return ("## Design memory (prior agent knowledge)\n\n"
                + "\n\n".join(parts))

    # ------------------------------------------------------------------
    # Source hash (for skip-if-unchanged)
    # ------------------------------------------------------------------

    def set_source_hash(self, h: str) -> None:
        self.data["source_hash"] = h
        self.save()

    @property
    def source_hash(self) -> str | None:
        return self.data.get("source_hash")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def last_run(self) -> dict | None:
        if not self.data["runs"]:
            return None
        return self.data["runs"][-1]

    def run_count(self) -> int:
        return len(self.data["runs"])

    def has_been_processed(self) -> bool:
        return len(self.data["runs"]) > 0

    def finding_trend(self, measurement_key: str) -> list[dict]:
        f = self.data["findings"].get(measurement_key)
        return f["history"] if f else []

    def latest_finding(self, measurement_key: str) -> Any:
        f = self.data["findings"].get(measurement_key)
        return f["latest"] if f else None

    def all_rules_learned(self) -> list[dict]:
        return list(self.data["rules_learned"])

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def format_summary(self) -> str:
        d = self.data
        lines = []
        lines.append(f"Design: {d['design_id']}")
        lines.append(f"  stage: {d.get('stage', '?')}  |  "
                      f"pack: {d.get('pack_path', '?')}")
        lines.append(f"  scenario: {d.get('scenario_dir', '?')}")
        lines.append(f"  total runs: {d['total_runs']}  |  "
                      f"total cost: ${d.get('total_cost_usd', 0):.4f}")
        lines.append(f"  best verdict: {d.get('best_verdict', 'none')}")

        st = d["current_state"]
        lines.append(f"  current phase: {st['phase']}")
        if st.get("session_dir"):
            lines.append(f"  active session: {st['session_dir']}")
        if st.get("last_command"):
            lines.append(f"  last command: {st['last_command']} "
                          f"at {st.get('last_command_at', '?')}")
        if st.get("readiness_verdict"):
            lines.append(f"  readiness: {st['readiness_verdict']}")

        if d["runs"]:
            lines.append("")
            lines.append("  Run History:")
            for r in d["runs"]:
                status = r.get("status", "?")
                cmd = r.get("command", "?")
                sid = r.get("session_id", "?")
                stats = r.get("stats", {})
                right = stats.get("n_right", "?")
                wrong = stats.get("n_wrong", "?")
                rv = r.get("readiness_verdict")
                rv_str = f"  verdict={rv}" if rv else ""
                lines.append(
                    f"    [{sid}] {cmd} -> {status}  "
                    f"(right={right}, wrong={wrong}{rv_str})")

        if d["findings"]:
            lines.append("")
            lines.append("  Findings (latest):")
            for key, val in sorted(d["findings"].items()):
                latest = val.get("latest")
                n_hist = len(val.get("history", []))
                lines.append(f"    {key} = {latest}  ({n_hist} measurement(s))")

        if d["rules_learned"]:
            lines.append("")
            lines.append(f"  Rules Learned ({len(d['rules_learned'])}):")
            for rl in d["rules_learned"]:
                lines.append(f"    {rl['rule_id']}  (session {rl.get('session_id', '?')})")

        return "\n".join(lines)

    def format_history(self) -> str:
        lines = [f"=== Run History for '{self.design_id}' ==="]
        if not self.data["runs"]:
            lines.append("  (no runs recorded)")
            return "\n".join(lines)

        for i, r in enumerate(self.data["runs"], 1):
            lines.append("")
            lines.append(f"  Run #{i}: {r.get('session_id', '?')}")
            lines.append(f"    command   : {r.get('command', '?')}")
            lines.append(f"    status    : {r.get('status', '?')}")
            lines.append(f"    started   : {r.get('started_at', '?')}")
            lines.append(f"    completed : {r.get('completed_at', '?')}")
            stats = r.get("stats", {})
            if stats:
                lines.append(f"    predicted : {stats.get('n_predicted', '?')}")
                lines.append(f"    right     : {stats.get('n_right', '?')}")
                lines.append(f"    wrong     : {stats.get('n_wrong', '?')}")
                lines.append(f"    refused   : {stats.get('n_refused', '?')}")
                lines.append(f"    kb edits  : {stats.get('n_kb_edits', '?')}")
                if stats.get("cost_usd"):
                    lines.append(f"    cost      : ${stats['cost_usd']:.4f}")
            rv = r.get("readiness_verdict")
            if rv:
                lines.append(f"    verdict   : {rv}")
            bl = r.get("blockers_remaining")
            if bl is not None:
                lines.append(f"    blockers  : {bl}")
            rl = r.get("rules_learned", [])
            if rl:
                lines.append(f"    learned   : {', '.join(rl)}")
            fa = r.get("fixes_applied", 0)
            if fa:
                lines.append(f"    fixes     : {fa}")
            sp = r.get("surprises", [])
            if sp:
                lines.append(f"    surprises : {len(sp)}")
                for s in sp:
                    lines.append(f"      - {s.get('measurement_key', '?')}: "
                                  f"measured={s.get('measured', '?')}")
        return "\n".join(lines)


def get_or_create(design_id: str, *, memory_dir: str | None = None) -> DesignMemory:
    return DesignMemory(design_id, memory_dir=memory_dir)


def design_id_from_config(cfg: dict, scenario_dir: str | None = None) -> str:
    name = cfg.get("name")
    if name:
        return name
    if scenario_dir:
        return os.path.basename(os.path.normpath(scenario_dir))
    return "unknown"
