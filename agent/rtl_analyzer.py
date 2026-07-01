"""
agent.rtl_analyzer
==================

Pure-Python RTL analyzer — no commercial tool dependency.
Uses tree-sitter for proper SystemVerilog AST parsing (like SpyGlass),
then runs analysis on the AST instead of regex.

Architecture (modeled after SpyGlass):
  Phase 1: PARSE     — tree-sitter builds full AST
  Phase 2: ELABORATE — resolve parameters, calculate signal widths
  Phase 3: ANALYZE   — walk AST for structural/semantic checks
  Phase 4: PREDICT   — synthesize findings into synthesis predictions

Rule categories (modeled after SpyGlass 293-rule set):
  W-series : Lint warnings (structural)
  STARC    : STARC methodology rules
  SYNTH    : Synthesis rules
  SIM      : Simulation rules
  CLK      : Clock/reset methodology
  PWR      : Power optimization
  STYLE    : Coding style / naming
  FUNC     : Functional / semantic analysis
"""
from __future__ import annotations

import json as _json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

import tree_sitter_verilog as tsv
import tree_sitter as ts

_SV_LANG = ts.Language(tsv.language())
_PARSER = ts.Parser(_SV_LANG)

_KNOWLEDGE_FILE = os.path.join(os.path.dirname(__file__), "cogni_knowledge.json")

_ACTIVE_SDC: SDCConstraints | None = None
_ACTIVE_UPF: "UPFConstraints | None" = None
_ACTIVE_ELAB: "ElaborationModel | None" = None   # current file's elaboration facts

_DEDUP_STOP = frozenset({
    'the','a','an','is','in','of','to','and','or','that','this','for','on',
    'with','as','by','at','from','be','are','not','it','can','but','when',
    'if','no','has','have','which','may','will','do','does','should','would',
    'could','into','than','then','its','also','detect','check','rule','block',
    'signal','module','logic','always','without','using','where','value',
    'values','causes','cause','ensure','specific','pattern','use','used',
})

def _rule_topic_keys(name: str, description: str) -> set[str]:
    text = (name.replace("LEARNED_", "") + " " + description).lower()
    return set(re.findall(r'[a-z]{3,}', text)) - _DEDUP_STOP

def _is_semantic_duplicate(new_name: str, new_desc: str,
                           existing_rules: list[dict],
                           threshold: float = 0.55) -> str | None:
    new_keys = _rule_topic_keys(new_name, new_desc)
    if len(new_keys) < 3:
        return None
    for existing in existing_rules:
        ex_keys = _rule_topic_keys(existing.get("name", ""),
                                   existing.get("description", ""))
        if len(ex_keys) < 3:
            continue
        intersection = new_keys & ex_keys
        similarity = len(intersection) / min(len(new_keys), len(ex_keys))
        if similarity >= threshold:
            return existing.get("name", "?")
    return None


def _load_knowledge() -> dict:
    if os.path.isfile(_KNOWLEDGE_FILE):
        with open(_KNOWLEDGE_FILE, encoding="utf-8") as f:
            return _json.load(f)
    return {"waivers": [], "learned_rules": [], "review_history": []}


def _save_knowledge(kb: dict) -> None:
    with open(_KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
        _json.dump(kb, f, indent=2)


def _apply_waivers(findings: list[Finding]) -> list[Finding]:
    kb = _load_knowledge()
    waivers = kb.get("waivers", [])
    if not waivers:
        return findings
    out = []
    for f in findings:
        suppressed = False
        for w in waivers:
            if w.get("line") and not w.get("file_pattern"):
                continue
            rule_match = (not w.get("rule") or w["rule"] == f.rule)
            file_match = (not w.get("file_pattern")
                          or re.search(w["file_pattern"], f.file))
            line_match = (not w.get("line") or w["line"] == f.line)
            msg_match = (not w.get("message_pattern")
                         or re.search(w["message_pattern"], f.message))
            if rule_match and file_match and line_match and msg_match:
                suppressed = True
                break
        if not suppressed:
            out.append(f)
    return out


def _run_learned_rules(tree, filename: str,
                       signals: dict) -> list[Finding]:
    kb = _load_knowledge()
    findings = []
    for rule in kb.get("learned_rules", []):
        name = rule.get("name", "LEARNED_unknown")
        pattern = rule.get("pattern", "")
        severity = rule.get("severity", "warning")
        message_tpl = rule.get("message", "")
        check_type = rule.get("check_type", "regex")

        if not pattern:
            continue
        if rule.get("status") == "disabled":
            continue

        if check_type == "regex":
            root = tree.root_node
            src_text = root.text.decode("utf-8", errors="replace")
            for m in re.finditer(pattern, src_text):
                line = src_text[:m.start()].count("\n") + 1
                findings.append(Finding(
                    rule=name, severity=severity, file=filename,
                    line=line,
                    message=message_tpl or f"Learned rule '{name}' matched",
                    synth_impact=rule.get("rationale", ""),
                ))
        elif check_type == "ast_node":
            for node in _find_nodes(tree.root_node, pattern):
                findings.append(Finding(
                    rule=name, severity=severity, file=filename,
                    line=node.start_point[0] + 1,
                    message=message_tpl or f"Learned rule '{name}' matched",
                    synth_impact=rule.get("rationale", ""),
                ))
        elif check_type == "param_expr":
            params = {s.name: s.param_value for s in signals.values()
                      if s.is_param and s.param_value is not None}
            if params:
                try:
                    val = _eval_param_expr(pattern, params)
                    if val is not None and val:
                        findings.append(Finding(
                            rule=name, severity=severity, file=filename,
                            line=1,
                            message=message_tpl or f"Parameter condition '{pattern}' is true",
                            synth_impact=rule.get("rationale", ""),
                        ))
                except Exception:
                    pass
    return findings


def _score_learned_rules(findings: list[Finding],
                         builtin_findings: list[Finding]) -> None:
    """Score learned rules: track hits, detect overlap with built-in checkers,
    update accuracy, auto-disable low-quality rules.

    Called after each analysis run to update cogni_knowledge.json with
    per-rule health metrics.
    """
    kb = _load_knowledge()
    rules = kb.get("learned_rules", [])
    if not rules:
        return

    # Index which lines each built-in rule covers
    builtin_lines: dict[str, set[tuple[str, int]]] = {}
    for f in builtin_findings:
        if not f.rule.startswith("LEARNED_"):
            builtin_lines.setdefault(f.rule, set()).add((f.file, f.line))
    builtin_covered = set()
    for locs in builtin_lines.values():
        builtin_covered |= locs

    # Index waived findings
    waivers = kb.get("waivers", [])
    waiver_keys = set()
    for w in waivers:
        if w.get("rule", "").startswith("LEARNED_"):
            waiver_keys.add((w["rule"], w.get("line", 0)))

    # Learned rule findings this run
    learned_findings = [f for f in findings if f.rule.startswith("LEARNED_")]
    learned_by_rule: dict[str, list[Finding]] = {}
    for f in learned_findings:
        learned_by_rule.setdefault(f.rule, []).append(f)

    changed = False
    for rule in rules:
        name = rule.get("name", "")
        if not name:
            continue

        # Initialize stats if missing
        if "stats" not in rule:
            rule["stats"] = {"hits": 0, "waived": 0, "overlap": 0,
                             "runs": 0, "accuracy": 1.0}
            changed = True

        stats = rule["stats"]
        stats["runs"] = stats.get("runs", 0) + 1

        hits = learned_by_rule.get(name, [])
        if not hits:
            continue

        for h in hits:
            stats["hits"] = stats.get("hits", 0) + 1

            # Was this hit waived (= false positive)?
            if (name, h.line) in waiver_keys:
                stats["waived"] = stats.get("waived", 0) + 1

            # Does a built-in rule already cover this line?
            if (h.file, h.line) in builtin_covered:
                stats["overlap"] = stats.get("overlap", 0) + 1

        # Compute accuracy: (hits - waived - overlap) / hits
        total_hits = stats.get("hits", 0)
        bad = stats.get("waived", 0) + stats.get("overlap", 0)
        if total_hits > 0:
            stats["accuracy"] = round(max(0, total_hits - bad) / total_hits, 3)
        else:
            stats["accuracy"] = 1.0

        # Auto-disable after enough evidence of low quality
        if (stats.get("runs", 0) >= 3
                and total_hits >= 3
                and stats["accuracy"] < 0.3):
            rule["status"] = "disabled"
            rule["disable_reason"] = (
                f"accuracy {stats['accuracy']:.0%} after {total_hits} hits "
                f"({stats.get('waived',0)} waived, {stats.get('overlap',0)} overlap)")

        changed = True

    if changed:
        _save_knowledge(kb)


def get_learned_rule_health() -> list[dict]:
    """Return health stats for all learned rules.

    Each entry: {name, status, accuracy, hits, waived, overlap, runs, severity, pattern_preview}
    Sorted by accuracy ascending (worst first) so the UI can show which rules need attention.
    """
    kb = _load_knowledge()
    health = []
    for rule in kb.get("learned_rules", []):
        name = rule.get("name", "")
        stats = rule.get("stats", {})
        health.append({
            "name": name,
            "status": rule.get("status", "active"),
            "accuracy": stats.get("accuracy", 1.0),
            "hits": stats.get("hits", 0),
            "waived": stats.get("waived", 0),
            "overlap": stats.get("overlap", 0),
            "runs": stats.get("runs", 0),
            "severity": rule.get("severity", "warning"),
            "check_type": rule.get("check_type", "regex"),
            "pattern_preview": (rule.get("pattern", "")[:50]
                                + ("..." if len(rule.get("pattern", "")) > 50
                                   else "")),
            "description": rule.get("description", "")[:100],
            "disable_reason": rule.get("disable_reason", ""),
        })
    health.sort(key=lambda x: (x["status"] != "disabled", x["accuracy"]))
    return health


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    rule: str
    severity: str       # error | warning | info
    file: str
    line: int
    message: str
    synth_impact: str
    confidence: int = 0  # 0=unset, 1-100 percent


@dataclass
class SynthPrediction:
    category: str
    prediction: str
    confidence: str     # high | medium | low
    detail: str


@dataclass
class AnalysisResult:
    files: list[str]
    findings: list[Finding] = field(default_factory=list)
    predictions: list[SynthPrediction] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)


@dataclass
class SignalInfo:
    name: str
    width: int          # -1 if unresolved
    line: int
    direction: str = ""     # input | output | inout | internal
    is_param: bool = False
    param_value: int | None = None


@dataclass
class PortInfo:
    name: str
    direction: str      # input | output | inout
    width: int          # -1 if unresolved
    line: int


@dataclass
class PortConnection:
    port_name: str
    signal_expr: str    # expression connected to the port ("" if unconnected)
    line: int


@dataclass
class InstanceInfo:
    module_name: str
    instance_name: str
    parent_module: str
    param_overrides: dict[str, str]
    port_connections: list[PortConnection]
    line: int
    file: str


@dataclass
class ModuleInfo:
    name: str
    file: str
    line: int
    ports: dict[str, PortInfo] = field(default_factory=dict)
    params: dict[str, int | None] = field(default_factory=dict)
    instances: list[InstanceInfo] = field(default_factory=list)
    signals: dict[str, SignalInfo] = field(default_factory=dict)
    _raw_text: str = ""


@dataclass
class ElaborationResult:
    module_db: dict[str, ModuleInfo] = field(default_factory=dict)
    top_modules: list[str] = field(default_factory=list)
    hierarchy: dict[str, list[InstanceInfo]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    resolved_instances: dict[str, dict] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SDC Constraint Model
# ---------------------------------------------------------------------------

@dataclass
class SDCClock:
    name: str
    period_ns: float
    port: str = ""
    waveform: tuple[float, float] = (0.0, 0.0)

@dataclass
class SDCGeneratedClock:
    name: str
    source: str
    divide_by: int = 1
    multiply_by: int = 1
    port: str = ""

@dataclass
class SDCFalsePath:
    from_spec: str = ""
    to_spec: str = ""
    from_type: str = ""   # "clock", "port", "pin"
    to_type: str = ""

@dataclass
class SDCMulticyclePath:
    multiplier: int = 2
    setup: bool = True
    from_spec: str = ""
    to_spec: str = ""

@dataclass
class SDCClockGroup:
    relation: str = "asynchronous"   # asynchronous | exclusive
    groups: list[list[str]] = field(default_factory=list)

@dataclass
class SDCPortDelay:
    delay_ns: float = 0.0
    clock: str = ""
    ports: list[str] = field(default_factory=list)
    is_max: bool = True

@dataclass
class SDCDelayConstraint:
    delay_ns: float = 0.0
    from_spec: str = ""
    to_spec: str = ""

@dataclass
class SDCConstraints:
    clocks: dict[str, SDCClock] = field(default_factory=dict)
    generated_clocks: dict[str, SDCGeneratedClock] = field(default_factory=dict)
    false_paths: list[SDCFalsePath] = field(default_factory=list)
    multicycle_paths: list[SDCMulticyclePath] = field(default_factory=list)
    clock_groups: list[SDCClockGroup] = field(default_factory=list)
    max_delays: list[SDCDelayConstraint] = field(default_factory=list)
    min_delays: list[SDCDelayConstraint] = field(default_factory=list)
    input_delays: list[SDCPortDelay] = field(default_factory=list)
    output_delays: list[SDCPortDelay] = field(default_factory=list)

    def clock_period(self, clk_name: str) -> float | None:
        if clk_name in self.clocks:
            return self.clocks[clk_name].period_ns
        for gc in self.generated_clocks.values():
            if gc.name == clk_name:
                src_period = self.clock_period(gc.source)
                if src_period is not None:
                    return src_period * gc.divide_by / gc.multiply_by
        for c in self.clocks.values():
            if c.port == clk_name:
                return c.period_ns
        return None

    def is_false_path(self, src_clk: str, dst_clk: str) -> bool:
        for fp in self.false_paths:
            if ((fp.from_spec == src_clk and fp.to_spec == dst_clk) or
                (fp.from_spec == dst_clk and fp.to_spec == src_clk)):
                return True
        return self._in_async_or_exclusive_group(src_clk, dst_clk)

    def _in_async_or_exclusive_group(self, clk_a: str, clk_b: str) -> bool:
        for cg in self.clock_groups:
            ga = gb = -1
            for i, grp in enumerate(cg.groups):
                if clk_a in grp:
                    ga = i
                if clk_b in grp:
                    gb = i
            if ga >= 0 and gb >= 0 and ga != gb:
                return True
        return False

    def is_multicycle(self, src_clk: str, dst_clk: str) -> int | None:
        for mc in self.multicycle_paths:
            if mc.from_spec == src_clk and mc.to_spec == dst_clk:
                return mc.multiplier
        return None

    def port_to_clock(self) -> dict[str, str]:
        mapping = {}
        for c in self.clocks.values():
            if c.port:
                mapping[c.port] = c.name
        for gc in self.generated_clocks.values():
            if gc.port:
                mapping[gc.port] = gc.name
        return mapping


def _parse_sdc_get_arg(text: str) -> list[str]:
    """Extract names from get_ports/get_clocks/get_pins/get_nets/get_cells."""
    names = []
    for m in re.finditer(
            r'\[get_(?:ports|clocks|pins|nets|cells)\s+'
            r'(?:\{([^}]+)\}|(\S+))\s*\]', text):
        raw = m.group(1) or m.group(2)
        names.extend(raw.split())
    if not names:
        parts = text.strip().split()
        if parts:
            names = [parts[-1].strip('{}')]
    return names


def _parse_sdc_flag(text: str, flag: str) -> str | None:
    m = re.search(rf'{re.escape(flag)}\s+(\S+)', text)
    return m.group(1) if m else None


def parse_sdc(text: str) -> SDCConstraints:
    """Parse SDC constraint file text into structured constraints."""
    sdc = SDCConstraints()

    lines: list[str] = []
    buf = ""
    for raw_line in text.splitlines():
        stripped = raw_line.split('#')[0].rstrip()
        if stripped.endswith('\\'):
            buf += stripped[:-1] + " "
        else:
            buf += stripped
            if buf.strip():
                lines.append(buf.strip())
            buf = ""
    if buf.strip():
        lines.append(buf.strip())

    for line in lines:
        if line.startswith('create_clock'):
            name = _parse_sdc_flag(line, '-name') or ""
            period_str = _parse_sdc_flag(line, '-period')
            period = float(period_str) if period_str else 10.0
            ports = _parse_sdc_get_arg(line)
            port = ports[0] if ports else ""
            wave = (0.0, period / 2)
            wm = re.search(r'-waveform\s*\{([^}]+)\}', line)
            if wm:
                vals = wm.group(1).split()
                if len(vals) >= 2:
                    wave = (float(vals[0]), float(vals[1]))
            if not name:
                name = port or "clk"
            sdc.clocks[name] = SDCClock(
                name=name, period_ns=period, port=port, waveform=wave)

        elif line.startswith('create_generated_clock'):
            name = _parse_sdc_flag(line, '-name') or ""
            div_str = _parse_sdc_flag(line, '-divide_by')
            mul_str = _parse_sdc_flag(line, '-multiply_by')
            src_ports = []
            sm = re.search(r'-source\s+(\[get_\w+\s+[^\]]+\]|\S+)', line)
            if sm:
                src_ports = _parse_sdc_get_arg(sm.group(1))
            source = src_ports[0] if src_ports else ""
            out_ports = _parse_sdc_get_arg(line)
            port = ""
            for p in out_ports:
                if p != source:
                    port = p
                    break
            sdc.generated_clocks[name] = SDCGeneratedClock(
                name=name, source=source,
                divide_by=int(div_str) if div_str else 1,
                multiply_by=int(mul_str) if mul_str else 1,
                port=port)

        elif line.startswith('set_false_path'):
            from_args = []
            to_args = []
            fm = re.search(r'-from\s+(\[get_\w+\s+[^\]]+\]|\S+)', line)
            tm = re.search(r'-to\s+(\[get_\w+\s+[^\]]+\]|\S+)', line)
            from_type = to_type = ""
            if fm:
                from_args = _parse_sdc_get_arg(fm.group(1))
                if 'get_clocks' in fm.group(1):
                    from_type = "clock"
                elif 'get_ports' in fm.group(1):
                    from_type = "port"
                else:
                    from_type = "pin"
            if tm:
                to_args = _parse_sdc_get_arg(tm.group(1))
                if 'get_clocks' in tm.group(1):
                    to_type = "clock"
                elif 'get_ports' in tm.group(1):
                    to_type = "port"
                else:
                    to_type = "pin"
            for f_spec in (from_args or [""]):
                for t_spec in (to_args or [""]):
                    sdc.false_paths.append(SDCFalsePath(
                        from_spec=f_spec, to_spec=t_spec,
                        from_type=from_type, to_type=to_type))

        elif line.startswith('set_multicycle_path'):
            parts = line.split()
            mult = 2
            for p in parts[1:]:
                if p.lstrip('-').isdigit():
                    mult = int(p.lstrip('-'))
                    break
            is_setup = '-hold' not in line
            fm = re.search(r'-from\s+(\[get_\w+\s+[^\]]+\]|\S+)', line)
            tm = re.search(r'-to\s+(\[get_\w+\s+[^\]]+\]|\S+)', line)
            from_spec = _parse_sdc_get_arg(fm.group(1))[0] if fm else ""
            to_spec = _parse_sdc_get_arg(tm.group(1))[0] if tm else ""
            sdc.multicycle_paths.append(SDCMulticyclePath(
                multiplier=mult, setup=is_setup,
                from_spec=from_spec, to_spec=to_spec))

        elif line.startswith('set_clock_groups'):
            relation = "asynchronous"
            if '-exclusive' in line or '-physically_exclusive' in line \
                    or '-logically_exclusive' in line:
                relation = "exclusive"
            groups = []
            for gm in re.finditer(r'-group\s+(\[get_clocks\s+[^\]]+\]|\S+)', line):
                names = _parse_sdc_get_arg(gm.group(1))
                if names:
                    groups.append(names)
            if len(groups) >= 2:
                sdc.clock_groups.append(SDCClockGroup(
                    relation=relation, groups=groups))

        elif line.startswith('set_max_delay') or line.startswith('set_min_delay'):
            is_max = line.startswith('set_max_delay')
            parts = line.split()
            delay = 0.0
            for p in parts[1:]:
                try:
                    delay = float(p)
                    break
                except ValueError:
                    continue
            fm = re.search(r'-from\s+(\[get_\w+\s+[^\]]+\]|\S+)', line)
            tm = re.search(r'-to\s+(\[get_\w+\s+[^\]]+\]|\S+)', line)
            from_spec = _parse_sdc_get_arg(fm.group(1))[0] if fm else ""
            to_spec = _parse_sdc_get_arg(tm.group(1))[0] if tm else ""
            dc = SDCDelayConstraint(delay_ns=delay,
                                    from_spec=from_spec, to_spec=to_spec)
            if is_max:
                sdc.max_delays.append(dc)
            else:
                sdc.min_delays.append(dc)

        elif line.startswith('set_input_delay') or line.startswith('set_output_delay'):
            is_input = line.startswith('set_input_delay')
            parts = line.split()
            delay = 0.0
            for p in parts[1:]:
                try:
                    delay = float(p)
                    break
                except ValueError:
                    continue
            clk_m = re.search(r'-clock\s+(\[get_clocks\s+[^\]]+\]|\S+)', line)
            clk_name = _parse_sdc_get_arg(clk_m.group(1))[0] if clk_m else ""
            ports = _parse_sdc_get_arg(line)
            is_max = '-min' not in line
            pd = SDCPortDelay(delay_ns=delay, clock=clk_name,
                              ports=ports, is_max=is_max)
            if is_input:
                sdc.input_delays.append(pd)
            else:
                sdc.output_delays.append(pd)

    return sdc


def parse_sdc_file(path: str) -> SDCConstraints:
    with open(path, encoding='utf-8', errors='replace') as f:
        return parse_sdc(f.read())


# ---------------------------------------------------------------------------
# UPF Power-Intent Model
# ---------------------------------------------------------------------------

@dataclass
class UPFPowerDomain:
    name: str
    elements: list[str] = field(default_factory=list)
    primary_power_net: str = ""
    primary_ground_net: str = ""

@dataclass
class UPFPowerSwitch:
    name: str
    domain: str = ""
    control_signal: str = ""      # RTL net that gates the switch
    output_supply: str = ""
    input_supply: str = ""

@dataclass
class UPFIsolation:
    name: str
    domain: str = ""
    isolation_signal: str = ""    # RTL net that enables the clamp
    clamp_value: str = ""
    applies_to: str = "outputs"   # outputs | inputs | both
    sense: str = "high"

@dataclass
class UPFRetention:
    name: str
    domain: str = ""
    elements: list[str] = field(default_factory=list)   # empty => whole domain
    save_signal: str = ""
    restore_signal: str = ""

@dataclass
class UPFLevelShifter:
    name: str
    domain: str = ""
    applies_to: str = "outputs"
    rule: str = ""                # low_to_high | high_to_low | both

@dataclass
class UPFConstraints:
    top: str = ""
    supply_nets: list[str] = field(default_factory=list)
    supply_voltage: dict[str, float] = field(default_factory=dict)  # net -> ON volts
    domains: dict[str, UPFPowerDomain] = field(default_factory=dict)
    switches: dict[str, UPFPowerSwitch] = field(default_factory=dict)
    isolations: dict[str, UPFIsolation] = field(default_factory=dict)
    retentions: dict[str, UPFRetention] = field(default_factory=dict)
    level_shifters: dict[str, UPFLevelShifter] = field(default_factory=dict)

    def domain_of(self, signal: str) -> str:
        """Return the power domain a signal belongs to (default domain if
        not explicitly listed in any create_power_domain -elements)."""
        for dname, d in self.domains.items():
            if signal in d.elements:
                return dname
        return self.default_domain()

    def default_domain(self) -> str:
        """The domain with no explicit elements is the always-on / default."""
        for dname, d in self.domains.items():
            if not d.elements:
                return dname
        return next(iter(self.domains), "")

    def domain_voltage(self, domain: str) -> float | None:
        d = self.domains.get(domain)
        if not d or not d.primary_power_net:
            return None
        return self.supply_voltage.get(d.primary_power_net)

    def is_switchable(self, domain: str) -> bool:
        d = self.domains.get(domain)
        if not d:
            return False
        return any(sw.domain == domain for sw in self.switches.values())

    def isolation_for(self, domain: str) -> UPFIsolation | None:
        for iso in self.isolations.values():
            if iso.domain == domain:
                return iso
        return None

    def retention_for(self, domain: str) -> UPFRetention | None:
        for ret in self.retentions.values():
            if ret.domain == domain:
                return ret
        return None

    def level_shifter_for(self, domain: str) -> UPFLevelShifter | None:
        for ls in self.level_shifters.values():
            if ls.domain == domain:
                return ls
        return None


def _upf_flag(text: str, flag: str) -> str | None:
    m = re.search(rf'{re.escape(flag)}\s+(\{{[^}}]+\}}|\S+)', text)
    if not m:
        return None
    return m.group(1).strip('{}').strip()


def _upf_elements(text: str) -> list[str]:
    m = re.search(r'-elements\s+\{([^}]*)\}', text)
    if not m:
        return []
    return m.group(1).split()


def parse_upf(text: str) -> UPFConstraints:
    """Parse a UPF (Unified Power Format) power-intent file."""
    upf = UPFConstraints()

    # Join backslash continuations, strip comments
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        s = raw.split('#')[0].rstrip()
        if s.endswith('\\'):
            buf += s[:-1] + " "
        else:
            buf += s
            if buf.strip():
                lines.append(buf.strip())
            buf = ""
    if buf.strip():
        lines.append(buf.strip())

    for line in lines:
        cmd = line.split()[0] if line.split() else ""

        if cmd == 'set_design_top':
            parts = line.split()
            if len(parts) >= 2:
                upf.top = parts[1]

        elif cmd == 'create_supply_net':
            parts = line.split()
            if len(parts) >= 2:
                upf.supply_nets.append(parts[1])

        elif cmd == 'add_port_state':
            parts = line.split()
            net = parts[1] if len(parts) >= 2 else ""
            for sm in re.finditer(r'-state\s+\{(\w+)\s+([^\}]+)\}', line):
                val = sm.group(2).strip()
                try:
                    upf.supply_voltage[net] = float(val)
                    break   # first numeric ON state
                except ValueError:
                    continue

        elif cmd == 'create_power_domain':
            parts = line.split()
            name = parts[1] if len(parts) >= 2 else ""
            if name:
                upf.domains[name] = UPFPowerDomain(
                    name=name, elements=_upf_elements(line))

        elif cmd == 'set_domain_supply_net':
            parts = line.split()
            dom = parts[1] if len(parts) >= 2 else ""
            if dom in upf.domains:
                pp = _upf_flag(line, '-primary_power_net')
                pg = _upf_flag(line, '-primary_ground_net')
                if pp:
                    upf.domains[dom].primary_power_net = pp
                if pg:
                    upf.domains[dom].primary_ground_net = pg

        elif cmd == 'create_power_switch':
            parts = line.split()
            name = parts[1] if len(parts) >= 2 else ""
            dom = _upf_flag(line, '-domain') or ""
            ctrl = ""
            cm = re.search(r'-control_port\s+\{(\w+)\s+(\w+)\}', line)
            if cm:
                ctrl = cm.group(2)
            out_sup = in_sup = ""
            om = re.search(r'-output_supply_port\s+\{(\w+)\s+(\w+)\}', line)
            im = re.search(r'-input_supply_port\s+\{(\w+)\s+(\w+)\}', line)
            if om:
                out_sup = om.group(2)
            if im:
                in_sup = im.group(2)
            if name:
                upf.switches[name] = UPFPowerSwitch(
                    name=name, domain=dom, control_signal=ctrl,
                    output_supply=out_sup, input_supply=in_sup)

        elif cmd == 'set_isolation':
            parts = line.split()
            name = parts[1] if len(parts) >= 2 else ""
            if name:
                upf.isolations[name] = UPFIsolation(
                    name=name,
                    domain=_upf_flag(line, '-domain') or "",
                    isolation_signal=_upf_flag(line, '-isolation_signal') or "",
                    clamp_value=_upf_flag(line, '-clamp_value') or "",
                    applies_to=_upf_flag(line, '-applies_to') or "outputs",
                    sense=_upf_flag(line, '-isolation_sense') or "high")

        elif cmd == 'set_retention':
            parts = line.split()
            name = parts[1] if len(parts) >= 2 else ""
            save = restore = ""
            sm = re.search(r'-save_signal\s+\{(\w+)', line)
            rm = re.search(r'-restore_signal\s+\{(\w+)', line)
            if sm:
                save = sm.group(1)
            if rm:
                restore = rm.group(1)
            if name:
                upf.retentions[name] = UPFRetention(
                    name=name,
                    domain=_upf_flag(line, '-domain') or "",
                    elements=_upf_elements(line),
                    save_signal=save, restore_signal=restore)

        elif cmd == 'set_level_shifter':
            parts = line.split()
            name = parts[1] if len(parts) >= 2 else ""
            if name:
                upf.level_shifters[name] = UPFLevelShifter(
                    name=name,
                    domain=_upf_flag(line, '-domain') or "",
                    applies_to=_upf_flag(line, '-applies_to') or "outputs",
                    rule=_upf_flag(line, '-rule') or "")

    return upf


def parse_upf_file(path: str) -> UPFConstraints:
    with open(path, encoding='utf-8', errors='replace') as f:
        return parse_upf(f.read())


# ---------------------------------------------------------------------------
# Inferred Netlist Model (SpyGlass Phase 4)
# ---------------------------------------------------------------------------

@dataclass
class NetlistCell:
    cell_type: str      # FF, LATCH, MUX, ADDER, MULT, COMP, SHIFT, LUT, BRAM, DSP, BUF
    name: str
    width: int
    line: int
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    properties: dict = field(default_factory=dict)


@dataclass
class NetlistNet:
    name: str
    width: int
    driver: str | None = None       # cell name that drives this net
    readers: list[str] = field(default_factory=list)  # cell names that read this net


class InferredNetlist:
    """Lightweight gate-level netlist inferred from RTL synthesis estimation."""

    def __init__(self):
        self.cells: dict[str, NetlistCell] = {}
        self.nets: dict[str, NetlistNet] = {}

    def add_cell(self, cell: NetlistCell):
        self.cells[cell.name] = cell
        for out_net in cell.outputs:
            net = self.nets.setdefault(out_net, NetlistNet(name=out_net, width=cell.width))
            net.driver = cell.name
        for in_net in cell.inputs:
            net = self.nets.setdefault(in_net, NetlistNet(name=in_net, width=cell.width))
            net.readers.append(cell.name)

    def fanout(self, net_name: str) -> int:
        net = self.nets.get(net_name)
        return len(net.readers) if net else 0

    def cells_of_type(self, cell_type: str) -> list[NetlistCell]:
        return [c for c in self.cells.values() if c.cell_type == cell_type]

    def driver_chain(self, net_name: str, max_depth: int = 20) -> list[NetlistCell]:
        """Trace backwards from a net through its driver chain."""
        chain = []
        visited = set()
        current = net_name
        for _ in range(max_depth):
            net = self.nets.get(current)
            if not net or not net.driver or net.driver in visited:
                break
            visited.add(net.driver)
            cell = self.cells.get(net.driver)
            if not cell:
                break
            chain.append(cell)
            if cell.cell_type == 'FF' or not cell.inputs:
                break
            current = cell.inputs[0]
        return chain

    def comb_depth_to(self, net_name: str, max_depth: int = 50) -> int:
        """Count combinational cells from a net back to the nearest FF/input."""
        depth = 0
        visited = set()
        current = net_name
        for _ in range(max_depth):
            net = self.nets.get(current)
            if not net or not net.driver or net.driver in visited:
                break
            visited.add(net.driver)
            cell = self.cells.get(net.driver)
            if not cell:
                break
            if cell.cell_type in ('FF', 'LATCH', 'BRAM'):
                break
            depth += 1
            if not cell.inputs:
                break
            current = cell.inputs[0]
        return depth

    def high_fanout_nets(self, threshold: int = 8) -> list[tuple[str, int]]:
        result = [(n.name, len(n.readers)) for n in self.nets.values()
                  if len(n.readers) >= threshold]
        result.sort(key=lambda x: -x[1])
        return result

    def mux_chain_depth(self, net_name: str) -> int:
        """Count consecutive MUX cells in the driver chain."""
        depth = 0
        for cell in self.driver_chain(net_name):
            if cell.cell_type == 'MUX':
                depth += 1
            elif cell.cell_type == 'FF':
                break
        return depth

    _CELL_DELAY = {
        'MUX':   0.10,
        'ADDER': 0.15,
        'COMP':  0.15,
        'MULT':  0.50,
        'SHIFT': 0.12,
        'LUT':   0.08,
        'BUF':   0.05,
    }

    def _cell_delay(self, cell: NetlistCell) -> float:
        base = self._CELL_DELAY.get(cell.cell_type, 0.05)
        if cell.cell_type == 'ADDER' and cell.width > 16:
            return 0.30
        if cell.cell_type == 'MULT':
            return 0.50 if cell.width <= 16 else 1.50
        return base

    def timing_path_to(self, net_name: str,
                       max_depth: int = 50) -> list[tuple[NetlistCell, float]]:
        """Trace backwards from net through combinational logic to nearest FF.
        Returns list of (cell, delay_ns) tuples forming the path."""
        path = []
        visited = set()
        current = net_name
        for _ in range(max_depth):
            net = self.nets.get(current)
            if not net or not net.driver or net.driver in visited:
                break
            visited.add(net.driver)
            cell = self.cells.get(net.driver)
            if not cell:
                break
            if cell.cell_type in ('FF', 'LATCH', 'BRAM'):
                path.append((cell, _DELAY_FF_CKQ))
                break
            delay = self._cell_delay(cell)
            path.append((cell, delay))
            if not cell.inputs:
                break
            current = cell.inputs[0]
        path.reverse()
        return path

    def timing_path_delay(self, net_name: str) -> float:
        return sum(d for _, d in self.timing_path_to(net_name))

    def critical_paths(self, top_n: int = 5) -> list[dict]:
        """Find the top-N longest timing paths through combinational logic.

        Endpoints are register D-inputs (setup-checked) and terminal
        combinational outputs (output ports). Each path is traced back to
        its launch point (a register Q or a primary input).
        """
        endpoints = []   # (net_name, endpoint_label, endpoint_kind)
        for cell in self.cells.values():
            if cell.cell_type == 'FF':
                for inp in cell.inputs:
                    net = self.nets.get(inp)
                    if net and net.driver:      # D-pin fed by comb logic
                        endpoints.append((inp, cell.outputs[0]
                                          if cell.outputs else inp, "reg"))
        for net in self.nets.values():
            if (net.driver and not net.readers
                    and self.cells.get(net.driver)
                    and self.cells[net.driver].cell_type
                    not in ('FF', 'LATCH', 'BRAM')):
                endpoints.append((net.name, net.name, "output"))

        paths = []
        for net_name, label, kind in endpoints:
            path_cells = self.timing_path_to(net_name)
            comb_cells = [(c, d) for c, d in path_cells
                          if c.cell_type not in ('FF', 'LATCH', 'BRAM')]
            if not comb_cells:
                continue
            data_delay = sum(d for _, d in path_cells)
            total = data_delay + (_DELAY_FF_SETUP if kind == "reg" else 0.0)
            launched_by = ""
            if path_cells and path_cells[0][0].cell_type in ('FF', 'LATCH', 'BRAM'):
                launched_by = path_cells[0][0].outputs[0] \
                    if path_cells[0][0].outputs else ""
            path_detail = [{
                "cell": c.name, "type": c.cell_type,
                "width": c.width, "line": c.line,
                "delay_ns": round(d, 3),
                "expr": c.properties.get("expr", ""),
            } for c, d in path_cells]
            paths.append({
                "endpoint": label,
                "endpoint_signal": label,
                "endpoint_kind": kind,
                "launched_by": launched_by,
                "data_delay_ns": round(data_delay, 3),
                "setup_ns": _DELAY_FF_SETUP if kind == "reg" else 0.0,
                "total_ns": round(total, 3),
                "depth": len(comb_cells),
                "path": path_detail,
            })
        paths.sort(key=lambda p: -p["total_ns"])
        return paths[:top_n]

    def summary(self) -> dict:
        type_counts = {}
        for c in self.cells.values():
            type_counts[c.cell_type] = type_counts.get(c.cell_type, 0) + 1
        return {
            "total_cells": len(self.cells),
            "total_nets": len(self.nets),
            "cell_types": type_counts,
            "high_fanout_nets": self.high_fanout_nets()[:10],
        }


@dataclass
class DesignContext:
    """Shared analysis state passed to Tier-2 (structural) checkers.
    SpyGlass equivalent: post-elaboration context available to structural rules."""
    synth_data: dict = field(default_factory=dict)
    netlist: InferredNetlist | None = None
    signal_rw_map: tuple | None = None      # (written_map, read_map)
    elab_result: ElaborationResult | None = None
    per_file_synth: dict = field(default_factory=dict)  # filename -> synth_data
    elab: "ElaborationModel | None" = None   # per-file elaboration facts


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _find_nodes(node, target_type: str) -> list:
    results = []
    if node.type == target_type:
        results.append(node)
    for child in node.children:
        results.extend(_find_nodes(child, target_type))
    return results


def _find_nodes_multi(node, types: set[str]) -> list:
    results = []
    if node.type in types:
        results.append(node)
    for child in node.children:
        results.extend(_find_nodes_multi(child, types))
    return results


# ---- Infrastructure 1: Scope-aware case_item finder ----
_SCOPE_BOUNDARY_TYPES = {'case_statement', 'case_statement_ansi'}

def _find_direct_case_items(case_node) -> list:
    """Return case_items that belong directly to this case_statement,
    NOT items from nested case statements inside it."""
    results = []
    def _walk(node, depth):
        if node is not case_node and node.type in _SCOPE_BOUNDARY_TYPES:
            return
        if node.type == 'case_item':
            results.append(node)
        for child in node.children:
            _walk(child, depth + 1)
    _walk(case_node, 0)
    return results


# ---- Infrastructure: Shared utilities for anti-pattern prevention ----

_COMMENT_RE = re.compile(r'//[^\n]*|/\*.*?\*/', re.DOTALL)

def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments from RTL text."""
    return _COMMENT_RE.sub('', text)


def _strip_brackets(text: str) -> str:
    """Remove [...] bracket expressions (index, bit-select, part-select)."""
    return re.sub(r'\[[^\]]*\]', '', text)


def _has_top_level_arith(text: str) -> bool:
    """Check if text contains arithmetic operators outside brackets."""
    stripped = _strip_brackets(text)
    return bool(re.search(r'[+\-*]', stripped))


def _rhs_is_comparison(text: str) -> bool:
    """Check if RHS expression is a comparison (produces 1-bit result)."""
    stripped = _strip_brackets(text)
    return bool(re.search(r'[=!<>]=|[<>](?!=)', stripped))


_ASSIGNMENT_TYPES = ('blocking_assignment', 'nonblocking_assignment',
                     'net_assignment', 'net_decl_assignment')

def _all_assignments(node):
    """Find all assignment nodes including clocking_drive (indexed array writes)."""
    results = []
    for ntype in _ASSIGNMENT_TYPES:
        results.extend(_find_nodes(node, ntype))
    results.extend(_find_nodes(node, 'clocking_drive'))
    return results


def _all_nb_assignments(node):
    """Find nonblocking assignments + clocking_drive."""
    return (_find_nodes(node, 'nonblocking_assignment')
            + _find_nodes(node, 'clocking_drive'))


def _get_lhs_from_any(asgn_node) -> str | None:
    """Get LHS signal name from any assignment type including clocking_drive."""
    if asgn_node.type == 'clocking_drive':
        for cv in _find_nodes(asgn_node, 'clockvar'):
            ids = _find_nodes(cv, 'simple_identifier')
            if ids:
                return ids[0].text.decode()
        return None
    return _get_lhs_signal(asgn_node)


def _func_task_name(decl_node, signals=None) -> str | None:
    """Extract function/task name from declaration, skipping type specifiers."""
    param_names = set()
    if signals:
        param_names = {n for n, s in signals.items() if s.is_param}
    for child in decl_node.named_children:
        if child.type in ('function_body_declaration', 'task_body_declaration'):
            ids = _find_nodes(child, 'simple_identifier')
            for i in ids:
                name = i.text.decode()
                if name not in param_names:
                    return name
    ids = _find_nodes(decl_node, 'simple_identifier')
    for i in ids:
        name = i.text.decode()
        if name not in param_names:
            return name
    return None


def _module_name(mod_node) -> str | None:
    """Extract module name from module_declaration, searching header first."""
    headers = _find_nodes(mod_node, 'module_header')
    if not headers:
        headers = _find_nodes(mod_node, 'module_ansi_header')
    if headers:
        ids = _find_nodes(headers[0], 'simple_identifier')
        if ids:
            return ids[0].text.decode()
    ids = _find_nodes(mod_node, 'simple_identifier')
    return ids[0].text.decode() if ids else None


# ---- Infrastructure 2: Global signal read/write map ----
def _build_signal_rw_map(root) -> tuple[dict, dict]:
    """Build global maps: signal_name -> set of lines where written/read.
    Scans ALL always blocks, continuous assigns, and port connections."""
    written: dict[str, set[int]] = {}
    read: dict[str, set[int]] = {}

    for ntype in _ASSIGNMENT_TYPES:
        for n in _find_nodes(root, ntype):
            sig = _get_lhs_signal(n)
            if sig:
                written.setdefault(sig, set()).add(_node_line(n))
            children = n.named_children
            if len(children) >= 2:
                for ident in _find_nodes(children[-1], 'simple_identifier'):
                    name = ident.text.decode()
                    read.setdefault(name, set()).add(_node_line(n))

    for cd in _find_nodes(root, 'clocking_drive'):
        sig = _get_lhs_from_any(cd)
        if sig:
            written.setdefault(sig, set()).add(_node_line(cd))
        for ident in _find_nodes(cd, 'simple_identifier'):
            name = ident.text.decode()
            if name != sig:
                read.setdefault(name, set()).add(_node_line(cd))

    for cond in _find_nodes(root, 'cond_predicate'):
        for ident in _find_nodes(cond, 'simple_identifier'):
            name = ident.text.decode()
            read.setdefault(name, set()).add(_node_line(cond))

    for ce in _find_nodes(root, 'case_expression'):
        for ident in _find_nodes(ce, 'simple_identifier'):
            name = ident.text.decode()
            read.setdefault(name, set()).add(_node_line(ce))

    for ev in _find_nodes(root, 'event_expression'):
        for ident in _find_nodes(ev, 'simple_identifier'):
            name = ident.text.decode()
            read.setdefault(name, set()).add(_node_line(ev))

    return written, read


# ---- Infrastructure 3: Expression value extractor ----
def _extract_rhs_identifiers(node) -> set[str]:
    """Extract all possible identifier values from an RHS expression,
    walking through ternary (cond ? a : b) and parenthesized exprs."""
    text = _node_text(node).strip()
    if re.fullmatch(r'[a-zA-Z_]\w*', text):
        return {text}
    results = set()
    conds = _find_nodes(node, 'conditional_expression')
    if conds:
        for ce in conds:
            children = ce.named_children
            for child in children:
                results |= _extract_rhs_identifiers(child)
        if results:
            return results
    for ident in _find_nodes(node, 'simple_identifier'):
        results.add(ident.text.decode())
    return results


def _get_identifiers(node) -> list[str]:
    return [n.text.decode() for n in _find_nodes(node, 'simple_identifier')]


def _get_lhs_signal(node) -> str | None:
    for child in node.named_children:
        if child.type in ('variable_lvalue', 'net_lvalue',
                          'hierarchical_identifier'):
            ids = _find_nodes(child, 'simple_identifier')
            if ids:
                return ids[0].text.decode()
    ids = _find_nodes(node, 'simple_identifier')
    return ids[0].text.decode() if ids else None


def _node_line(node) -> int:
    return node.start_point[0] + 1


def _node_text(node) -> str:
    return node.text.decode() if node.text else ''


def _always_type(always_node) -> str:
    """Return 'always_ff' or 'always_comb'.

    Handles both SystemVerilog (always_ff/always_comb) and
    old Verilog (always @(posedge clk) / always @(*)).
    """
    if _find_nodes(always_node, 'always_ff'):
        return 'always_ff'
    if _find_nodes(always_node, 'always_comb'):
        return 'always_comb'
    if _find_nodes(always_node, 'always_latch'):
        return 'always_comb'

    events = _find_nodes(always_node, 'event_expression')
    if not events:
        text = _node_text(always_node)[:80]
        if '@' in text and '*' in text:
            return 'always_comb'
        return 'always_comb'

    for ev in events:
        ev_text = _node_text(ev)
        if 'posedge' in ev_text or 'negedge' in ev_text:
            return 'always_ff'

    return 'always_comb'


def _get_ff_clock(always_node) -> str:
    """Extract the clock signal name from an always_ff sensitivity list."""
    for ev in _find_nodes(always_node, 'event_expression'):
        ev_text = _node_text(ev)
        for m in re.finditer(r'(?:posedge|negedge)\s+(\w+)', ev_text):
            sig = m.group(1)
            if 'rst' not in sig.lower() and 'reset' not in sig.lower():
                return sig
    return ""


def _is_inside(inner, outer) -> bool:
    return (inner.start_byte >= outer.start_byte and
            inner.end_byte <= outer.end_byte)


# ---------------------------------------------------------------------------
# Phase 2: ELABORATE
# ---------------------------------------------------------------------------

_VERILOG_LIT_RE = re.compile(
    r"(\d+)'([sS])?([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)")

def _parse_verilog_literal(expr: str) -> int | None:
    m = _VERILOG_LIT_RE.fullmatch(expr.strip())
    if not m:
        return None
    base_ch = m.group(3).lower()
    digits = m.group(4).replace('_', '')
    if any(c in digits.lower() for c in 'xz?'):
        return None
    base = {'b': 2, 'o': 8, 'd': 10, 'h': 16}[base_ch]
    try:
        return int(digits, base)
    except ValueError:
        return None


def _eval_param_expr(expr: str, params: dict[str, int]) -> int | None:
    expr = expr.strip()
    if not expr:
        return None

    # Plain decimal
    if re.fullmatch(r'\d[\d_]*', expr):
        return int(expr.replace('_', ''))

    # C-style hex: 0xFF, 0x1A (common in parameter defaults)
    cm = re.fullmatch(r'0[xX]([0-9a-fA-F_]+)', expr)
    if cm:
        return int(cm.group(1).replace('_', ''), 16)

    # C-style binary: 0b1010
    cm = re.fullmatch(r'0[bB]([01_]+)', expr)
    if cm:
        return int(cm.group(1).replace('_', ''), 2)

    # Verilog sized literal: 8'd255, 4'hF, 32'b0, etc.
    lit = _parse_verilog_literal(expr)
    if lit is not None:
        return lit

    # Unsized Verilog literal: 'b1, 'h0F, 'd10
    um = re.fullmatch(r"'([bBoOdDhH])([0-9a-fA-F_]+)", expr)
    if um:
        base_ch = um.group(1).lower()
        digits = um.group(2).replace('_', '')
        base = {'b': 2, 'o': 8, 'd': 10, 'h': 16}[base_ch]
        try:
            return int(digits, base)
        except ValueError:
            pass

    # Parameter name lookup
    if expr in params:
        return params[expr]

    # $clog2(...)
    m = re.fullmatch(r'\$clog2\s*\((.+)\)', expr)
    if m:
        inner = _eval_param_expr(m.group(1), params)
        if inner is not None and inner > 0:
            return max(1, math.ceil(math.log2(inner)))
        return None

    # $bits(...)  — common in SV, resolve from params or signals
    m = re.fullmatch(r'\$bits\s*\((.+)\)', expr)
    if m:
        inner_name = m.group(1).strip()
        if inner_name in params:
            return params[inner_name]
        return None

    # Ternary: cond ? a : b — find the top-level ? : at depth 0
    depth = 0
    q_pos = -1
    for i, ch in enumerate(expr):
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        elif ch == '?' and depth == 0 and q_pos < 0:
            q_pos = i
        elif ch == ':' and depth == 0 and q_pos >= 0:
            cond_e = expr[:q_pos]
            true_e = expr[q_pos+1:i]
            false_e = expr[i+1:]
            cond_v = _eval_param_expr(cond_e, params)
            if cond_v is not None:
                branch = true_e if cond_v else false_e
                return _eval_param_expr(branch, params)
            t_v = _eval_param_expr(true_e, params)
            f_v = _eval_param_expr(false_e, params)
            if t_v is not None and f_v is not None and t_v == f_v:
                return t_v
            return None

    # Binary operators — lowest precedence first, right-to-left scan
    _BINARY_OPS = [
        ('||', lambda a, b: int(bool(a) or bool(b))),
        ('&&', lambda a, b: int(bool(a) and bool(b))),
        ('|',  lambda a, b: a | b),
        ('^',  lambda a, b: a ^ b),
        ('&',  lambda a, b: a & b),
        ('==', lambda a, b: int(a == b)),
        ('!=', lambda a, b: int(a != b)),
        ('>=', lambda a, b: int(a >= b)),
        ('<=', lambda a, b: int(a <= b)),
        ('>',  lambda a, b: int(a > b)),
        ('<',  lambda a, b: int(a < b)),
        ('<<<', lambda a, b: a << b),
        ('>>>', lambda a, b: a >> b),
        ('<<', lambda a, b: a << b),
        ('>>', lambda a, b: a >> b),
        ('-',  lambda a, b: a - b),
        ('+',  lambda a, b: a + b),
        ('**', lambda a, b: a ** b if b >= 0 and b < 32 else None),
        ('/',  lambda a, b: a // b if b else None),
        ('%',  lambda a, b: a % b if b else None),
        ('*',  lambda a, b: a * b),
    ]

    for op_str, op_fn in _BINARY_OPS:
        op_len = len(op_str)
        depth = 0
        # Right-to-left scan for lowest precedence
        i = len(expr) - 1
        while i >= op_len:
            if expr[i] in ')]':
                depth += 1
            elif expr[i] in '([':
                depth -= 1
            elif depth == 0 and expr[i-op_len+1:i+1] == op_str:
                # Avoid matching '<<' when looking for '<', etc.
                if op_len == 1 and op_str in '<>':
                    if (i > 0 and expr[i-1] in '<>') or (i+1 < len(expr) and expr[i+1] in '<>='):
                        i -= 1
                        continue
                if op_len == 1 and op_str in '|&^':
                    if (i > 0 and expr[i-1] == op_str) or (i+1 < len(expr) and expr[i+1] == op_str):
                        i -= 1
                        continue
                if op_len == 1 and op_str in '+-':
                    if i == 0:
                        i -= 1
                        continue
                if op_len == 2 and op_str in ('==', '!=', '>=', '<=', '<<', '>>', '&&', '||'):
                    if i >= op_len and expr[i-op_len] in '<>!=':
                        i -= 1
                        continue
                    if i+1 < len(expr) and op_str in ('<<', '>>') and expr[i+1] in '<>':
                        i -= 1
                        continue
                left_str = expr[:i-op_len+1]
                right_str = expr[i+1:]
                if not left_str.strip():
                    i -= 1
                    continue
                left = _eval_param_expr(left_str, params)
                right = _eval_param_expr(right_str, params)
                if left is not None and right is not None:
                    result = op_fn(left, right)
                    return result
                return None
            i -= 1

    # Unary operators
    if expr.startswith('~') and len(expr) > 1:
        inner = _eval_param_expr(expr[1:], params)
        if inner is not None:
            return ~inner & 0xFFFFFFFF
    if expr.startswith('!') and len(expr) > 1:
        inner = _eval_param_expr(expr[1:], params)
        if inner is not None:
            return int(not inner)
    if expr.startswith('-') and len(expr) > 1:
        inner = _eval_param_expr(expr[1:], params)
        if inner is not None:
            return -inner
    if expr.startswith('+') and len(expr) > 1:
        return _eval_param_expr(expr[1:], params)

    # Parenthesized sub-expression
    if expr.startswith('(') and expr.endswith(')'):
        return _eval_param_expr(expr[1:-1], params)

    # Part-select: base[msb:lsb] — mask to the selected bit range.
    m = re.fullmatch(r'(.+)\[(.+):(.+)\]', expr)
    if m:
        base = _eval_param_expr(m.group(1), params)
        msb = _eval_param_expr(m.group(2), params)
        lsb = _eval_param_expr(m.group(3), params)
        if (base is not None and msb is not None and lsb is not None
                and msb >= lsb >= 0):
            width = msb - lsb + 1
            return (base >> lsb) & ((1 << width) - 1)
        return None

    # Bit-select: base[idx]
    m = re.fullmatch(r'(.+)\[(.+)\]', expr)
    if m:
        base = _eval_param_expr(m.group(1), params)
        idx = _eval_param_expr(m.group(2), params)
        if base is not None and idx is not None and idx >= 0:
            return (base >> idx) & 1
        return None

    return None


def _capture_balanced(text: str, start: int) -> str:
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            if depth == 0:
                break
            depth -= 1
        elif ch in ',;\n' and depth == 0:
            break
        i += 1
    return text[start:i].strip()


def _resolve_generate_blocks(text: str, params: dict[str, int]) -> str:
    """Evaluate generate-if/else blocks, keeping active branches.

    Handles:
      - if (COND) begin...end [else begin...end]
      - generate...endgenerate wrappers (stripped)
      - for (genvar i=0; i<N; i++) — unrolled up to 256 iterations
    Only top-level generate constructs are resolved; nested ones are
    left for downstream to handle as-is.
    """
    # Strip generate/endgenerate keywords (SV makes them optional)
    text = re.sub(r'\bgenerate\b', '', text)
    text = re.sub(r'\bendgenerate\b', '', text)

    # Resolve if-generate blocks where condition can be evaluated
    _IF_GEN_RE = re.compile(
        r'\bif\s*\(([^)]+)\)\s*begin\b', re.DOTALL)
    max_passes = 8
    for _ in range(max_passes):
        m = _IF_GEN_RE.search(text)
        if not m:
            break
        cond_val = _eval_param_expr(m.group(1), params)
        if cond_val is None:
            break

        # Find matching end for the if-begin block
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            if text[i:i+5] == 'begin':
                depth += 1
                i += 5
            elif text[i:i+3] == 'end' and (i+3 >= len(text) or not text[i+3].isalnum()):
                depth -= 1
                if depth == 0:
                    break
                i += 3
            else:
                i += 1

        if_body = text[start:i]
        end_pos = i + 3  # skip 'end'

        # Check for else branch
        else_body = ""
        rest_after = text[end_pos:]
        em = re.match(r'\s*else\s+begin\b', rest_after)
        if em:
            es = end_pos + em.end()
            depth = 1
            j = es
            while j < len(text) and depth > 0:
                if text[j:j+5] == 'begin':
                    depth += 1
                    j += 5
                elif text[j:j+3] == 'end' and (j+3 >= len(text) or not text[j+3].isalnum()):
                    depth -= 1
                    if depth == 0:
                        break
                    j += 3
                else:
                    j += 1
            else_body = text[es:j]
            end_pos = j + 3

        chosen = if_body if cond_val else else_body
        nl_count = text[m.start():end_pos].count('\n')
        padding = '\n' * nl_count
        text = text[:m.start()] + chosen + padding + text[end_pos:]

    # Unroll for-generate: for (genvar i = 0; i < N; i = i + 1) begin...end
    _FOR_GEN_RE = re.compile(
        r'\bfor\s*\(\s*(?:genvar\s+)?(\w+)\s*=\s*(\w+)\s*;'
        r'\s*\1\s*(<|<=|>|>=)\s*(\w+)\s*;'
        r'\s*\1\s*=\s*\1\s*([+\-])\s*(\w+)\s*\)\s*begin\b'
        r'(?:\s*:\s*\w+)?', re.DOTALL)
    max_passes = 4
    for _ in range(max_passes):
        m = _FOR_GEN_RE.search(text)
        if not m:
            break
        var = m.group(1)
        init_v = _eval_param_expr(m.group(2), params)
        cmp_op = m.group(3)
        limit_v = _eval_param_expr(m.group(4), params)
        step_op = m.group(5)
        step_v = _eval_param_expr(m.group(6), params)
        if any(v is None for v in (init_v, limit_v, step_v)) or step_v == 0:
            break

        # Find matching end
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            if text[i:i+5] == 'begin':
                depth += 1
                i += 5
            elif text[i:i+3] == 'end' and (i+3 >= len(text) or not text[i+3].isalnum()):
                depth -= 1
                if depth == 0:
                    break
                i += 3
            else:
                i += 1

        loop_body = text[start:i]
        end_pos = i + 3

        cmp_fns = {'<': lambda a, b: a < b, '<=': lambda a, b: a <= b,
                    '>': lambda a, b: a > b, '>=': lambda a, b: a >= b}
        cmp_fn = cmp_fns[cmp_op]
        step_fn = (lambda v, s: v + s) if step_op == '+' else (lambda v, s: v - s)

        expanded = []
        cur = init_v
        for _ in range(256):
            if not cmp_fn(cur, limit_v):
                break
            instance = re.sub(r'\b' + re.escape(var) + r'\b', str(cur), loop_body)
            expanded.append(instance)
            cur = step_fn(cur, step_v)

        nl_count = text[m.start():end_pos].count('\n')
        joined = '\n'.join(expanded)
        padding = '\n' * max(0, nl_count - joined.count('\n'))
        text = text[:m.start()] + joined + padding + text[end_pos:]

    return text


def _elaborate_from_text(text: str, line_offset: int = 0,
                         param_overrides: dict[str, int] | None = None,
                         ) -> dict[str, SignalInfo]:
    text_clean = re.sub(r'//.*?$', '', text, flags=re.MULTILINE)
    text_clean = re.sub(r'/\*.*?\*/', '', text_clean, flags=re.DOTALL)

    signals: dict[str, SignalInfo] = {}
    params: dict[str, int] = {}
    if param_overrides:
        params.update(param_overrides)

    def _line_at(pos: int) -> int:
        return text_clean[:pos].count('\n') + 1 + line_offset

    # Two-pass parameter extraction: first collect all params so forward
    # references resolve, then re-evaluate any that failed first time.
    _PARAM_RE = re.compile(
        r'parameter\s+(?:int\s+(?:unsigned\s+)?)?(\w+)\s*=\s*')
    _LPARAM_RE = re.compile(
        r'localparam\s+(?:\w+\s+)?(\w+)\s*=\s*')

    _overridden = set(param_overrides.keys()) if param_overrides else set()

    pending: list[tuple[str, str, int]] = []
    for pat in (_PARAM_RE, _LPARAM_RE):
        is_param_decl = (pat is _PARAM_RE)
        for m in pat.finditer(text_clean):
            name = m.group(1)
            # For `parameter` (not localparam), keep the override value
            if is_param_decl and name in _overridden:
                signals[name] = SignalInfo(
                    name=name, width=32, line=_line_at(m.start()),
                    is_param=True, param_value=params[name])
                continue
            expr = _capture_balanced(text_clean, m.end())
            val = _eval_param_expr(expr, params)
            if val is not None:
                params[name] = val
                signals[name] = SignalInfo(
                    name=name, width=32, line=_line_at(m.start()),
                    is_param=True, param_value=val)
            else:
                pending.append((name, expr, m.start()))

    # Second pass: resolve forward references
    changed = True
    max_iters = 4
    while pending and changed and max_iters > 0:
        max_iters -= 1
        changed = False
        still_pending = []
        for name, expr, pos in pending:
            val = _eval_param_expr(expr, params)
            if val is not None:
                params[name] = val
                signals[name] = SignalInfo(
                    name=name, width=32, line=_line_at(pos),
                    is_param=True, param_value=val)
                changed = True
            else:
                still_pending.append((name, expr, pos))
        pending = still_pending

    # Resolve generate blocks now that params are known
    text_clean = _resolve_generate_blocks(text_clean, params)

    typedef_widths: dict[str, int] = {}
    for m in re.finditer(
            r'typedef\s+enum\s+logic\s*\[([^\]]+):([^\]]+)\]\s*\{[^}]*\}\s*(\w+)',
            text_clean):
        hi = _eval_param_expr(m.group(1), params)
        lo = _eval_param_expr(m.group(2), params)
        type_name = m.group(3)
        if hi is not None and lo is not None:
            typedef_widths[type_name] = hi - lo + 1

    for m in re.finditer(
            r'typedef\s+enum\s*\{[^}]*\}\s*(\w+)', text_clean):
        type_name = m.group(1)
        if type_name not in typedef_widths:
            typedef_widths[type_name] = 32

    for tname, tw in typedef_widths.items():
        for m in re.finditer(
                r'\b' + re.escape(tname) + r'\s+'
                r'(\w+(?:\s*,\s*\w+)*)\s*;', text_clean):
            for nm in re.finditer(r'(\w+)', m.group(1)):
                sig_name = nm.group(1)
                if sig_name not in signals and sig_name not in params:
                    signals[sig_name] = SignalInfo(
                        name=sig_name, width=tw,
                        line=text_clean[:m.start()].count('\n') + 1,
                        direction="internal")

    dir_kw = {'input', 'output', 'inout'}
    type_kw = {'logic', 'reg', 'wire'}

    for m in re.finditer(
            r'(input|output|inout)\s+(?:logic|reg|wire)?\s*'
            r'(?:\[([^\]]+):([^\]]+)\])?\s*(\w+)', text_clean):
        direction, hi_expr, lo_expr, name = m.groups()
        if name in dir_kw | type_kw | set(params):
            continue
        if hi_expr and lo_expr:
            hi = _eval_param_expr(hi_expr, params)
            lo = _eval_param_expr(lo_expr, params)
            width = (hi - lo + 1) if (hi is not None and lo is not None) else -1
        else:
            width = 1
        signals[name] = SignalInfo(
            name=name, width=width, line=_line_at(m.start()),
            direction=direction)

    for m in re.finditer(
            r'(?:logic|reg|wire)\s*'
            r'(?:\[([^\]]+):([^\]]+)\])?\s*'
            r'(\w+(?:\s*\[[^\]]*\])?'
            r'(?:\s*,\s*\w+(?:\s*\[[^\]]*\])?)*)\s*[;=]', text_clean):
        prefix = text_clean[max(0, m.start()-50):m.start()]
        if re.search(r'(input|output|inout)\s*$', prefix):
            continue
        hi_expr, lo_expr, names_str = m.groups()
        if hi_expr and lo_expr:
            hi = _eval_param_expr(hi_expr, params)
            lo = _eval_param_expr(lo_expr, params)
            width = (hi - lo + 1) if (hi is not None and lo is not None) else -1
        else:
            width = 1
        for nm in re.finditer(r'(\w+)', names_str):
            sig_name = nm.group(1)
            if (sig_name in signals or sig_name in params
                    or sig_name in dir_kw | type_kw
                    or re.fullmatch(r'\d+', sig_name)):
                continue
            signals[sig_name] = SignalInfo(
                name=sig_name, width=width, line=_line_at(m.start()),
                direction="internal")

    return signals


# ===================================================================
# Phase 2a: UNIFIED ELABORATION MODEL
# ===================================================================

class ElaborationModel:
    """Single source of elaboration facts for one file, computed once.

    Rules and the synthesis estimator query this instead of each re-deriving
    constant folding, loop-index detection, and resolved widths. Centralizing
    these facts is what keeps synthesis tools from ever mistaking an
    elaboration-time construct (`8*i`, `DATA_WIDTH/8`, `count == DEPTH[1:0]-1`)
    for runtime hardware.

    Query surface:
      constants          {name: value}  params + localparams (folded)
      loop_vars          {name}         genvars + for-loop indices
      enum_members       {name}         typedef enum labels
      const_of(expr)     -> int|None    fold an expression to a constant
      is_constant(name)  -> bool        compile-time constant symbol
      is_loop_var(name)  -> bool        elaboration-only loop index
      is_elab_expr(ids)  -> bool        operand is index/const arithmetic
      is_index_expr(ids) -> bool        operand is a loop-index expression
      width_of(name)     -> int         resolved signal width (-1 unknown)
    """

    def __init__(self, tree, signals: dict, src_text: str):
        self.tree = tree
        self.signals = signals
        self.src_text = src_text
        clean = re.sub(r'//[^\n]*', '', src_text)
        clean = re.sub(r'/\*.*?\*/', '', clean, flags=re.DOTALL)
        self._clean = clean

        # --- compile-time constants: params + localparams ---
        self.constants: dict[str, int] = {
            s.name: s.param_value for s in signals.values()
            if getattr(s, 'is_param', False) and s.param_value is not None}
        # localparams not already resolved as params
        for lm in re.finditer(r'\blocalparam\s+(?:\w+\s+)?(\w+)\s*=\s*([^;,]+)',
                              clean):
            name = lm.group(1)
            if name in self.constants:
                continue
            v = _eval_param_expr(lm.group(2).strip(), self.constants)
            self.constants[name] = v if v is not None else 0

        # --- enum members (compile-time constants) ---
        self.enum_members: set[str] = set()
        for em in re.finditer(r'typedef\s+enum\s+[^{]*\{([^}]+)\}', clean):
            body = re.sub(r'=\s*[^,]+', '', em.group(1))
            for name in re.findall(r'[A-Za-z_]\w*', body):
                self.enum_members.add(name)

        # --- loop indices: genvars + for-loop init vars (elaboration-only) ---
        self.loop_vars: set[str] = set()
        for gm in re.finditer(r'\bgenvar\s+([\w,\s]+?);', clean):
            self.loop_vars.update(v.strip() for v in gm.group(1).split(',')
                                  if v.strip())
        for fm in re.finditer(
                r'\bfor\s*\(\s*(?:int\s+|integer\s+|genvar\s+)?(\w+)\s*=',
                clean):
            self.loop_vars.add(fm.group(1))

    # ---- fact queries -------------------------------------------------
    def const_of(self, expr) -> int | None:
        """Fold an expression string to a constant, or None if not constant."""
        if expr is None:
            return None
        return _eval_param_expr(str(expr).strip(), self.constants)

    def is_constant(self, name: str) -> bool:
        return name in self.constants or name in self.enum_members

    def is_loop_var(self, name: str) -> bool:
        return name in self.loop_vars

    def _all_symbolic(self, ids) -> bool:
        return all(i in self.constants or i in self.enum_members
                   or i in self.loop_vars for i in ids)

    def is_index_expr(self, ids) -> bool:
        """True when an operand is loop-index arithmetic (`8*i`, `i+1`, `i<N`):
        involves a loop var and nothing but loop vars / constants."""
        return (bool(ids) and any(i in self.loop_vars for i in ids)
                and self._all_symbolic(ids))

    def is_const_expr(self, ids) -> bool:
        """True when every operand identifier is a compile-time constant."""
        if not ids:
            return True
        return all(self.is_constant(i) for i in ids)

    def is_elab_expr(self, ids) -> bool:
        """Elaboration-time (no hardware): pure constant OR loop-index arith."""
        return self.is_const_expr(ids) or self.is_index_expr(ids)

    def width_of(self, name: str) -> int:
        si = self.signals.get(name)
        return si.width if si else -1


def build_elaboration_model(tree, signals, src_text) -> "ElaborationModel":
    return ElaborationModel(tree, signals, src_text)


def _current_elab(tree, signals) -> "ElaborationModel":
    """Elaboration facts for the file being checked. Uses the model built once
    in analyze_design; falls back to building one for standalone rule calls."""
    if _ACTIVE_ELAB is not None:
        return _ACTIVE_ELAB
    return build_elaboration_model(
        tree, signals, tree.root_node.text.decode('utf-8', errors='replace'))


# ===================================================================
# Phase 2b: ELABORATE DESIGN — cross-module hierarchy & connectivity
# ===================================================================

class DesignElaborator:
    """Build full design hierarchy from multiple RTL files.

    SpyGlass Phase 2: resolve modules, instances, port connectivity,
    parameter overrides across the entire design.
    """

    def __init__(self):
        self.module_db: dict[str, ModuleInfo] = {}
        self._trees: dict[str, Any] = {}  # module_name -> parsed tree

    # -- Phase 2a: Parse all files, build module database ---------------

    def parse_files(self, files: list[str]) -> None:
        for filepath in files:
            if not os.path.isfile(filepath):
                continue
            with open(filepath, 'rb') as f:
                src = f.read()
            tree = _PARSER.parse(src)
            text = src.decode('utf-8', errors='replace')
            filename = os.path.basename(filepath)
            for md in _find_nodes(tree.root_node, 'module_declaration'):
                mi = self._extract_module(md, filepath, filename, text)
                if mi:
                    self.module_db[mi.name] = mi
                    self._trees[mi.name] = tree

    def _extract_module(self, md_node, filepath, filename, text) -> ModuleInfo | None:
        mh = _find_nodes(md_node, 'module_header')
        if not mh:
            return None
        ids = _find_nodes(mh[0], 'simple_identifier')
        if not ids:
            return None
        mod_name = _node_text(ids[0])
        line = _node_line(md_node)

        mi = ModuleInfo(name=mod_name, file=filename, line=line)
        mod_text = _node_text(md_node)
        mi._raw_text = mod_text
        mi.signals = _elaborate_from_text(mod_text, line_offset=line - 1)

        # Extract ports from AST
        for pd in _find_nodes(md_node, 'ansi_port_declaration'):
            port = self._extract_port(pd, mi.signals)
            if port:
                mi.ports[port.name] = port

        # Extract parameters from signals (already resolved by _elaborate_from_text)
        for sig_name, sig in mi.signals.items():
            if sig.is_param:
                mi.params[sig_name] = sig.param_value

        # Extract instances
        for inst_node in _find_nodes(md_node, 'module_instantiation'):
            inst = self._extract_instance(inst_node, mod_name, filename)
            if inst:
                mi.instances.append(inst)

        return mi

    def _extract_port(self, pd_node, signals) -> PortInfo | None:
        pid = _find_nodes(pd_node, 'port_identifier')
        if not pid:
            return None
        name = _node_text(pid[0]).strip()

        direction = ""
        for pd_dir in _find_nodes(pd_node, 'port_direction'):
            direction = _node_text(pd_dir).strip()
            break

        width = 1
        si = signals.get(name)
        if si and si.width > 0:
            width = si.width
        else:
            dims = _find_nodes(pd_node, 'packed_dimension')
            if dims:
                dim_text = _node_text(dims[0]).strip()
                m = re.match(r'\[(\d+):(\d+)\]', dim_text)
                if m:
                    width = abs(int(m.group(1)) - int(m.group(2))) + 1

        return PortInfo(name=name, direction=direction, width=width,
                        line=_node_line(pd_node))

    def _extract_instance(self, inst_node, parent_mod, filename) -> InstanceInfo | None:
        ids = _find_nodes(inst_node, 'simple_identifier')
        if not ids:
            return None
        mod_name = _node_text(ids[0]).strip()

        inst_name = ""
        for noi in _find_nodes(inst_node, 'name_of_instance'):
            noi_ids = _find_nodes(noi, 'simple_identifier')
            if noi_ids:
                inst_name = _node_text(noi_ids[0]).strip()
                break
        if not inst_name:
            hi = _find_nodes(inst_node, 'hierarchical_instance')
            if hi:
                hi_ids = _find_nodes(hi[0], 'simple_identifier')
                if hi_ids:
                    inst_name = _node_text(hi_ids[0]).strip()

        # Parameter overrides
        param_overrides = {}
        for npa in _find_nodes(inst_node, 'named_parameter_assignment'):
            p_ids = _find_nodes(npa, 'parameter_identifier')
            if not p_ids:
                continue
            p_name = _node_text(p_ids[0]).strip()
            p_expr = _find_nodes(npa, 'param_expression')
            p_val = _node_text(p_expr[0]).strip() if p_expr else ""
            param_overrides[p_name] = p_val

        # Port connections
        port_conns = []
        for npc in _find_nodes(inst_node, 'named_port_connection'):
            p_ids = _find_nodes(npc, 'port_identifier')
            if not p_ids:
                continue
            p_name = _node_text(p_ids[0]).strip()
            exprs = _find_nodes(npc, 'expression')
            sig_expr = _node_text(exprs[0]).strip() if exprs else ""
            port_conns.append(PortConnection(
                port_name=p_name, signal_expr=sig_expr,
                line=_node_line(npc)))

        return InstanceInfo(
            module_name=mod_name, instance_name=inst_name,
            parent_module=parent_mod, param_overrides=param_overrides,
            port_connections=port_conns, line=_node_line(inst_node),
            file=filename)

    # -- Phase 2b: Build hierarchy, find top modules --------------------

    def _resolve_instance_params(self, inst: InstanceInfo,
                                 parent_params: dict[str, int],
                                 ) -> dict[str, int]:
        """Evaluate parameter overrides in the parent's parameter context."""
        resolved = {}
        for p_name, p_expr in inst.param_overrides.items():
            val = _eval_param_expr(p_expr, parent_params)
            if val is not None:
                resolved[p_name] = val
        return resolved

    def _elaborate_instance(self, child_mi: ModuleInfo,
                            overrides: dict[str, int],
                            ) -> tuple[dict[str, int | None], dict[str, PortInfo]]:
        """Re-elaborate a child module with parameter overrides applied.

        Returns (resolved_params, resolved_ports) reflecting overridden widths.
        """
        if not overrides or not child_mi._raw_text:
            return dict(child_mi.params), dict(child_mi.ports)

        new_signals = _elaborate_from_text(
            child_mi._raw_text, line_offset=child_mi.line - 1,
            param_overrides=overrides)

        new_params = {}
        for sig_name, sig in new_signals.items():
            if sig.is_param:
                new_params[sig_name] = sig.param_value

        new_ports = {}
        for port_name, orig_port in child_mi.ports.items():
            new_sig = new_signals.get(port_name)
            width = new_sig.width if (new_sig and new_sig.width > 0) else orig_port.width
            new_ports[port_name] = PortInfo(
                name=port_name, direction=orig_port.direction,
                width=width, line=orig_port.line)

        return new_params, new_ports

    def elaborate(self) -> ElaborationResult:
        result = ElaborationResult(module_db=dict(self.module_db))

        # Find top modules: modules never instantiated by any other module
        instantiated = set()
        for mi in self.module_db.values():
            for inst in mi.instances:
                instantiated.add(inst.module_name)

        result.top_modules = [
            name for name in self.module_db
            if name not in instantiated
        ]

        # Build hierarchy: parent -> instances
        for mi in self.module_db.values():
            if mi.instances:
                result.hierarchy[mi.name] = list(mi.instances)

        # Propagate parameter overrides and build resolved instance views
        result.resolved_instances = {}
        for mi in self.module_db.values():
            parent_params = {k: v for k, v in mi.params.items()
                            if v is not None}
            for inst in mi.instances:
                if not inst.param_overrides:
                    continue
                child = self.module_db.get(inst.module_name)
                if not child:
                    continue
                overrides = self._resolve_instance_params(inst, parent_params)
                if overrides:
                    r_params, r_ports = self._elaborate_instance(child, overrides)
                    key = f"{mi.name}.{inst.instance_name}"
                    result.resolved_instances[key] = {
                        'params': r_params,
                        'ports': r_ports,
                        'module': inst.module_name,
                    }

        # Run cross-module checks
        result.findings = self._check_cross_module(result)

        return result

    # -- Cross-module checks --------------------------------------------

    def _check_cross_module(self, elab: ElaborationResult) -> list[Finding]:
        findings = []
        findings.extend(self._check_port_mismatch(elab))
        findings.extend(self._check_unconnected_ports(elab))
        findings.extend(self._check_port_direction(elab))
        findings.extend(self._check_port_width(elab))
        findings.extend(self._check_missing_module(elab))
        findings.extend(self._check_cdc(elab))
        findings.extend(self._check_multi_driver_cross(elab))
        return findings

    def _check_port_mismatch(self, elab: ElaborationResult) -> list[Finding]:
        """Port name in instantiation doesn't exist in child module."""
        findings = []
        for mi in elab.module_db.values():
            for inst in mi.instances:
                child = elab.module_db.get(inst.module_name)
                if not child:
                    continue
                for pc in inst.port_connections:
                    if pc.port_name not in child.ports:
                        findings.append(Finding(
                            rule="ELAB_port_not_found", severity="error",
                            file=inst.file, line=pc.line,
                            message=(f"Port '.{pc.port_name}' not found in "
                                     f"module '{inst.module_name}' "
                                     f"(instance '{inst.instance_name}')"),
                            synth_impact="Elaboration failure: port does not exist",
                        ))
        return findings

    def _check_unconnected_ports(self, elab: ElaborationResult) -> list[Finding]:
        """Child module ports left unconnected or missing from instantiation."""
        findings = []
        for mi in elab.module_db.values():
            for inst in mi.instances:
                child = elab.module_db.get(inst.module_name)
                if not child:
                    continue
                connected_ports = set()
                for pc in inst.port_connections:
                    connected_ports.add(pc.port_name)
                    if not pc.signal_expr:
                        port_info = child.ports.get(pc.port_name)
                        direction = port_info.direction if port_info else "?"
                        sev = "error" if direction == "input" else "warning"
                        findings.append(Finding(
                            rule="ELAB_unconnected_port", severity=sev,
                            file=inst.file, line=pc.line,
                            message=(f"Port '.{pc.port_name}' ({direction}) "
                                     f"unconnected on '{inst.instance_name}' "
                                     f"({inst.module_name})"),
                            synth_impact="Unconnected input floats to X; "
                                         "unconnected output wastes logic",
                        ))
                # Ports in child not mentioned at all in the instantiation
                for port_name, port_info in child.ports.items():
                    if port_name not in connected_ports:
                        sev = "error" if port_info.direction == "input" else "warning"
                        findings.append(Finding(
                            rule="ELAB_missing_port", severity=sev,
                            file=inst.file, line=inst.line,
                            message=(f"Port '.{port_name}' ({port_info.direction}) "
                                     f"of '{inst.module_name}' not connected "
                                     f"in instance '{inst.instance_name}'"),
                            synth_impact="Missing port connection: "
                                         "input floats, output lost",
                        ))
        return findings

    def _check_port_direction(self, elab: ElaborationResult) -> list[Finding]:
        """Output-to-output or input-to-input connections across hierarchy."""
        findings = []
        for mi in elab.module_db.values():
            for inst in mi.instances:
                child = elab.module_db.get(inst.module_name)
                if not child:
                    continue
                for pc in inst.port_connections:
                    if not pc.signal_expr:
                        continue
                    child_port = child.ports.get(pc.port_name)
                    if not child_port:
                        continue
                    parent_sig = mi.signals.get(pc.signal_expr)
                    if not parent_sig or not parent_sig.direction:
                        continue
                    # output port connected to output signal (both driving)
                    if (child_port.direction == "output" and
                            parent_sig.direction == "output"):
                        pass  # valid: child drives parent output
                    elif (child_port.direction == "input" and
                          parent_sig.direction == "input"):
                        pass  # valid: parent input feeds child input
                    elif (child_port.direction == "input" and
                          parent_sig.direction == "output"):
                        findings.append(Finding(
                            rule="ELAB_direction_mismatch", severity="error",
                            file=inst.file, line=pc.line,
                            message=(f"Output signal '{pc.signal_expr}' "
                                     f"connected to input port "
                                     f"'.{pc.port_name}' of "
                                     f"'{inst.instance_name}' — "
                                     f"two drivers on same net"),
                            synth_impact="Multi-driven net: bus contention",
                        ))
        return findings

    def _check_port_width(self, elab: ElaborationResult) -> list[Finding]:
        """Width mismatch between parent signal and child port.

        Uses resolved instance ports when parameter overrides are present.
        """
        findings = []
        for mi in elab.module_db.values():
            for inst in mi.instances:
                child = elab.module_db.get(inst.module_name)
                if not child:
                    continue
                # Use resolved ports if param overrides were propagated
                inst_key = f"{mi.name}.{inst.instance_name}"
                resolved = elab.resolved_instances.get(inst_key)
                child_ports = resolved['ports'] if resolved else child.ports

                for pc in inst.port_connections:
                    if not pc.signal_expr:
                        continue
                    child_port = child_ports.get(pc.port_name)
                    if not child_port or child_port.width <= 0:
                        continue
                    if '[' in pc.signal_expr:
                        continue
                    parent_sig = mi.signals.get(pc.signal_expr)
                    if not parent_sig or parent_sig.width <= 0:
                        continue
                    if parent_sig.width != child_port.width:
                        override_note = ""
                        if resolved:
                            r_params = resolved.get('params', {})
                            changed = {k: v for k, v in r_params.items()
                                       if child.params.get(k) != v}
                            if changed:
                                override_note = (
                                    f" (with overrides: "
                                    f"{', '.join(f'{k}={v}' for k, v in changed.items())})")
                        findings.append(Finding(
                            rule="ELAB_port_width_mismatch", severity="warning",
                            file=inst.file, line=pc.line,
                            message=(f"Width mismatch: signal "
                                     f"'{pc.signal_expr}'({parent_sig.width}b) "
                                     f"connected to port "
                                     f"'.{pc.port_name}'({child_port.width}b) "
                                     f"of '{inst.instance_name}' "
                                     f"({inst.module_name}){override_note}"),
                            synth_impact="Implicit truncation or zero-extension",
                        ))
        return findings

    def _check_missing_module(self, elab: ElaborationResult) -> list[Finding]:
        """Module instantiated but definition not found in design."""
        findings = []
        for mi in elab.module_db.values():
            for inst in mi.instances:
                if inst.module_name not in elab.module_db:
                    findings.append(Finding(
                        rule="ELAB_missing_module", severity="error",
                        file=inst.file, line=inst.line,
                        message=(f"Module '{inst.module_name}' instantiated "
                                 f"as '{inst.instance_name}' but not found "
                                 f"in design files"),
                        synth_impact="Elaboration failure: unresolved module",
                    ))
        return findings

    def _check_cdc(self, elab: ElaborationResult) -> list[Finding]:
        """Clock domain crossing: signals driven by one clock read under another."""
        findings = []
        # Build clock domain map: for each module's instances, track
        # which clock port they use
        for mi in elab.module_db.values():
            clock_domains: dict[str, list[str]] = {}
            for inst in mi.instances:
                child = elab.module_db.get(inst.module_name)
                if not child:
                    continue
                for pc in inst.port_connections:
                    if not pc.signal_expr:
                        continue
                    child_port = child.ports.get(pc.port_name)
                    if not child_port:
                        continue
                    # Detect clock ports by name convention
                    pn = pc.port_name.lower()
                    if any(ck in pn for ck in ('clk', 'clock', 'pclk',
                                               'hclk', 'aclk', 'fclk')):
                        if child_port.direction == 'input':
                            clock_domains.setdefault(
                                pc.signal_expr, []).append(
                                inst.instance_name)

            # Find output signals from one clock domain used as input
            # in another clock domain
            if len(clock_domains) < 2:
                continue

            # Map instance -> clock signal
            inst_clock = {}
            for clk_sig, inst_names in clock_domains.items():
                for iname in inst_names:
                    inst_clock[iname] = clk_sig

            # Check data signals crossing between instances on different clocks
            sig_drivers: dict[str, tuple[str, str]] = {}
            for inst in mi.instances:
                child = elab.module_db.get(inst.module_name)
                if not child:
                    continue
                clk = inst_clock.get(inst.instance_name)
                if not clk:
                    continue
                for pc in inst.port_connections:
                    if not pc.signal_expr:
                        continue
                    child_port = child.ports.get(pc.port_name)
                    if not child_port:
                        continue
                    if child_port.direction == 'output':
                        sig_drivers[pc.signal_expr] = (
                            inst.instance_name, clk)
                    elif child_port.direction == 'input':
                        pn = pc.port_name.lower()
                        if any(ck in pn for ck in ('clk', 'clock', 'rst',
                                                   'reset')):
                            continue
                        driver = sig_drivers.get(pc.signal_expr)
                        if driver and driver[1] != clk:
                            findings.append(Finding(
                                rule="ELAB_cdc_crossing",
                                severity="error",
                                file=inst.file, line=pc.line,
                                message=(
                                    f"CDC: signal '{pc.signal_expr}' "
                                    f"driven by '{driver[0]}' "
                                    f"(clock '{driver[1]}') read by "
                                    f"'{inst.instance_name}' "
                                    f"(clock '{clk}') — needs synchronizer"),
                                synth_impact="Metastability risk: "
                                             "signal crosses clock domains "
                                             "without synchronization",
                            ))
        return findings

    def _check_multi_driver_cross(self, elab: ElaborationResult) -> list[Finding]:
        """Signal driven by multiple child instance outputs."""
        findings = []
        for mi in elab.module_db.values():
            sig_drivers: dict[str, list[tuple[str, str, int]]] = {}
            for inst in mi.instances:
                child = elab.module_db.get(inst.module_name)
                if not child:
                    continue
                for pc in inst.port_connections:
                    if not pc.signal_expr:
                        continue
                    child_port = child.ports.get(pc.port_name)
                    if not child_port:
                        continue
                    if child_port.direction == 'output':
                        sig_drivers.setdefault(
                            pc.signal_expr, []).append(
                            (inst.instance_name, inst.file, pc.line))

            for sig, drivers in sig_drivers.items():
                if len(drivers) > 1:
                    driver_names = [d[0] for d in drivers]
                    findings.append(Finding(
                        rule="ELAB_multi_driver", severity="error",
                        file=drivers[0][1], line=drivers[0][2],
                        message=(f"Signal '{sig}' driven by multiple "
                                 f"instances: {', '.join(driver_names)}"),
                        synth_impact="Multi-driven net: bus contention or X",
                    ))
        return findings

    def format_hierarchy(self, elab: ElaborationResult) -> str:
        """Pretty-print the design hierarchy tree."""
        lines = []
        lines.append("Design Hierarchy:")
        lines.append(f"  Modules: {len(elab.module_db)}")
        lines.append(f"  Top modules: {', '.join(elab.top_modules)}")
        lines.append("")

        def _print_tree(mod_name, indent=0):
            mi = elab.module_db.get(mod_name)
            if not mi:
                lines.append(f"{'  ' * indent}{mod_name} [NOT FOUND]")
                return
            port_count = len(mi.ports)
            lines.append(
                f"{'  ' * indent}{mod_name} "
                f"({port_count} ports, {mi.file})")
            for inst in mi.instances:
                lines.append(
                    f"{'  ' * (indent + 1)}{inst.instance_name}: "
                    f"{inst.module_name}")
                if inst.module_name in elab.module_db:
                    _print_tree(inst.module_name, indent + 2)

        for top in elab.top_modules:
            _print_tree(top)
        return '\n'.join(lines)


# ===================================================================
# Phase 3: ANALYZE — checkers organized by SpyGlass category
# ===================================================================

# -------------------------------------------------------------------
# Category 1: W-SERIES LINT (structural lint)
# -------------------------------------------------------------------

def W336_blocking_in_sequential(tree, file, signals):
    """W336: Blocking assignment in sequential block."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ba in _find_nodes(always, 'blocking_assignment'):
            sig = _get_lhs_signal(ba)
            if not sig:
                continue
            findings.append(Finding(
                rule="W336_blocking_in_seq", severity="error",
                file=file, line=_node_line(ba),
                message=f"Blocking '=' on '{sig}' in sequential block",
                synth_impact="Sim/synth mismatch: race in gate-level sim",
            ))
    return findings


def W505_mixed_assignments(tree, file, signals):
    """W505: Signal uses both blocking and non-blocking across different blocks.

    Suppressed when both = and <= appear in the same always_ff block,
    since W336 already covers that case (SpyGlass behavior).
    """
    findings = []
    # Map signal -> set of always blocks containing each assignment type
    blocking_blocks: dict[str, set[int]] = {}
    nonblocking_blocks: dict[str, set[int]] = {}
    blocking_lines: dict[str, int] = {}

    for always in _find_nodes(tree.root_node, 'always_construct'):
        blk_id = id(always)
        for ba in _find_nodes(always, 'blocking_assignment'):
            sig = _get_lhs_signal(ba)
            if sig:
                blocking_blocks.setdefault(sig, set()).add(blk_id)
                if sig not in blocking_lines:
                    blocking_lines[sig] = _node_line(ba)
        for nba in _find_nodes(always, 'nonblocking_assignment'):
            sig = _get_lhs_signal(nba)
            if sig:
                nonblocking_blocks.setdefault(sig, set()).add(blk_id)

    for sig in set(blocking_blocks) & set(nonblocking_blocks):
        # Suppress if both types are in the same always block (W336 covers it)
        if blocking_blocks[sig] == nonblocking_blocks[sig]:
            continue
        findings.append(Finding(
            rule="W505_mixed_assign", severity="error",
            file=file, line=blocking_lines[sig],
            message=f"'{sig}' uses both = and <= assignments across different blocks",
            synth_impact="Sim/synth mismatch: undefined scheduling",
        ))
    return findings


def _collect_unconditional_assigns(block, result_set):
    """Add signals with unconditional blocking assignments in a seq_block to result_set."""
    for child in block.named_children:
        if child.type == 'statement_or_null':
            inner = child.named_children[0] if child.named_children else None
            if inner and inner.type == 'statement':
                si = inner.named_children[0] if inner.named_children else None
                if si and si.type == 'statement_item':
                    ba_nodes = _find_nodes(si, 'blocking_assignment')
                    if ba_nodes and not _find_nodes(si, 'conditional_statement') \
                            and not _find_nodes(si, 'case_statement'):
                        for ba in ba_nodes:
                            s = _get_lhs_signal(ba)
                            if s:
                                result_set.add(s)


def W263_case_no_default(tree, file, signals):
    """W263: Case without default in combinational block."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue
        for cs in _find_nodes(always, 'case_statement'):
            items = _find_direct_case_items(cs)
            if not any('default' in _node_text(i)[:20] for i in items):
                findings.append(Finding(
                    rule="W263_case_no_default", severity="error",
                    file=file, line=_node_line(cs),
                    message="case without default in comb block — latch inferred",
                    synth_impact="Synthesis inserts latch for unassigned arms",
                ))
    return findings


def W402_latch_inference(tree, file, signals):
    """W402/InferLatch: Signal not assigned on all paths in comb block."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue

        all_ba = _find_nodes(always, 'blocking_assignment')
        assigned_sigs = set()
        for ba in all_ba:
            sig = _get_lhs_signal(ba)
            if sig:
                assigned_sigs.add(sig)

        cases = _find_nodes(always, 'case_statement')
        all_cases_complete = all(
            any('default' in _node_text(i)[:20]
                for i in _find_direct_case_items(c))
            for c in cases
        ) if cases else True

        cond_stmts = _find_nodes(always, 'conditional_statement')
        all_ifs_complete = True
        for cs in cond_stmts:
            text = _node_text(cs)
            if '\nelse' not in text and ' else' not in text:
                all_ifs_complete = False
                break

        if all_cases_complete and all_ifs_complete:
            continue

        seq_block = _find_nodes(always, 'seq_block')
        default_assigned = set()
        if seq_block:
            block = seq_block[0]
            _collect_unconditional_assigns(block, default_assigned)
            for child in block.named_children:
                if child.type == 'statement_or_null':
                    inner = child.named_children[0] if child.named_children else None
                    if inner and inner.type == 'statement':
                        si = inner.named_children[0] if inner.named_children else None
                        if si and si.type == 'statement_item':
                            for loop in _find_nodes(si, 'loop_statement'):
                                for lc in loop.named_children:
                                    if lc.type == 'statement_or_null':
                                        ls = lc.named_children[0] if lc.named_children else None
                                        if ls and ls.type == 'statement':
                                            lsi = ls.named_children[0] if ls.named_children else None
                                            if lsi and lsi.type == 'statement_item':
                                                lb = _find_nodes(lsi, 'seq_block')
                                                if lb:
                                                    _collect_unconditional_assigns(lb[0], default_assigned)

        for sig in assigned_sigs - default_assigned:
            for ba in all_ba:
                if _get_lhs_signal(ba) == sig:
                    ba_line = _node_line(ba)
                    always_line = _node_line(always)
                    evidence = []
                    if sig not in default_assigned:
                        evidence.append("Missing default assignment for '%s'" % sig)
                    if not all_ifs_complete:
                        evidence.append("Missing else branch in if/else")
                    if not all_cases_complete:
                        evidence.append("Missing default in case statement")
                    ev_text = " | ".join(evidence)

                    is_output = sig in {n for n, s in signals.items()
                                        if s.direction == 'output'}
                    sig_w = signals[sig].width if sig in signals else 1
                    fix = "%s = %s'b0; // default before conditional" % (
                        sig, sig_w if sig_w > 0 else 1)

                    findings.append(Finding(
                        rule="W402_latch_inferred", severity="warning",
                        file=file, line=ba_line,
                        message=(
                            "Incomplete assignment in always_comb — latch "
                            "inferred for '%s' (L%d). Evidence: %s. "
                            "Impact: 1 latch, +%d gate area, timing hazard%s. "
                            "Fix (L%d): add '%s' before the if/case"
                            % (sig, ba_line, ev_text,
                               _GATE_LATCH,
                               ", glitch-prone output" if is_output else "",
                               always_line + 1, fix)),
                        synth_impact=(
                            "Latch: %d gate overhead, timing hazard. "
                            "Fix: %s" % (_GATE_LATCH, fix)),
                    ))
                    break
    return findings


def W164_width_mismatch(tree, file, signals):
    """W164: Width mismatch in comparison or assignment."""
    findings = []
    seen = set()
    elab = _current_elab(tree, signals)

    for binop in _find_nodes_multi(tree.root_node,
                                    {'binary_expression', 'expression'}):
        text = _node_text(binop)
        if '==' not in text and '!=' not in text:
            continue
        for m in re.finditer(r'(\w+)\s*[!=]=\s*(\w+)', text):
            lhs, rhs = m.group(1), m.group(2)
            pair = tuple(sorted((lhs, rhs)))
            if pair in seen:
                continue
            # Skip when either operand has a part-select (narrows the width)
            after_lhs = text[m.start(1)+len(lhs):m.start(1)+len(lhs)+10].lstrip()
            after_rhs = text[m.end():m.end()+10].lstrip()
            if after_lhs.startswith('[') or after_rhs.startswith('['):
                continue
            # A compile-time constant operand (DEPTH, DEPTH-1, a literal) is
            # sized to context by synthesis — not a real width mismatch.
            if elab.is_constant(lhs) or elab.is_constant(rhs):
                continue
            li, ri = signals.get(lhs), signals.get(rhs)
            if not li or not ri or li.width <= 0 or ri.width <= 0:
                continue
            if li.width != ri.width and abs(li.width - ri.width) > 1:
                seen.add(pair)
                findings.append(Finding(
                    rule="W164_width_mismatch", severity="warning",
                    file=file, line=_node_line(binop),
                    message=f"Width mismatch: '{lhs}'({li.width}b) vs '{rhs}'({ri.width}b)",
                    synth_impact="Implicit extend/truncate may change behavior",
                ))

    for atype in ('blocking_assignment', 'nonblocking_assignment'):
        for asgn in _find_nodes(tree.root_node, atype):
            lhs = _get_lhs_signal(asgn)
            if not lhs:
                continue
            li = signals.get(lhs)
            if not li or li.width <= 0:
                continue
            # Handle operator_assignment wrapping
            target = asgn
            oa = _find_nodes(asgn, 'operator_assignment')
            if oa:
                target = oa[0]
            children = target.named_children
            if len(children) < 2:
                continue
            rhs_text = _node_text(children[-1]).strip()
            if _rhs_is_comparison(rhs_text):
                continue
            rhs_ids = _get_identifiers(children[-1])
            if len(rhs_ids) == 1:
                rhs = rhs_ids[0]
                if re.search(rf'{re.escape(rhs)}\s*\[', rhs_text):
                    continue
                ri = signals.get(rhs)
                if not ri or ri.width <= 0:
                    continue
                pair = tuple(sorted((lhs, rhs)))
                if pair in seen:
                    continue
                if li.width != ri.width and abs(li.width - ri.width) > 1:
                    seen.add(pair)
                    findings.append(Finding(
                        rule="W164_width_mismatch", severity="warning",
                        file=file, line=_node_line(asgn),
                        message=f"Width mismatch: '{rhs}'({ri.width}b) assigned to '{lhs}'({li.width}b)",
                        synth_impact="Truncation or zero-extend may lose data",
                    ))
    return findings


def W528_unused_signal(tree, file, signals):
    """W528: Signal written but never read."""
    findings = []
    write_map, read_map = _build_signal_rw_map(tree.root_node)

    for sig_name, sig_info in signals.items():
        if sig_info.is_param or sig_info.direction in ('input', 'inout', 'output'):
            continue
        if sig_name not in write_map:
            continue
        if sig_name in read_map:
            continue
        findings.append(Finding(
            rule="W528_unused_signal", severity="warning",
            file=file, line=sig_info.line,
            message=f"'{sig_name}' written but never read",
            synth_impact="Dead logic: synthesis removes signal and driver",
        ))
    return findings


def W120_unread_variable(tree, file, signals):
    """W120: Variable declared but never used anywhere."""
    findings = []
    all_text = _strip_comments(_node_text(tree.root_node))
    for sig_name, sig_info in signals.items():
        if sig_info.is_param or sig_info.direction in ('input', 'output', 'inout'):
            continue
        count = len(re.findall(rf'\b{re.escape(sig_name)}\b', all_text))
        if count <= 1:
            findings.append(Finding(
                rule="W120_unread_var", severity="warning",
                file=file, line=sig_info.line,
                message=f"Variable '{sig_name}' declared but never used",
                synth_impact="Dead declaration: no synthesis impact but confusing",
            ))
    return findings


def W240_input_not_read(tree, file, signals):
    """W240: Input port declared but never read in the module."""
    findings = []
    all_text = _strip_comments(_node_text(tree.root_node))
    for sig_name, sig_info in signals.items():
        if sig_info.direction != 'input':
            continue
        count = len(re.findall(rf'\b{re.escape(sig_name)}\b', all_text))
        if count <= 1:
            findings.append(Finding(
                rule="W240_input_not_read", severity="warning",
                file=file, line=sig_info.line,
                message=f"Input port '{sig_name}' declared but never read",
                synth_impact="Unused input: wasted routing, possible connection error",
            ))
    return findings


def W287_output_not_driven(tree, file, signals):
    """W287: Output port never assigned."""
    findings = []
    all_text = _strip_comments(_node_text(tree.root_node))
    driven = set()
    for ntype in ('blocking_assignment', 'nonblocking_assignment',
                  'net_assignment', 'net_decl_assignment'):
        for n in _find_nodes(tree.root_node, ntype):
            sig = _get_lhs_signal(n)
            if sig:
                driven.add(sig)
    for cd in _find_nodes(tree.root_node, 'clocking_drive'):
        sig = _get_lhs_from_any(cd)
        if sig:
            driven.add(sig)
    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        for na in _find_nodes(ca, 'net_assignment'):
            sig = _get_lhs_signal(na)
            if sig:
                driven.add(sig)
    # Fallback: scan text for assign statements inside ERROR nodes
    for m in re.finditer(r'\bassign\s+(\w+)\s*=', all_text):
        driven.add(m.group(1))

    for sig_name, sig_info in signals.items():
        if sig_info.direction != 'output':
            continue
        if sig_name not in driven:
            findings.append(Finding(
                rule="W287_output_not_driven", severity="error",
                file=file, line=sig_info.line,
                message=f"Output port '{sig_name}' is never driven",
                synth_impact="Undriven output: will be X or Z at synthesis",
            ))
    return findings


def W289_assign_to_input(tree, file, signals):
    """W289: Assignment to input port (illegal)."""
    findings = []
    input_sigs = {n for n, s in signals.items() if s.direction == 'input'}
    for ntype in ('blocking_assignment', 'nonblocking_assignment'):
        for n in _find_nodes(tree.root_node, ntype):
            sig = _get_lhs_signal(n)
            if sig and sig in input_sigs:
                findings.append(Finding(
                    rule="W289_assign_to_input", severity="error",
                    file=file, line=_node_line(n),
                    message=f"Assignment to input port '{sig}'",
                    synth_impact="Illegal: input ports cannot be driven internally",
                ))
    return findings


def W391_no_output(tree, file, signals):
    """W391: Module has no output ports."""
    findings = []
    has_output = any(s.direction == 'output' for s in signals.values())
    if not has_output and signals:
        findings.append(Finding(
            rule="W391_no_output", severity="warning",
            file=file, line=1,
            message="Module has no output ports",
            synth_impact="Module with no outputs may be optimized away entirely",
        ))
    return findings


def W415_sensitivity_list(tree, file, signals):
    """W415: Signal missing from sensitivity list (Verilog always @(...))."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        # Only check old Verilog always blocks with explicit sensitivity
        if _find_nodes(always, 'always_comb') or _find_nodes(always, 'always_ff'):
            continue
        events = _find_nodes(always, 'event_expression')
        if not events:
            continue

        ev_text = ' '.join(_node_text(e) for e in events)
        if '*' in ev_text:
            continue
        if 'posedge' in ev_text or 'negedge' in ev_text:
            continue

        sens_sigs = set()
        for e in events:
            sens_sigs.update(_get_identifiers(e))

        # Find all signals read in the always block body
        body = always
        read_sigs = set()
        for ba in _find_nodes(body, 'blocking_assignment'):
            children = ba.named_children
            if len(children) >= 2:
                read_sigs.update(_get_identifiers(children[-1]))
        for cs in _find_nodes(body, 'conditional_statement'):
            cond = _find_nodes(cs, 'cond_predicate')
            for c in cond:
                read_sigs.update(_get_identifiers(c))
        for ce in _find_nodes(body, 'case_expression'):
            read_sigs.update(_get_identifiers(ce))

        missing = read_sigs - sens_sigs
        lhs_sigs = set()
        for ba in _find_nodes(body, 'blocking_assignment'):
            s = _get_lhs_signal(ba)
            if s:
                lhs_sigs.add(s)
        missing -= lhs_sigs

        if missing:
            findings.append(Finding(
                rule="W415_incomplete_sens", severity="warning",
                file=file, line=_node_line(always),
                message=f"Sensitivity list missing: {', '.join(sorted(missing))}",
                synth_impact="Sim/synth mismatch: synthesis assumes full sensitivity",
            ))
    return findings


def W446_part_select_oob(tree, file, signals):
    """W446: Constant part-select out of bounds."""
    findings = []
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}
    all_text = _strip_comments(_node_text(tree.root_node))
    for m in re.finditer(r'(\w+)\[(\d+)\]', all_text):
        sig_name, idx_str = m.group(1), m.group(2)
        si = signals.get(sig_name)
        if not si or si.width <= 0:
            continue
        idx = int(idx_str)
        if idx >= si.width:
            line = all_text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="W446_oob_select", severity="error",
                file=file, line=line,
                message=f"Bit select '{sig_name}[{idx}]' out of bounds "
                        f"(signal is {si.width}-bit, max index {si.width-1})",
                synth_impact="Accessing non-existent bit: X or 0 at synthesis",
            ))
    return findings


def W480_loop_var_type(tree, file, signals):
    """W480: Loop variable should be integer or genvar."""
    findings = []
    for loop in _find_nodes(tree.root_node, 'loop_statement'):
        text = _node_text(loop)
        if text.startswith('for'):
            for_init = _find_nodes(loop, 'for_initialization')
            if for_init:
                init_ids = _get_identifiers(for_init[0])
                for v in init_ids:
                    vi = signals.get(v)
                    if vi and vi.direction == 'internal' and vi.width > 0:
                        if vi.width < 32:
                            findings.append(Finding(
                                rule="W480_loop_var", severity="info",
                                file=file, line=_node_line(loop),
                                message=f"Loop var '{v}' is {vi.width}-bit "
                                        f"— use integer for synthesis safety",
                                synth_impact="Narrow loop var may overflow",
                            ))
    return findings


def W494_non_full_case(tree, file, signals):
    """W494: Case statement doesn't cover all possible values."""
    findings = []
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}
    for cs in _find_nodes(tree.root_node, 'case_statement'):
        items = _find_direct_case_items(cs)
        has_default = any('default' in _node_text(i)[:20] for i in items)
        if has_default:
            continue
        # Count case items (exclude default)
        n_items = len([i for i in items
                       if 'default' not in _node_text(i)[:20]])
        # Get case expression to find width
        ce_nodes = [c for c in cs.named_children
                    if c.type == 'case_expression']
        ce = ce_nodes
        if ce:
            ce_ids = _get_identifiers(ce[0])
            for cid in ce_ids:
                si = signals.get(cid)
                if si and si.width > 0:
                    total_possible = 1 << si.width
                    if n_items < total_possible:
                        findings.append(Finding(
                            rule="W494_non_full_case", severity="info",
                            file=file, line=_node_line(cs),
                            message=f"case({cid}) has {n_items}/{total_possible} "
                                    f"values covered, no default",
                            synth_impact="Non-full case: synthesis may infer latches "
                                         "or dont-care for missing arms",
                        ))
                    break
    return findings


def W_duplicate_case(tree, file, signals):
    """Duplicate case values in case statement."""
    findings = []
    for cs in _find_nodes(tree.root_node, 'case_statement'):
        seen_values: dict[str, int] = {}
        for item in _find_direct_case_items(cs):
            item_text = _node_text(item).strip()
            if item_text.startswith('default'):
                continue
            colon = item_text.find(':')
            if colon < 0:
                continue
            val = re.sub(r'\s+', '', item_text[:colon])
            if val in seen_values:
                findings.append(Finding(
                    rule="W_duplicate_case", severity="error",
                    file=file, line=_node_line(item),
                    message=f"Duplicate case value '{item_text[:colon].strip()}' "
                            f"(first at L{seen_values[val]})",
                    synth_impact="Unreachable arm: sim/synth mismatch",
                ))
            else:
                seen_values[val] = _node_line(item)
    return findings


def W_multiple_drivers(tree, file, signals):
    """Signal driven from multiple always blocks."""
    findings = []
    # Memory arrays: `logic [W-1:0] mem [0:D-1]` — a multi-driven memory is a
    # 2-write-port RAM pattern, not a bus-contention bug.
    mem_arrays = set(re.findall(
        r'\b(?:logic|reg|bit)\s+(?:\[[^\]]+\]\s+)?(\w+)\s*\[',
        _strip_comments(_node_text(tree.root_node))))

    sig_drivers: dict[str, list[int]] = {}
    for always in _find_nodes(tree.root_node, 'always_construct'):
        al = _node_line(always)
        seen = set()
        for ntype in ('blocking_assignment', 'nonblocking_assignment'):
            for n in _find_nodes(always, ntype):
                sig = _get_lhs_signal(n)
                if sig and sig not in seen:
                    seen.add(sig)
                    sig_drivers.setdefault(sig, []).append(al)
        for cd in _find_nodes(always, 'clocking_drive'):
            sig = _get_lhs_from_any(cd)
            if sig and sig not in seen:
                seen.add(sig)
                sig_drivers.setdefault(sig, []).append(al)

    for sig, blocks in sig_drivers.items():
        unique = sorted(set(blocks))
        if len(unique) >= 2:
            locs = ', '.join(f'L{b}' for b in unique)
            if sig in mem_arrays:
                findings.append(Finding(
                    rule="W_multi_driver", severity="error",
                    file=file, line=unique[0],
                    message=(f"memory '{sig}' written from {len(unique)} "
                             f"always blocks ({locs}) — {len(unique)} write "
                             f"ports on a generic RAM array"),
                    synth_impact=(
                        "Multi-write-port RAM: a plain array with 2 write "
                        "ports will not infer a standard block RAM. FPGA needs "
                        "a vendor dual-port template (or write-conflict "
                        "arbitration); ASIC needs a 2-write-port SRAM macro"),
                ))
            else:
                findings.append(Finding(
                    rule="W_multi_driver", severity="error",
                    file=file, line=unique[0],
                    message=f"'{sig}' driven from {len(unique)} always blocks ({locs})",
                    synth_impact="Multiple drivers: bus contention or X propagation",
                ))
    return findings


def W_multi_seq_assign(tree, file, signals):
    """Signal assigned in multiple sequential blocks."""
    findings = []
    sig_blocks: dict[str, list[int]] = {}
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        al = _node_line(always)
        seen = set()
        for ntype in ('blocking_assignment', 'nonblocking_assignment'):
            for n in _find_nodes(always, ntype):
                sig = _get_lhs_signal(n)
                if sig and sig not in seen:
                    seen.add(sig)
                    sig_blocks.setdefault(sig, []).append(al)
    for sig, blocks in sig_blocks.items():
        unique = sorted(set(blocks))
        if len(unique) >= 2:
            locs = ', '.join(f'L{b}' for b in unique)
            findings.append(Finding(
                rule="W_multi_seq_assign", severity="error",
                file=file, line=unique[0],
                message=f"'{sig}' in {len(unique)} sequential blocks ({locs})",
                synth_impact="Same register from multiple clocked blocks: fail",
            ))
    return findings


# -------------------------------------------------------------------
# Category 2: SYNTHESIS RULES (SYNTH)
# -------------------------------------------------------------------

def SYNTH_initial_block(tree, file, signals):
    """SYNTH_5000: initial block is not synthesizable."""
    findings = []
    for init in _find_nodes(tree.root_node, 'initial_construct'):
        findings.append(Finding(
            rule="SYNTH_5000_initial", severity="warning",
            file=file, line=_node_line(init),
            message="'initial' block is not synthesizable",
            synth_impact="Synthesis ignores initial blocks: sim-only behavior",
        ))
    return findings


def SYNTH_delay(tree, file, signals):
    """SYNTH_5001: #delay is not synthesizable."""
    findings = []
    for d in _find_nodes(tree.root_node, 'delay_control'):
        findings.append(Finding(
            rule="SYNTH_5001_delay", severity="warning",
            file=file, line=_node_line(d),
            message=f"#delay '{_node_text(d)[:20]}' is not synthesizable",
            synth_impact="Synthesis ignores delays: timing will differ",
        ))
    for d in _find_nodes(tree.root_node, 'delay_value'):
        parent = d.parent
        if parent and parent.type == 'delay_control':
            continue
        if parent and parent.type == 'net_declaration':
            findings.append(Finding(
                rule="SYNTH_5001_delay", severity="warning",
                file=file, line=_node_line(d),
                message=f"Net delay is not synthesizable",
                synth_impact="Synthesis ignores net delays",
            ))
    return findings


def SYNTH_real_type(tree, file, signals):
    """SYNTH_5002: 'real' type not synthesizable."""
    findings = []
    for n in _find_nodes(tree.root_node, 'non_integer_type'):
        if 'real' in _node_text(n):
            findings.append(Finding(
                rule="SYNTH_5002_real", severity="error",
                file=file, line=_node_line(n),
                message="'real' type is not synthesizable",
                synth_impact="Synthesis cannot map floating-point to gates",
            ))
    return findings


def SYNTH_integer_type(tree, file, signals):
    """SYNTH_5003: 'integer' used for synthesis (32-bit, wastes area)."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\binteger\s+(\w+)', text):
        name = m.group(1)
        if name in ('i', 'j', 'k', 'idx', 'n', 'ii', 'jj'):
            continue
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SYNTH_5003_integer", severity="info",
            file=file, line=line,
            message=f"'integer {name}' synthesizes to 32-bit register",
            synth_impact="Use sized logic/reg to save area",
        ))
    return findings


def SYNTH_system_tasks(tree, file, signals):
    """SYNTH_5004: System tasks ($display, $monitor, etc.) in RTL."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    sys_tasks = [r'\$display', r'\$monitor', r'\$write', r'\$strobe',
                 r'\$finish', r'\$stop', r'\$time', r'\$realtime',
                 r'\$fopen', r'\$fclose', r'\$fwrite', r'\$fdisplay',
                 r'\$readmemh', r'\$readmemb', r'\$dumpfile',
                 r'\$dumpvars', r'\$random', r'\$urandom']
    for st in sys_tasks:
        for m in re.finditer(st + r'\b', text):
            task_name = m.group(0)
            line = text[:m.start()].count('\n') + 1
            # Skip if inside initial block (expected for sim)
            findings.append(Finding(
                rule="SYNTH_5004_sys_task", severity="info",
                file=file, line=line,
                message=f"System task '{task_name}' is simulation-only",
                synth_impact="Synthesis ignores system tasks",
            ))
    return findings


def SYNTH_tristate(tree, file, signals):
    """SYNTH_5005: Tristate ('z) in non-top module."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r"\b\d+'[bB]?[zZ]+\b", text):
        line = text[:m.start()].count('\n') + 1
        has_inout = any(s.direction == 'inout' for s in signals.values())
        if not has_inout:
            findings.append(Finding(
                rule="SYNTH_5005_tristate", severity="warning",
                file=file, line=line,
                message=f"High-Z value '{m.group(0)}' without inout port",
                synth_impact="Internal tri-state: most ASIC flows don't support",
            ))
    return findings


def SYNTH_latch_always(tree, file, signals):
    """SYNTH_12608: always_comb block creates latch due to incomplete assignment."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue
        has_if = bool(_find_nodes(always, 'conditional_statement'))
        has_case = bool(_find_nodes(always, 'case_statement'))
        if not has_if and not has_case:
            continue

        all_ba = _find_nodes(always, 'blocking_assignment')
        assigned = set()
        for ba in all_ba:
            s = _get_lhs_signal(ba)
            if s:
                assigned.add(s)

        default_assigned = set()
        seq_block = _find_nodes(always, 'seq_block')
        if seq_block:
            block = seq_block[0]
            _collect_unconditional_assigns(block, default_assigned)
            for child in block.named_children:
                if child.type == 'statement_or_null':
                    inner = child.named_children[0] if child.named_children else None
                    if inner and inner.type == 'statement':
                        si = inner.named_children[0] if inner.named_children else None
                        if si and si.type == 'statement_item':
                            for loop in _find_nodes(si, 'loop_statement'):
                                for lc in loop.named_children:
                                    if lc.type == 'statement_or_null':
                                        ls = lc.named_children[0] if lc.named_children else None
                                        if ls and ls.type == 'statement':
                                            lsi = ls.named_children[0] if ls.named_children else None
                                            if lsi and lsi.type == 'statement_item':
                                                lb = _find_nodes(lsi, 'seq_block')
                                                if lb:
                                                    _collect_unconditional_assigns(lb[0], default_assigned)

        at_risk = assigned - default_assigned
        if not at_risk:
            continue

        conds = _find_nodes(always, 'conditional_statement')
        for cs in conds:
            text = _node_text(cs)
            if '\nelse' not in text and ' else' not in text:
                for s in at_risk:
                    findings.append(Finding(
                        rule="SYNTH_12608_latch", severity="warning",
                        file=file, line=_node_line(cs),
                        message=f"if without else: '{s}' may infer latch",
                        synth_impact="Synthesis creates latch for incomplete conditional",
                    ))
                break
    return findings


# -------------------------------------------------------------------
# Category 3: FUNCTIONAL / SEMANTIC (FUNC)
# -------------------------------------------------------------------

def _split_comparison(text: str):
    """Split 'a <op> b' at the top-level comparison operator (tracking () and
    [] depth). Returns (lhs_expr, op, rhs_expr) or None."""
    text = text.strip()
    while text.startswith('(') and text.endswith(')'):
        # only strip if the outer parens are balanced as a wrapper
        d = 0
        wrap = True
        for j, ch in enumerate(text):
            if ch in '([':
                d += 1
            elif ch in ')]':
                d -= 1
                if d == 0 and j < len(text) - 1:
                    wrap = False
                    break
        if wrap:
            text = text[1:-1].strip()
        else:
            break
    depth = 0
    for op in ('==', '!=', '>=', '<='):
        d = 0
        for i in range(len(text) - 1):
            ch = text[i]
            if ch in '([':
                d += 1
            elif ch in ')]':
                d -= 1
            elif d == 0 and text[i:i+2] == op:
                return text[:i].strip(), op, text[i+2:].strip()
    # single-char < or > (not part of << >> <= >=)
    d = 0
    for i, ch in enumerate(text):
        if ch in '([':
            d += 1
        elif ch in ')]':
            d -= 1
        elif d == 0 and ch in '<>':
            prev = text[i-1] if i > 0 else ''
            nxt = text[i+1] if i+1 < len(text) else ''
            if prev in '<>' or nxt in '<>=':
                continue
            return text[:i].strip(), ch, text[i+1:].strip()
    return None


def FUNC_comparison_oor(tree, file, signals):
    """Comparison out of range: signal width cannot hold the value."""
    findings = []
    elab = _current_elab(tree, signals)

    seen = set()
    for binop in _find_nodes_multi(tree.root_node,
                                    {'binary_expression', 'expression'}):
        text = _node_text(binop).strip()
        split = _split_comparison(text)
        if not split:
            continue
        lhs_expr, op, rhs_expr = split
        key = (lhs_expr, op, rhs_expr, _node_line(binop))
        if key in seen:
            continue
        seen.add(key)

        for sig_expr, val_expr in [(lhs_expr, rhs_expr), (rhs_expr, lhs_expr)]:
            if not re.fullmatch(r'\w+', sig_expr):
                continue
            sig_name, val_name = sig_expr, val_expr
            si = signals.get(sig_name)
            # Fold the *whole* other operand (part-selects, arithmetic, etc.)
            val = elab.const_of(val_expr)

            if not si or si.is_param or si.width <= 0 or val is None:
                continue
            # A folded negative constant (e.g. X[1:0]-1 wrapping) is in range.
            if val < 0:
                continue

            max_val = (1 << si.width) - 1
            needed_width = val.bit_length() if val > 0 else 1
            fix_hint = (f". Fix: widen '{sig_name}' to {needed_width} bits"
                        f", or compare against {max_val}")

            if op == '==' and val > max_val:
                findings.append(Finding(
                    rule="FUNC_cmp_out_of_range", severity="error",
                    file=file, line=_node_line(binop),
                    message=f"'{sig_name} == {val_name}' always FALSE: "
                            f"{si.width}-bit max={max_val}{fix_hint}",
                    synth_impact="Dead branch: unreachable states, FSM may get stuck",
                ))
            elif op == '!=' and val > max_val:
                findings.append(Finding(
                    rule="FUNC_cmp_out_of_range", severity="warning",
                    file=file, line=_node_line(binop),
                    message=f"'{sig_name} != {val_name}' always TRUE: "
                            f"{si.width}-bit max={max_val} vs {val}",
                    synth_impact="Constant condition: branch always taken",
                ))
            elif op == '>=' and val > max_val:
                findings.append(Finding(
                    rule="FUNC_cmp_out_of_range", severity="error",
                    file=file, line=_node_line(binop),
                    message=f"'{sig_name} >= {val_name}' always FALSE: "
                            f"{si.width}-bit max={max_val}{fix_hint}",
                    synth_impact="Dead branch: code is unreachable",
                ))
            elif op == '>' and val >= max_val:
                findings.append(Finding(
                    rule="FUNC_cmp_out_of_range", severity="error",
                    file=file, line=_node_line(binop),
                    message=f"'{sig_name} > {val_name}' always FALSE: "
                            f"{si.width}-bit max={max_val}{fix_hint}",
                    synth_impact="Dead branch: code is unreachable",
                ))
            elif op == '<' and val == 0:
                findings.append(Finding(
                    rule="FUNC_cmp_out_of_range", severity="error",
                    file=file, line=_node_line(binop),
                    message=f"'{sig_name} < {val_name}' always FALSE: "
                            f"unsigned signal cannot be negative",
                    synth_impact="Dead branch: code is unreachable",
                ))
            break
    return findings


def FUNC_counter_overflow(tree, file, signals):
    """Counter wraps before reaching terminal count — FSM exit unreachable."""
    findings = []
    elab = _current_elab(tree, signals)

    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        # Find counter increments: sig <= sig + 1
        for atype in ('nonblocking_assignment', 'blocking_assignment'):
            for asgn in _find_nodes(always, atype):
                lhs = _get_lhs_signal(asgn)
                if not lhs:
                    continue
                target = asgn
                oa = _find_nodes(asgn, 'operator_assignment')
                if oa:
                    target = oa[0]
                children = target.named_children
                if len(children) < 2:
                    continue
                rhs_text = _node_text(children[-1]).strip()
                # Match: sig + 1, sig + 1'b1, etc.
                if not re.match(rf'{re.escape(lhs)}\s*\+\s*1\b', rhs_text):
                    continue
                si = signals.get(lhs)
                if not si or si.width <= 0:
                    continue
                max_val = (1 << si.width) - 1
                # Search for comparisons against this counter in the full tree.
                # Capture the full RHS expression (part-selects, arithmetic)
                # up to a delimiter, then constant-fold it.
                full_text = _node_text(tree.root_node)
                for cm in re.finditer(
                        rf'\b{re.escape(lhs)}\s*==\s*([^;,)&|?\n]+)', full_text):
                    val_expr = cm.group(1).strip()
                    val = elab.const_of(val_expr)
                    # Only a clean, in-representable terminal count that the
                    # counter genuinely cannot reach is an overflow.
                    if val is not None and val > max_val:
                        findings.append(Finding(
                            rule="FUNC_counter_overflow", severity="error",
                            file=file, line=_node_line(asgn),
                            message=(
                                f"Counter overflow: '{lhs}' is {si.width}-bit "
                                f"(max {max_val}), wraps before reaching "
                                f"{val_expr}={val}. "
                                f"Fix: widen to {val.bit_length()} bits"),
                            synth_impact="Counter wraps to 0: exit condition "
                                         "never met, FSM stuck in loop",
                        ))
    return findings


def FUNC_comb_loop(tree, file, signals):
    """Combinational loop: signal reads itself in comb block or via continuous assigns."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue
        for ba in _find_nodes(always, 'blocking_assignment'):
            lhs = _get_lhs_signal(ba)
            if not lhs:
                continue
            children = ba.named_children
            if len(children) >= 2:
                rhs_ids = _get_identifiers(children[-1])
                if lhs in rhs_ids:
                    findings.append(Finding(
                        rule="FUNC_comb_loop", severity="error",
                        file=file, line=_node_line(ba),
                        message=f"'{lhs}' reads itself in comb block — comb loop",
                        synth_impact="Synthesis fails or creates oscillating logic",
                    ))

    # Cross-assign dependency graph: detect cycles through continuous assigns
    dep_graph = {}  # signal -> [(dependency, line_number)]
    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        for na in _find_nodes(ca, 'net_assignment'):
            nc = na.named_children
            if len(nc) < 2:
                continue
            lv = nc[0]
            lhs = _node_text(lv).strip()
            rhs_ids = _get_identifiers(nc[-1])
            dep_graph.setdefault(lhs, [])
            for rhs_id in rhs_ids:
                if rhs_id != lhs:
                    dep_graph[lhs].append((rhs_id, _node_line(ca)))

    # DFS cycle detection
    reported = set()
    for start_sig in dep_graph:
        visited = set()
        stack = [(start_sig, [start_sig])]
        while stack:
            sig, path = stack.pop()
            if sig in visited:
                continue
            visited.add(sig)
            for dep, line in dep_graph.get(sig, []):
                if dep == start_sig:
                    cycle_sigs = frozenset(path)
                    if cycle_sigs not in reported:
                        reported.add(cycle_sigs)
                        cycle_str = ' -> '.join(path + [start_sig])
                        findings.append(Finding(
                            rule="FUNC_comb_loop", severity="error",
                            file=file, line=line,
                            message=f"Combinational loop via continuous assigns: {cycle_str}",
                            synth_impact="Synthesis fails or creates oscillating logic",
                        ))
                elif dep not in visited and dep in dep_graph:
                    stack.append((dep, path + [dep]))

    return findings


def FUNC_comb_depth(tree, file, signals):
    """Deep combinational nesting — timing risk."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue

        def _max_depth(node, depth=0):
            best = depth
            for child in node.children:
                d = depth + 1 if child.type in ('conditional_statement', 'case_statement') else depth
                best = max(best, _max_depth(child, d))
            return best

        d = _max_depth(always)
        if d >= 4:
            findings.append(Finding(
                rule="FUNC_comb_depth", severity="warning",
                file=file, line=_node_line(always),
                message=f"Comb nesting depth {d} — timing bottleneck",
                synth_impact=f"~{d * 0.2:.1f}ns added to critical path",
            ))
    return findings


def _split_top_level(text: str, sep: str = ',') -> list[str]:
    """Split on top-level `sep`, respecting () {} [] nesting."""
    parts, depth, cur = [], 0, ''
    for ch in text:
        if ch in '({[':
            depth += 1
        elif ch in ')}]':
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def _operand_width(expr: str, signals, elab=None) -> int | None:
    """Width of a single expression operand (not a comma list).
    A boolean/logical/comparison result is 1 bit — NOT the sum of its
    operands, which is the classic concat-width bug."""
    expr = expr.strip()
    # Replication {N{x}}
    m = re.fullmatch(r'\{\s*(\w+)\s*\{(.+)\}\s*\}', expr)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else (
            elab.const_of(m.group(1)) if elab else None)
        w = _operand_width(m.group(2), signals, elab)
        return n * w if (n and w) else None
    # Nested concat
    if expr.startswith('{') and expr.endswith('}'):
        return _concat_width(expr, signals, elab)
    # Sized literal N'b...
    m = re.match(r"(\d+)'[bBhHdDoO]", expr)
    if m:
        return int(m.group(1))
    # Boolean/logical/comparison/reduction result → 1 bit
    if re.search(r'&&|\|\||==|!=|>=|<=|[<>]|^\s*!|^\s*[&|^~]', expr):
        return 1
    # Part-select sig[hi:lo]
    m = re.fullmatch(r'(\w+)\s*\[(.+):(.+)\]', expr)
    if m and elab:
        hi, lo = elab.const_of(m.group(2)), elab.const_of(m.group(3))
        if hi is not None and lo is not None:
            return abs(hi - lo) + 1
    # Bit-select sig[i] → 1
    if re.fullmatch(r'\w+\s*\[[^\]:]+\]', expr):
        return 1
    # Bare signal
    si = signals.get(expr)
    if si and si.width > 0:
        return si.width
    return None


def _concat_width(text: str, signals, elab=None) -> int | None:
    """Width of a concatenation = sum of its top-level element widths."""
    inner = text.strip()
    if inner.startswith('{') and inner.endswith('}'):
        inner = inner[1:-1]
    total = 0
    for elem in _split_top_level(inner):
        w = _operand_width(elem, signals, elab)
        if w is None:
            return None
        total += w
    return total if total > 0 else None


def FUNC_case_width_mismatch(tree, file, signals):
    """Case expression width doesn't match case item widths."""
    findings = []
    elab = _current_elab(tree, signals)
    for cs in _find_nodes(tree.root_node, 'case_statement'):
        ce_nodes = [c for c in cs.named_children
                    if c.type == 'case_expression']
        if not ce_nodes:
            continue
        ce_text = _node_text(ce_nodes[0]).strip()
        ce_width = None
        # Concatenation: width = sum of ELEMENT widths (a boolean element is
        # 1 bit, not the sum of the identifiers inside it).
        if ce_text.startswith('{') and ce_text.endswith('}'):
            ce_width = _concat_width(ce_text, signals, elab)
        else:
            for cid in _get_identifiers(ce_nodes[0]):
                si = signals.get(cid)
                if si and si.width > 0:
                    ce_width = si.width
                    break
        if ce_width is None:
            continue

        for item in _find_direct_case_items(cs):
            item_text = _node_text(item).strip()
            if item_text.startswith('default'):
                continue
            m = re.match(r"(\d+)'[bhBHdDoO](\w+)", item_text)
            if m:
                item_width = int(m.group(1))
                if item_width != ce_width:
                    findings.append(Finding(
                        rule="FUNC_case_width", severity="warning",
                        file=file, line=_node_line(item),
                        message=f"Case item is {item_width}-bit but "
                                f"expression is {ce_width}-bit",
                        synth_impact="Width mismatch in case: may never match",
                    ))
    return findings


def FUNC_magic_numbers(tree, file, signals):
    """Magic numbers: unlabeled numeric literals > 1 in assignments."""
    findings = []
    count = 0
    for always in _find_nodes(tree.root_node, 'always_construct'):
        for ba in _find_nodes(always, 'blocking_assignment'):
            children = ba.named_children
            if len(children) < 2:
                continue
            rhs = children[-1]
            nums = _find_nodes(rhs, 'decimal_number')
            for n in nums:
                val_text = _node_text(n)
                try:
                    val = int(val_text)
                except ValueError:
                    continue
                if val > 1 and not re.match(r"\d+'", val_text):
                    count += 1
    if count >= 3:
        findings.append(Finding(
            rule="FUNC_magic_numbers", severity="info",
            file=file, line=1,
            message=f"{count} magic numbers found — use localparam/define",
            synth_impact="Magic numbers reduce readability and maintainability",
        ))
    return findings


def FUNC_nonblocking_in_comb(tree, file, signals):
    """Non-blocking <= in combinational block."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue
        for nba in _find_nodes(always, 'nonblocking_assignment'):
            sig = _get_lhs_signal(nba)
            if sig:
                findings.append(Finding(
                    rule="FUNC_nba_in_comb", severity="warning",
                    file=file, line=_node_line(nba),
                    message=f"Non-blocking '<=' on '{sig}' in comb block",
                    synth_impact="Sim/synth mismatch: use = in combinational",
                ))
    return findings


def FUNC_assign_x(tree, file, signals):
    """Assignment of X value (except in synthesis pragmas)."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r"(\d+)'[bBhH][xX]+", text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="FUNC_assign_x", severity="info",
            file=file, line=line,
            message=f"X assignment '{m.group(0)}' — intentional don't-care?",
            synth_impact="Synthesis treats X as don't-care: may optimize aggressively",
        ))
    return findings


# -------------------------------------------------------------------
# Category 4: CLOCK / RESET (CLK)
# -------------------------------------------------------------------

def CLK_no_reset(tree, file, signals):
    """Sequential block without reset."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        text = _node_text(always)
        if 'rst' not in text.lower() and 'reset' not in text.lower():
            regs = set()
            for nba in _all_nb_assignments(always):
                s = _get_lhs_from_any(nba) if nba.type == 'clocking_drive' else _get_lhs_signal(nba)
                if s:
                    regs.add(s)
            for ba in _find_nodes(always, 'blocking_assignment'):
                s = _get_lhs_signal(ba)
                if s:
                    regs.add(s)
            if regs:
                findings.append(Finding(
                    rule="CLK_no_reset", severity="info",
                    file=file, line=_node_line(always),
                    message=f"Sequential block has no reset ({len(regs)} regs: "
                            f"{', '.join(sorted(regs)[:3])}...)",
                    synth_impact="Registers power up to unknown state in ASIC",
                ))
    return findings


def CLK_async_reset_data(tree, file, signals):
    """STARC05-1.3.1.3: Async reset used in data logic."""
    findings = []
    async_resets = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            text = _node_text(ev)
            for m_rst in re.finditer(r'negedge\s+(\w+)', text):
                async_resets.add(m_rst.group(1))
            for m_rst in re.finditer(r'posedge\s+(\w+)', text):
                sig = m_rst.group(1)
                if 'rst' in sig.lower() or 'reset' in sig.lower():
                    async_resets.add(sig)

    for rst in async_resets:
        for always in _find_nodes(tree.root_node, 'always_construct'):
            if _always_type(always) != 'always_comb':
                continue
            if rst in _get_identifiers(always):
                findings.append(Finding(
                    rule="CLK_async_rst_data", severity="warning",
                    file=file, line=_node_line(always),
                    message=f"Async reset '{rst}' in comb logic — STARC violation",
                    synth_impact="Reset glitches may corrupt data path",
                ))
                break
        for ca in _find_nodes(tree.root_node, 'continuous_assign'):
            if rst in _get_identifiers(ca) and _get_lhs_signal(ca) != rst:
                findings.append(Finding(
                    rule="CLK_async_rst_data", severity="warning",
                    file=file, line=_node_line(ca),
                    message=f"Async reset '{rst}' in assign — STARC violation",
                    synth_impact="Reset in data path: glitch risk",
                ))
                break
    return findings


def CLK_mixed_edge(tree, file, signals):
    """Clock signal used on both posedge and negedge."""
    findings = []
    pos_clks = set()
    neg_clks = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            text = _node_text(ev)
            for m_e in re.finditer(r'posedge\s+(\w+)', text):
                sig = m_e.group(1)
                if 'rst' not in sig.lower() and 'reset' not in sig.lower():
                    pos_clks.add(sig)
            for m_e in re.finditer(r'negedge\s+(\w+)', text):
                sig = m_e.group(1)
                if 'rst' not in sig.lower() and 'reset' not in sig.lower():
                    neg_clks.add(sig)
    for clk in pos_clks & neg_clks:
        findings.append(Finding(
            rule="CLK_mixed_edge", severity="warning",
            file=file, line=1,
            message=f"Clock '{clk}' used on both posedge and negedge",
            synth_impact="DDR design pattern: requires special handling in synthesis",
        ))
    return findings


def CLK_gated_clock(tree, file, signals):
    """Clock signal used in logic expressions (gated clock)."""
    findings = []
    clk_sigs = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            for m_e in re.finditer(r'(?:pos|neg)edge\s+(\w+)', _node_text(ev)):
                sig = m_e.group(1)
                if 'rst' not in sig.lower() and 'reset' not in sig.lower():
                    clk_sigs.add(sig)

    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        ids = _get_identifiers(ca)
        lhs = _get_lhs_signal(ca)
        for clk in clk_sigs:
            if clk in ids and clk != lhs:
                findings.append(Finding(
                    rule="CLK_gated_clock", severity="warning",
                    file=file, line=_node_line(ca),
                    message=f"Clock '{clk}' used in assign — gated clock",
                    synth_impact="Manual clock gating: use ICG cell instead",
                ))
    return findings


# -------------------------------------------------------------------
# Category 5: POWER (PWR)
# -------------------------------------------------------------------

def PWR_clock_gating_opp(tree, file, signals):
    """Sequential block without enable — clock gating opportunity."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        regs = set()
        for nba in _all_nb_assignments(always):
            s = _get_lhs_from_any(nba) if nba.type == 'clocking_drive' else _get_lhs_signal(nba)
            if s:
                regs.add(s)
        if len(regs) < 3:
            continue

        has_enable = False
        for cond in _find_nodes(always, 'conditional_statement'):
            m_if = re.search(r'if\s*\(([^)]+)\)', _node_text(cond))
            if m_if:
                cond_expr = m_if.group(1).lower()
                if 'rst' not in cond_expr and 'reset' not in cond_expr:
                    has_enable = True
                    break

        if not has_enable:
            findings.append(Finding(
                rule="PWR_no_clock_gate", severity="info",
                file=file, line=_node_line(always),
                message=f"{len(regs)} regs without enable — clock gating missed",
                synth_impact="Higher dynamic power: regs toggle every cycle",
            ))
    return findings


def PWR_large_mux(tree, file, signals):
    """Large case statement creates big mux tree."""
    findings = []
    for cs in _find_nodes(tree.root_node, 'case_statement'):
        items = _find_direct_case_items(cs)
        n = len([i for i in items if 'default' not in _node_text(i)[:20]])
        if n >= 8:
            findings.append(Finding(
                rule="PWR_large_mux", severity="info",
                file=file, line=_node_line(cs),
                message=f"Case with {n} arms creates large mux tree",
                synth_impact="Large mux: higher area and power, consider encoding",
            ))
    return findings


# -------------------------------------------------------------------
# Category 6: MEMORY / ARRAY
# -------------------------------------------------------------------

def MEM_array_size(tree, file, signals):
    """Memory array size check."""
    findings = []
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}
    text = _node_text(tree.root_node)
    text_clean = re.sub(r'//.*?$', '', text, flags=re.MULTILINE)
    for m in re.finditer(
            r'(?:logic|reg)\s*\[([^\]]+):([^\]]+)\]\s*(\w+)\s*\[([^\]]+?)(?::([^\]]+))?\]',
            text_clean):
        hi = _eval_param_expr(m.group(1), params)
        lo = _eval_param_expr(m.group(2), params)
        name = m.group(3)
        arr_hi = _eval_param_expr(m.group(4), params)
        arr_lo = _eval_param_expr(m.group(5), params) if m.group(5) else None

        if hi is None or lo is None or arr_hi is None:
            continue
        width = hi - lo + 1
        depth = abs(arr_hi - arr_lo) + 1 if arr_lo is not None else arr_hi
        total_bits = width * depth
        line = text_clean[:m.start()].count('\n') + 1

        if total_bits > 4096:
            findings.append(Finding(
                rule="MEM_large_array", severity="error",
                file=file, line=line,
                message=f"'{name}' [{width}x{depth}]={total_bits} bits — needs SRAM macro",
                synth_impact=f"FF array: ~{total_bits*3:.0f} um^2 wasted area",
            ))
        elif total_bits > 1024:
            findings.append(Finding(
                rule="MEM_medium_array", severity="warning",
                file=file, line=line,
                message=f"'{name}' [{width}x{depth}]={total_bits} bits — consider SRAM",
                synth_impact=f"{total_bits} FFs vs SRAM macro: ~60% area savings",
            ))
        elif total_bits > 256:
            findings.append(Finding(
                rule="MEM_small_array", severity="info",
                file=file, line=line,
                message=f"'{name}' [{width}x{depth}]={total_bits} bits — "
                        f"SRAM optional for area savings",
                synth_impact=f"FF-based: {total_bits} FFs, SRAM saves ~40%",
            ))
    return findings


# -------------------------------------------------------------------
# Category 7: FSM
# -------------------------------------------------------------------

def FSM_encoding(tree, file, signals):
    """FSM encoding analysis."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(
            r'typedef\s+enum\s+logic\s*\[(\d+):(\d+)\]\s*\{([^}]+)\}\s*(\w+)',
            text):
        if not _is_fsm_enum(m.group(4), tree):
            continue
        width = int(m.group(1)) - int(m.group(2)) + 1
        body = re.sub(r'//[^\n]*', '', m.group(3))
        states = [s.strip().split('=')[0].strip()
                  for s in body.split(',')
                  if s.strip() and re.fullmatch(r'[a-zA-Z_]\w*',
                     s.strip().split('=')[0].strip())]
        n_states = len(states)
        line = text[:m.start()].count('\n') + 1
        min_bits = max(1, math.ceil(math.log2(max(n_states, 2))))

        if width > min_bits + 1:
            findings.append(Finding(
                rule="FSM_over_encoded", severity="info",
                file=file, line=line,
                message=f"FSM '{m.group(4)}': {n_states} states in "
                        f"{width}-bit (min {min_bits})",
                synth_impact=f"Over-encoded: {width - min_bits} extra bits per reg",
            ))
        if n_states > 16:
            findings.append(Finding(
                rule="FSM_large", severity="warning",
                file=file, line=line,
                message=f"FSM '{m.group(4)}' has {n_states} states",
                synth_impact="Large FSM: complex decode, timing risk",
            ))
    return findings


# -------------------------------------------------------------------
# Category 8: STYLE / NAMING
# -------------------------------------------------------------------

def STYLE_module_name(tree, file, signals):
    """Module name should match filename."""
    findings = []
    for mod in _find_nodes(tree.root_node, 'module_declaration'):
        mod_name = _module_name(mod)
        if mod_name:
            file_stem = os.path.splitext(file)[0]
            if mod_name != file_stem:
                findings.append(Finding(
                    rule="STYLE_mod_name", severity="info",
                    file=file, line=_node_line(mod),
                    message=f"Module '{mod_name}' doesn't match file '{file}'",
                    synth_impact="No synthesis impact, but confusing for tooling",
                ))
            break
    return findings


def STYLE_active_low_suffix(tree, file, signals):
    """Active-low signals should end with _n or _b."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            text = _node_text(ev)
            for m_e in re.finditer(r'negedge\s+(\w+)', text):
                sig = m_e.group(1)
                if not sig.endswith(('_n', '_b', '_ni', '_no')):
                    if 'rst' in sig.lower() or 'reset' in sig.lower():
                        findings.append(Finding(
                            rule="STYLE_active_low", severity="info",
                            file=file, line=_node_line(ev),
                            message=f"Active-low '{sig}' should end with _n or _b",
                            synth_impact="No impact, naming convention for readability",
                        ))
    return findings


def STYLE_clk_naming(tree, file, signals):
    """Clock signal naming convention."""
    findings = []
    clk_sigs = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            for m_e in re.finditer(r'posedge\s+(\w+)', _node_text(ev)):
                sig = m_e.group(1)
                if 'rst' not in sig.lower() and 'reset' not in sig.lower():
                    clk_sigs.add(sig)
    for clk in clk_sigs:
        if not (clk.startswith('clk') or clk.endswith('clk') or
                clk.startswith('clock') or clk.endswith('_clk') or
                '_clk' in clk or clk == 'clk_i'):
            findings.append(Finding(
                rule="STYLE_clk_name", severity="info",
                file=file, line=1,
                message=f"Clock signal '{clk}' doesn't follow clk naming convention",
                synth_impact="No impact, naming convention for readability",
            ))
    return findings


def STYLE_tab_indent(tree, file, signals):
    """Mixed tabs and spaces."""
    findings = []
    text = _node_text(tree.root_node)
    lines = text.split('\n')
    has_tab = any('\t' in l for l in lines[:100])
    has_space = any(l.startswith('  ') for l in lines[:100])
    if has_tab and has_space:
        findings.append(Finding(
            rule="STYLE_mixed_indent", severity="info",
            file=file, line=1,
            message="Mixed tabs and spaces in indentation",
            synth_impact="No synthesis impact, readability issue",
        ))
    return findings


def STYLE_line_length(tree, file, signals):
    """Lines exceeding 120 characters."""
    findings = []
    text = _node_text(tree.root_node)
    long_lines = 0
    for i, line in enumerate(text.split('\n'), 1):
        if len(line) > 120:
            long_lines += 1
    if long_lines > 0:
        findings.append(Finding(
            rule="STYLE_long_lines", severity="info",
            file=file, line=1,
            message=f"{long_lines} line(s) exceed 120 characters",
            synth_impact="No impact, readability issue",
        ))
    return findings


# -------------------------------------------------------------------
# Category 9: SIMULATION (SIM)
# -------------------------------------------------------------------

def SIM_force_release(tree, file, signals):
    """force/release are simulation-only."""
    findings = []
    for node_type, kw in [('force_statement', 'force'),
                          ('release_statement', 'release')]:
        for node in _find_nodes(tree.root_node, node_type):
            findings.append(Finding(
                rule="SIM_force_release", severity="error",
                file=file, line=_node_line(node),
                message=f"'{kw}' statement is simulation-only",
                synth_impact="Not synthesizable: synthesis will error",
            ))
    if not findings:
        text = _node_text(tree.root_node)
        text_no_comments = re.sub(r'//[^\n]*', '', text)
        text_no_comments = re.sub(r'/\*.*?\*/', '', text_no_comments, flags=re.DOTALL)
        for kw in ['force', 'release']:
            for m in re.finditer(rf'^\s*{kw}\s+\w+', text_no_comments, re.MULTILINE):
                line = text_no_comments[:m.start()].count('\n') + 1
                findings.append(Finding(
                    rule="SIM_force_release", severity="error",
                    file=file, line=line,
                    message=f"'{kw}' statement is simulation-only",
                    synth_impact="Not synthesizable: synthesis will error",
                ))
    return findings


def SIM_deassign(tree, file, signals):
    """assign/deassign procedural are simulation-only."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bdeassign\s+\w+', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SIM_deassign", severity="error",
            file=file, line=line,
            message="'deassign' is simulation-only",
            synth_impact="Not synthesizable",
        ))
    return findings


def SIM_event_trigger(tree, file, signals):
    """Named event triggers (-> event) are simulation-only."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'->\s*\w+\s*;', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SIM_event_trigger", severity="warning",
            file=file, line=line,
            message="Event trigger '->' is typically simulation-only",
            synth_impact="May not be synthesizable depending on tool",
        ))
    return findings


# -------------------------------------------------------------------
# Category 10: ADDITIONAL STRUCTURAL
# -------------------------------------------------------------------

def STRUCT_empty_block(tree, file, signals):
    """Empty begin/end blocks."""
    findings = []
    for sb in _find_nodes(tree.root_node, 'seq_block'):
        children = [c for c in sb.named_children
                    if c.type not in ('begin', 'end')]
        if not children:
            findings.append(Finding(
                rule="STRUCT_empty_block", severity="info",
                file=file, line=_node_line(sb),
                message="Empty begin/end block",
                synth_impact="Dead code: synthesis ignores, but confusing",
            ))
    return findings


def STRUCT_nested_ternary(tree, file, signals):
    """Nested ternary operators (hard to read, may cause priority issues)."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\?[^;]*\?', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="STRUCT_nested_ternary", severity="info",
            file=file, line=line,
            message="Nested ternary operator — consider if/else or case",
            synth_impact="Deep mux chain, may impact timing",
        ))
    return findings


def STRUCT_assign_in_cond(tree, file, signals):
    """Assignment inside conditional expression (likely meant ==)."""
    findings = []
    for cs in _find_nodes(tree.root_node, 'conditional_statement'):
        conds = _find_nodes(cs, 'cond_predicate')
        for c in conds:
            text = _node_text(c)
            # Single = but not ==, !=, <=, >=
            cleaned = re.sub(r'[!=<>]=', '', text)
            if '=' in cleaned and '(' not in text:
                findings.append(Finding(
                    rule="STRUCT_assign_in_cond", severity="error",
                    file=file, line=_node_line(c),
                    message=f"Possible assignment in condition: '{text[:40]}'",
                    synth_impact="Likely meant == instead of =",
                ))
    return findings


def STRUCT_recursive_assign(tree, file, signals):
    """Signal assigned to itself (a = a)."""
    findings = []
    all_always = _find_nodes(tree.root_node, 'always_construct')
    for atype in ('blocking_assignment', 'nonblocking_assignment'):
        for asgn in _find_nodes(tree.root_node, atype):
            lhs = _get_lhs_signal(asgn)
            if not lhs:
                continue
            children = asgn.named_children
            if len(children) >= 2:
                rhs_text = _node_text(children[-1]).strip()
                if rhs_text == lhs:
                    in_ff = any(_always_type(a) == 'always_ff'
                                for a in all_always if _is_inside(asgn, a))
                    if in_ff:
                        findings.append(Finding(
                            rule="STRUCT_self_assign", severity="info",
                            file=file, line=_node_line(asgn),
                            message=f"'{lhs}' explicit hold (a <= a) in sequential block — redundant",
                            synth_impact="FF holds value by default; no synthesis impact",
                        ))
                    else:
                        findings.append(Finding(
                            rule="STRUCT_self_assign", severity="warning",
                            file=file, line=_node_line(asgn),
                            message=f"'{lhs}' assigned to itself (a = a)",
                            synth_impact="No-op assignment: likely a bug",
                        ))
    return findings


def STRUCT_param_no_default(tree, file, signals):
    """Parameter without default value."""
    findings = []
    for pd in _find_nodes(tree.root_node, 'parameter_declaration'):
        text = _node_text(pd)
        if '=' not in text:
            ids = _get_identifiers(pd)
            if ids:
                findings.append(Finding(
                    rule="STRUCT_param_no_default", severity="info",
                    file=file, line=_node_line(pd),
                    message=f"Parameter '{ids[0]}' has no default value",
                    synth_impact="Must be overridden at instantiation",
                ))
    return findings


def STRUCT_unused_param(tree, file, signals):
    """Parameter defined but never used."""
    findings = []
    all_text = _strip_comments(_node_text(tree.root_node))
    for sig_name, sig_info in signals.items():
        if not sig_info.is_param:
            continue
        count = len(re.findall(rf'\b{re.escape(sig_name)}\b', all_text))
        if count <= 1:
            findings.append(Finding(
                rule="STRUCT_unused_param", severity="info",
                file=file, line=sig_info.line,
                message=f"Parameter '{sig_name}' defined but never used",
                synth_impact="Dead parameter: no impact but confusing",
            ))
    return findings


def STRUCT_always_star_recommend(tree, file, signals):
    """Old Verilog always @(sig list) — recommend always_comb or always @(*)."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _find_nodes(always, 'always_ff') or _find_nodes(always, 'always_comb'):
            continue
        events = _find_nodes(always, 'event_expression')
        if not events:
            continue
        ev_text = ' '.join(_node_text(e) for e in events)
        if '*' in ev_text or 'posedge' in ev_text or 'negedge' in ev_text:
            continue
        # Old style: always @(a or b or c)
        findings.append(Finding(
            rule="STRUCT_use_always_star", severity="info",
            file=file, line=_node_line(always),
            message="Use always @(*) or always_comb instead of explicit sensitivity",
            synth_impact="Explicit list may be incomplete; @(*) is safer",
        ))
    return findings


def STRUCT_continuous_assign_to_reg(tree, file, signals):
    """Continuous assign to reg/logic (should be wire)."""
    findings = []
    reg_sigs = set()
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\breg\s+(?:\[[^\]]+\]\s*)?(\w+)', text):
        reg_sigs.add(m.group(1))

    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        for na in _find_nodes(ca, 'net_assignment'):
            sig = _get_lhs_signal(na)
            if sig and sig in reg_sigs:
                findings.append(Finding(
                    rule="STRUCT_assign_to_reg", severity="error",
                    file=file, line=_node_line(na),
                    message=f"Continuous assign to reg '{sig}' — use wire",
                    synth_impact="Type conflict: may cause synthesis error",
                ))
    return findings


# -------------------------------------------------------------------
# Category 11: ADDITIONAL W-SERIES
# -------------------------------------------------------------------

def W213_signed_unsigned(tree, file, signals):
    """W213: Signed/unsigned comparison mismatch."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    signed_sigs = set()
    for m in re.finditer(r'\b(?:signed|int)\s+(?:\[[^\]]+\]\s*)?(\w+)', text):
        name = m.group(1)
        if name not in signals or signals[name].is_param:
            continue
        signed_sigs.add(name)
    if not signed_sigs:
        return findings
    for binop in _find_nodes_multi(tree.root_node,
                                    {'binary_expression', 'expression'}):
        bt = _node_text(binop)
        if '==' not in bt and '!=' not in bt and '<' not in bt and '>' not in bt:
            continue
        ids = _get_identifiers(binop)
        has_signed = any(i in signed_sigs for i in ids)
        has_unsigned = any(i not in signed_sigs and i in signals
                          and not signals[i].is_param for i in ids)
        if has_signed and has_unsigned:
            findings.append(Finding(
                rule="W213_sign_compare", severity="warning",
                file=file, line=_node_line(binop),
                message=f"Signed/unsigned comparison: may produce wrong result",
                synth_impact="Implicit sign-extend can flip comparison outcome",
            ))
            break
    return findings


def W224_multibit_boolean(tree, file, signals):
    """W224: Multi-bit signal used as boolean in if/while condition."""
    findings = []
    for cs in _find_nodes(tree.root_node, 'conditional_statement'):
        conds = _find_nodes(cs, 'cond_predicate')
        for c in conds:
            ids = _get_identifiers(c)
            text = _node_text(c).strip()
            if '==' in text or '!=' in text or '>' in text or '<' in text:
                continue
            if '&' in text or '|' in text or '~' in text or '^' in text:
                continue
            for i in ids:
                si = signals.get(i)
                if not si or si.width <= 1 or si.is_param:
                    continue
                # A bit/part-select (a_be[i], data[3:0]) narrows the operand —
                # it is not the full multi-bit signal used as a boolean.
                if re.search(rf'\b{re.escape(i)}\s*\[', text):
                    continue
                findings.append(Finding(
                    rule="W224_multibit_bool", severity="warning",
                    file=file, line=_node_line(c),
                    message=f"'{i}' ({si.width}-bit) used as boolean in if()",
                    synth_impact="Implicit OR-reduce: if(data) means if(|data)",
                ))
                break
    return findings


def W443_casex_usage(tree, file, signals):
    """W443: casex used instead of casez (X-matching is dangerous)."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bcasex\b', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="W443_casex", severity="warning",
            file=file, line=line,
            message="casex used: X-matching can mask bugs. Use casez or case inside",
            synth_impact="casex treats X as dont-care in both selector and items",
        ))
    return findings


def W456_many_ports(tree, file, signals):
    """W456: Module has too many ports (>20)."""
    findings = []
    port_count = sum(1 for s in signals.values()
                     if s.direction in ('input', 'output', 'inout'))
    if port_count > 20:
        findings.append(Finding(
            rule="W456_many_ports", severity="info",
            file=file, line=1,
            message=f"Module has {port_count} ports (>20): consider interface/struct",
            synth_impact="No synthesis impact, maintainability concern",
        ))
    return findings


def W468_arith_overflow(tree, file, signals):
    """W468: Arithmetic result may overflow the destination width."""
    findings = []
    for atype in ('blocking_assignment', 'nonblocking_assignment'):
        for asgn in _find_nodes(tree.root_node, atype):
            lhs = _get_lhs_signal(asgn)
            if not lhs:
                continue
            li = signals.get(lhs)
            if not li or li.width <= 0:
                continue
            children = asgn.named_children
            if len(children) < 2:
                continue
            rhs_text = _node_text(children[-1]).strip()
            rhs_clean = _strip_brackets(rhs_text)
            if _rhs_is_comparison(rhs_text):
                continue
            if '+' in rhs_clean or '*' in rhs_clean:
                rhs_ids = _get_identifiers(children[-1])
                max_rhs_w = 0
                for rid in rhs_ids:
                    ri = signals.get(rid)
                    if ri and ri.width > 0:
                        max_rhs_w = max(max_rhs_w, ri.width)
                if '*' in rhs_clean and max_rhs_w > 0:
                    needed = max_rhs_w * 2
                    if li.width < needed and li.width < max_rhs_w + 1:
                        findings.append(Finding(
                            rule="W468_arith_overflow", severity="warning",
                            file=file, line=_node_line(asgn),
                            message=f"'{lhs}'({li.width}b) may overflow: multiply needs {needed}b",
                            synth_impact="Truncated result: wrong arithmetic output",
                        ))
                elif '+' in rhs_clean and max_rhs_w > 0:
                    if li.width <= max_rhs_w and li.width < max_rhs_w + 1:
                        n_adds = rhs_clean.count('+')
                        if n_adds >= 2:
                            findings.append(Finding(
                                rule="W468_arith_overflow", severity="info",
                                file=file, line=_node_line(asgn),
                                message=f"'{lhs}'({li.width}b): {n_adds} additions, no extra bit for carry",
                                synth_impact="Carry may be lost in chained additions",
                            ))
    return findings


def W497_undriven_net(tree, file, signals):
    """W497: Internal net declared but never driven."""
    findings = []
    driven = set()
    for ntype in ('blocking_assignment', 'nonblocking_assignment',
                  'net_assignment', 'net_decl_assignment'):
        for n in _find_nodes(tree.root_node, ntype):
            sig = _get_lhs_signal(n)
            if sig:
                driven.add(sig)
            lhs_text = ''
            for child in n.named_children:
                if child.type in ('variable_lvalue', 'net_lvalue'):
                    lhs_text = _node_text(child)
                    break
            if '[' in lhs_text:
                base = lhs_text.split('[')[0].strip()
                if base:
                    driven.add(base)
    # tree-sitter parses array[idx] <= val as clocking_drive
    for cd in _find_nodes(tree.root_node, 'clocking_drive'):
        for cv in _find_nodes(cd, 'clockvar'):
            ids = _find_nodes(cv, 'simple_identifier')
            if ids:
                driven.add(ids[0].text.decode())
    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        for na in _find_nodes(ca, 'net_assignment'):
            sig = _get_lhs_signal(na)
            if sig:
                driven.add(sig)
    # Fallback: scan text for assign statements inside ERROR nodes
    all_text = _strip_comments(_node_text(tree.root_node))
    for m_assign in re.finditer(r'\bassign\s+(\w+)\s*=', all_text):
        driven.add(m_assign.group(1))

    for sig_name, si in signals.items():
        if si.direction in ('input', 'inout') or si.is_param:
            continue
        if si.direction == 'output':
            continue
        if sig_name not in driven:
            count = len(re.findall(rf'\b{re.escape(sig_name)}\b', all_text))
            if count > 1:
                findings.append(Finding(
                    rule="W497_undriven", severity="warning",
                    file=file, line=si.line,
                    message=f"Net '{sig_name}' read but never driven",
                    synth_impact="Undriven net: will be X at simulation, tied-off at synthesis",
                ))
    return findings


def W362_output_partial(tree, file, signals):
    """W362: Output not driven on all conditional paths (comb only)."""
    findings = []
    output_sigs = {n for n, s in signals.items() if s.direction == 'output'}
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) == 'always_ff':
            continue

        # Conditional/case scopes inside this block — an assignment contained in
        # one of these is only a *conditional* driver.
        scopes = (_find_nodes(always, 'conditional_statement')
                  + _find_nodes(always, 'case_statement'))

        driven_in_block = set()
        unconditionally_driven = set()
        for ntype in ('blocking_assignment', 'nonblocking_assignment'):
            for n in _find_nodes(always, ntype):
                sig = _get_lhs_signal(n)
                if not sig or sig not in output_sigs:
                    continue
                driven_in_block.add(sig)
                # A default assignment (not nested in any if/case) fully drives
                # the signal regardless of later conditional overrides.
                if not any(_is_inside(n, sc) for sc in scopes):
                    unconditionally_driven.add(sig)

        # Outputs with a default value are fully driven — no latch, no stale.
        partial_candidates = driven_in_block - unconditionally_driven
        if not partial_candidates:
            continue

        conds = _find_nodes(always, 'conditional_statement')
        for cs in conds:
            text = _node_text(cs)
            if '\nelse' not in text and ' else' not in text:
                for out in partial_candidates:
                    findings.append(Finding(
                        rule="W362_output_partial", severity="warning",
                        file=file, line=_node_line(cs),
                        message=f"Output '{out}' not driven on all if/else paths",
                        synth_impact="Output may hold stale value or infer latch",
                    ))
                break
    return findings


def W341_non_constant_case(tree, file, signals):
    """W341: Non-constant expression used as case item."""
    findings = []
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}
    # Collect enum member names (they are constants)
    text = _node_text(tree.root_node)
    enum_members = set()
    for em in re.finditer(r'typedef\s+enum\s+[^{]*\{([^}]+)\}', text):
        body = re.sub(r'//[^\n]*', '', em.group(1))
        for member in body.split(','):
            name = member.strip().split('=')[0].strip()
            if name and re.fullmatch(r'[a-zA-Z_]\w*', name):
                enum_members.add(name)
    # Collect localparam names
    for m in re.finditer(r'localparam\s+(?:\w+\s+)?(\w+)\s*=', text):
        params[m.group(1)] = 0
    constants = set(params) | enum_members

    for cs in _find_nodes(tree.root_node, 'case_statement'):
        for item in _find_direct_case_items(cs):
            item_text = _node_text(item).strip()
            if item_text.startswith('default'):
                continue
            colon = item_text.find(':')
            if colon < 0:
                continue
            val_text = item_text[:colon].strip()
            if re.fullmatch(r"[\d'hHbBdDoO_a-fA-FxXzZ\s]+", val_text):
                continue
            if val_text in constants:
                continue
            ids_in_val = re.findall(r'\b([a-zA-Z_]\w*)\b', val_text)
            non_const = [i for i in ids_in_val if i not in constants]
            if non_const:
                findings.append(Finding(
                    rule="W341_non_const_case", severity="info",
                    file=file, line=_node_line(item),
                    message=f"Case item '{val_text[:30]}' uses variable(s): "
                            f"{', '.join(non_const[:3])}",
                    synth_impact="Variable case item: creates priority-encoded mux, not simple decoder",
                ))
    return findings


# -------------------------------------------------------------------
# Category 12: ADDITIONAL SYNTH
# -------------------------------------------------------------------

def SYNTH_while_loop(tree, file, signals):
    """SYNTH_5006: while/repeat loops are not directly synthesizable."""
    findings = []
    for loop in _find_nodes(tree.root_node, 'loop_statement'):
        text = _node_text(loop)[:30]
        if text.strip().startswith('while') or text.strip().startswith('repeat'):
            findings.append(Finding(
                rule="SYNTH_5006_while", severity="error",
                file=file, line=_node_line(loop),
                message=f"'{text.split('(')[0].strip()}' loop is not synthesizable",
                synth_impact="Synthesis requires statically unrollable loops (for with constant bound)",
            ))
    return findings


def SYNTH_forever_loop(tree, file, signals):
    """SYNTH_5007: forever loop is simulation-only."""
    findings = []
    for loop in _find_nodes(tree.root_node, 'loop_statement'):
        text = _node_text(loop)[:20]
        if text.strip().startswith('forever'):
            findings.append(Finding(
                rule="SYNTH_5007_forever", severity="error",
                file=file, line=_node_line(loop),
                message="'forever' loop is simulation-only",
                synth_impact="Not synthesizable: use always block for repetitive logic",
            ))
    return findings


def SYNTH_for_non_constant(tree, file, signals):
    """SYNTH_5008: For loop with non-constant bound."""
    findings = []
    params = {s.name for s, si in signals.items() if signals[s].is_param}
    for loop in _find_nodes(tree.root_node, 'loop_statement'):
        text = _node_text(loop)
        if not text.strip().startswith('for'):
            continue
        m = re.search(r';\s*\w+\s*[<>!=]+\s*(\w+)', text)
        if m:
            bound = m.group(1)
            if not re.fullmatch(r'\d+', bound) and bound not in params:
                findings.append(Finding(
                    rule="SYNTH_5008_for_bound", severity="warning",
                    file=file, line=_node_line(loop),
                    message=f"For loop bound '{bound}' is not a constant",
                    synth_impact="Non-constant bound: synthesis cannot unroll loop",
                ))
    return findings


def SYNTH_string_type(tree, file, signals):
    """SYNTH_5009: string type is not synthesizable."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bstring\s+(\w+)', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SYNTH_5009_string", severity="error",
            file=file, line=line,
            message=f"'string {m.group(1)}' is not synthesizable",
            synth_impact="String type has no hardware equivalent",
        ))
    return findings


def SYNTH_time_type(tree, file, signals):
    """SYNTH_5010: time/realtime types not synthesizable."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\b(time|realtime)\s+(\w+)', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SYNTH_5010_time", severity="error",
            file=file, line=line,
            message=f"'{m.group(1)} {m.group(2)}' is not synthesizable",
            synth_impact="Time types are simulation-only",
        ))
    return findings


def SYNTH_class_usage(tree, file, signals):
    """SYNTH_5011: class/object is not synthesizable."""
    findings = []
    for n in _find_nodes(tree.root_node, 'class_declaration'):
        # First identifier in the class declaration is the class name;
        # skip any that are keywords like extends/implements targets
        ids = _find_nodes(n, 'simple_identifier')
        name = 'unknown'
        for cid in ids:
            candidate = _node_text(cid)
            if candidate not in ('extends', 'implements'):
                name = candidate
                break
        findings.append(Finding(
            rule="SYNTH_5011_class", severity="error",
            file=file, line=_node_line(n),
            message=f"Class '{name}' is not synthesizable",
            synth_impact="OOP constructs have no hardware mapping",
        ))
    return findings


def SYNTH_event_in_comb(tree, file, signals):
    """SYNTH_5007: Event control (@posedge/@negedge) inside always_comb is not synthesizable."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        atype = _always_type(always)
        if atype != 'always_comb':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            ev_text = _node_text(ev)
            if re.search(r'\b(posedge|negedge)\b', ev_text):
                findings.append(Finding(
                    rule="SYNTH_5007_event_in_comb", severity="error",
                    file=file, line=_node_line(ev),
                    message=f"Event control '{ev_text.strip()[:40]}' inside always_comb is not synthesizable",
                    synth_impact="Edge-sensitive events in combinational blocks have no hardware mapping",
                ))
    return findings


def SYNTH_recursive_func(tree, file, signals):
    """Recursive function calls are not synthesizable."""
    findings = []
    for func in _find_nodes(tree.root_node, 'function_declaration'):
        func_name = _func_task_name(func, signals)
        if not func_name:
            continue
        body = func
        for call in _find_nodes(body, 'tf_call'):
            call_ids = _find_nodes(call, 'simple_identifier')
            if call_ids and call_ids[0].text.decode() == func_name:
                findings.append(Finding(
                    rule="SYNTH_recursive_func", severity="error",
                    file=file, line=_node_line(call),
                    message=f"Recursive call to function '{func_name}' is not synthesizable",
                    synth_impact="Recursion requires dynamic stack allocation — no hardware equivalent",
                ))
    return findings


def SYNTH_unique_priority(tree, file, signals):
    """SYNTH_5012: unique/priority — all modern tools support these, no warning."""
    return []


def SYNTH_disable_iff(tree, file, signals):
    """SYNTH_5013: disable iff in assertions needs synth translate_off."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bdisable\s+iff\b', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SYNTH_5013_disable_iff", severity="info",
            file=file, line=line,
            message="'disable iff' assertion: ensure synthesis translate_off",
            synth_impact="Assertions must be excluded from synthesis netlist",
        ))
    return findings


# -------------------------------------------------------------------
# Category 13: ADDITIONAL FUNC
# -------------------------------------------------------------------

def FUNC_shift_overflow(tree, file, signals):
    """Shift amount may exceed signal width."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'(\w+)\s*(<<|>>)\s*(\w+)', text):
        sig_name, op, shift_val = m.group(1), m.group(2), m.group(3)
        si = signals.get(sig_name)
        if not si or si.width <= 0:
            continue
        if re.fullmatch(r'\d+', shift_val):
            sv = int(shift_val)
            if sv >= si.width:
                line = text[:m.start()].count('\n') + 1
                findings.append(Finding(
                    rule="FUNC_shift_overflow", severity="error",
                    file=file, line=line,
                    message=f"'{sig_name} {op} {sv}': shift >= width ({si.width}b)",
                    synth_impact="Result is always 0 (left shift) or 0 (right shift)",
                ))
    return findings


def FUNC_divide_power2(tree, file, signals):
    """Division/modulo by power-of-2 could use shift/mask."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)

    # A division whose dividend is compile-time constant folds at elaboration
    # and infers no divider hardware.
    elab = _current_elab(tree, signals)

    for m in re.finditer(r'(\w+)\s*([/%])\s*(\d+)', text):
        dividend = m.group(1)
        # Skip elaboration-time constant division (DATA_WIDTH/8, etc.)
        if elab.is_constant(dividend) or re.fullmatch(r'\d+', dividend):
            continue
        val = int(m.group(3))
        if val > 1 and (val & (val - 1)) == 0:
            line = text[:m.start()].count('\n') + 1
            op = 'Division' if m.group(2) == '/' else 'Modulo'
            shift = int(math.log2(val))
            findings.append(Finding(
                rule="FUNC_div_power2", severity="info",
                file=file, line=line,
                message=f"{op} by {val} (2^{shift}): use {'>> ' + str(shift) if op == 'Division' else 'bit mask'}",
                synth_impact=f"{op} synthesizes to divider; shift/mask is simpler",
            ))
    return findings


def FUNC_constant_if(tree, file, signals):
    """If condition is a constant (always true or always false)."""
    findings = []
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}
    for cs in _find_nodes(tree.root_node, 'conditional_statement'):
        conds = _find_nodes(cs, 'cond_predicate')
        for c in conds:
            text = _node_text(c).strip()
            if re.fullmatch(r'\d+', text):
                val = int(text)
                findings.append(Finding(
                    rule="FUNC_constant_if", severity="warning",
                    file=file, line=_node_line(c),
                    message=f"if({text}) is always {'TRUE' if val else 'FALSE'}",
                    synth_impact="Dead branch: synthesis optimizes out one path",
                ))
            elif re.fullmatch(r"1'b[01]", text):
                val = text[-1]
                findings.append(Finding(
                    rule="FUNC_constant_if", severity="warning",
                    file=file, line=_node_line(c),
                    message=f"if({text}) is always {'TRUE' if val == '1' else 'FALSE'}",
                    synth_impact="Dead branch: synthesis optimizes out one path",
                ))
    return findings


def FUNC_async_set_reset(tree, file, signals):
    """Both async set and async reset in same sensitivity list."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        events = _find_nodes(always, 'event_expression')
        async_sigs = set()
        for ev in events:
            text = _node_text(ev)
            for m_e in re.finditer(r'(?:pos|neg)edge\s+(\w+)', text):
                sig = m_e.group(1)
                if 'rst' in sig.lower() or 'reset' in sig.lower() or \
                   'set' in sig.lower() or 'clr' in sig.lower() or 'clear' in sig.lower():
                    async_sigs.add(sig)
        unique_async = sorted(async_sigs)
        if len(unique_async) >= 2:
            findings.append(Finding(
                rule="FUNC_async_set_reset", severity="warning",
                file=file, line=_node_line(always),
                message=f"Multiple async controls: {', '.join(unique_async)}",
                synth_impact="Async set+reset: priority depends on tool; STARC discourages",
            ))
    return findings


def FUNC_read_before_write(tree, file, signals):
    """Signal read before assigned in always block."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue
        first_read: dict[str, int] = {}
        first_write: dict[str, int] = {}
        for ba in _find_nodes(always, 'blocking_assignment'):
            lhs = _get_lhs_signal(ba)
            children = ba.named_children
            if len(children) >= 2:
                rhs_ids = _get_identifiers(children[-1])
                for r in rhs_ids:
                    if r not in first_read:
                        first_read[r] = ba.start_byte
            if lhs and lhs not in first_write:
                first_write[lhs] = ba.start_byte

        for sig in set(first_write) & set(first_read):
            si = signals.get(sig)
            if si and si.direction in ('input', 'inout'):
                continue
            if first_read[sig] < first_write[sig]:
                findings.append(Finding(
                    rule="FUNC_read_before_write", severity="warning",
                    file=file, line=_node_line(always),
                    message=f"'{sig}' read before written in comb block",
                    synth_impact="Uses previous value: may infer latch or cause race",
                ))
    return findings


def FUNC_truncation_assign(tree, file, signals):
    """Explicit truncation: assigning wider signal to narrower destination."""
    findings = []
    seen = set()
    for atype in ('blocking_assignment', 'nonblocking_assignment'):
        for asgn in _find_nodes(tree.root_node, atype):
            lhs = _get_lhs_signal(asgn)
            if not lhs or lhs in seen:
                continue
            li = signals.get(lhs)
            if not li or li.width <= 0:
                continue
            children = asgn.named_children
            if len(children) < 2:
                continue
            rhs_node = children[-1]
            rhs_text = _node_text(rhs_node).strip()
            if '[' in rhs_text:
                continue
            # Comparisons (==, !=, <, >, <=, >=) produce 1-bit result
            if re.search(r'[=!<>]=|[<>](?!=)', rhs_text):
                continue
            rhs_ids = _get_identifiers(rhs_node)
            for rid in rhs_ids:
                ri = signals.get(rid)
                if ri and ri.width > 0 and ri.width > li.width + 1:
                    seen.add(lhs)
                    findings.append(Finding(
                        rule="FUNC_truncation", severity="warning",
                        file=file, line=_node_line(asgn),
                        message=f"Truncation: '{rid}'({ri.width}b) -> '{lhs}'({li.width}b) "
                                f"loses {ri.width - li.width} MSBs",
                        synth_impact="Silent data loss: top bits discarded",
                    ))
                    break
    return findings


def FUNC_zero_width_concat(tree, file, signals):
    """Concatenation with zero-width or single-bit redundancy."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\{(\d+)\{', text):
        rep = int(m.group(1))
        if rep == 0:
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="FUNC_zero_replicate", severity="error",
                file=file, line=line,
                message=f"Zero replication {{{rep}{{...}}}}: result is empty",
                synth_impact="Zero-width result: synthesis error or unexpected behavior",
            ))
        elif rep == 1:
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="FUNC_redundant_replicate", severity="info",
                file=file, line=line,
                message=f"Replication {{1{{...}}}}: redundant, same as just {{...}}",
                synth_impact="No impact, but unnecessary syntax",
            ))
    return findings


# -------------------------------------------------------------------
# Category 14: ADDITIONAL CLK
# -------------------------------------------------------------------

def CLK_multiple_clocks(tree, file, signals):
    """Module uses multiple clock domains."""
    findings = []
    clk_sigs = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            for m_e in re.finditer(r'posedge\s+(\w+)', _node_text(ev)):
                sig = m_e.group(1)
                if 'rst' not in sig.lower() and 'reset' not in sig.lower():
                    clk_sigs.add(sig)
    if len(clk_sigs) >= 2:
        findings.append(Finding(
            rule="CLK_multi_domain", severity="warning",
            file=file, line=1,
            message=f"Module uses {len(clk_sigs)} clock domains: {', '.join(sorted(clk_sigs))}",
            synth_impact="CDC: signals crossing domains need synchronizers",
        ))
    return findings


def CLK_data_as_clock(tree, file, signals):
    """Non-clock signal used as clock (posedge on data signal)."""
    findings = []
    clk_like = set()
    for s_name in signals:
        if 'clk' in s_name.lower() or 'clock' in s_name.lower():
            clk_like.add(s_name)
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            for m_e in re.finditer(r'posedge\s+(\w+)', _node_text(ev)):
                sig = m_e.group(1)
                if 'rst' not in sig.lower() and 'reset' not in sig.lower():
                    if sig not in clk_like and not sig.startswith('clk'):
                        si = signals.get(sig)
                        if si and si.direction != 'input':
                            findings.append(Finding(
                                rule="CLK_data_as_clock", severity="warning",
                                file=file, line=_node_line(ev),
                                message=f"Internal signal '{sig}' used as clock edge",
                                synth_impact="Derived clock: glitch-prone without ICG cell",
                            ))
    return findings


def CLK_reset_polarity_mix(tree, file, signals):
    """Reset used with both active-high and active-low polarity."""
    findings = []
    rst_polarities: dict[str, set[str]] = {}
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            text = _node_text(ev)
            for m_e in re.finditer(r'(posedge|negedge)\s+(\w+)', text):
                edge, sig = m_e.group(1), m_e.group(2)
                if 'rst' in sig.lower() or 'reset' in sig.lower():
                    rst_polarities.setdefault(sig, set()).add(edge)
    for rst, edges in rst_polarities.items():
        if len(edges) >= 2:
            findings.append(Finding(
                rule="CLK_reset_polarity_mix", severity="error",
                file=file, line=1,
                message=f"Reset '{rst}' used with both posedge and negedge",
                synth_impact="Inconsistent reset polarity: unpredictable behavior",
            ))
    return findings


# -------------------------------------------------------------------
# Category 15: ADDITIONAL PWR
# -------------------------------------------------------------------

def PWR_wide_bus_no_enable(tree, file, signals):
    """Wide bus (>16 bit) register without enable."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        wide_regs = set()
        for nba in _find_nodes(always, 'nonblocking_assignment'):
            s = _get_lhs_signal(nba)
            if s:
                si = signals.get(s)
                if si and si.width > 16:
                    wide_regs.add(s)
        if not wide_regs:
            continue
        has_enable = False
        for cond in _find_nodes(always, 'conditional_statement'):
            m_if = re.search(r'if\s*\(([^)]+)\)', _node_text(cond))
            if m_if:
                cond_expr = m_if.group(1).lower()
                if 'rst' not in cond_expr and 'reset' not in cond_expr:
                    has_enable = True
                    break
        if not has_enable:
            for wr in wide_regs:
                findings.append(Finding(
                    rule="PWR_wide_no_enable", severity="info",
                    file=file, line=_node_line(always),
                    message=f"Wide reg '{wr}' ({signals[wr].width}b) always toggles: add enable",
                    synth_impact="High dynamic power: wide bus toggles every cycle",
                ))
    return findings


def PWR_constant_output(tree, file, signals):
    """Output port always driven to a constant value."""
    findings = []
    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        for na in _find_nodes(ca, 'net_assignment'):
            lhs = _get_lhs_signal(na)
            if not lhs:
                continue
            si = signals.get(lhs)
            if not si or si.direction != 'output':
                continue
            children = na.named_children
            if len(children) >= 2:
                rhs_text = _node_text(children[-1]).strip()
                if re.fullmatch(r"[\d'hHbBdDoO_a-fA-FxXzZ]+", rhs_text) or \
                   rhs_text in ('0', '1'):
                    findings.append(Finding(
                        rule="PWR_constant_output", severity="info",
                        file=file, line=_node_line(na),
                        message=f"Output '{lhs}' is constant '{rhs_text}': wasted pin",
                        synth_impact="Constant output: consider tying off at parent level",
                    ))
    return findings


def PWR_redundant_assign(tree, file, signals):
    """Same signal assigned same value in multiple branches."""
    findings = []
    for cs in _find_nodes(tree.root_node, 'case_statement'):
        items = _find_direct_case_items(cs)
        if len(items) < 3:
            continue
        sig_vals: dict[str, set[str]] = {}
        for item in items:
            for ba in _find_nodes(item, 'blocking_assignment'):
                sig = _get_lhs_signal(ba)
                children = ba.named_children
                if sig and len(children) >= 2:
                    val = _node_text(children[-1]).strip()
                    sig_vals.setdefault(sig, set()).add(val)
        for sig, vals in sig_vals.items():
            if len(vals) == 1 and len(items) >= 3:
                findings.append(Finding(
                    rule="PWR_redundant_assign", severity="info",
                    file=file, line=_node_line(cs),
                    message=f"'{sig}' assigned same value in all {len(items)} case arms",
                    synth_impact="Redundant mux: signal doesn't depend on case expression",
                ))
    return findings


# -------------------------------------------------------------------
# Category 16: STARC METHODOLOGY
# -------------------------------------------------------------------

def STARC_if_else_chain(tree, file, signals):
    """STARC: Long if/else-if chain (>4) should be case statement."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        for cs in _find_nodes(always, 'conditional_statement'):
            text = _node_text(cs)
            n_else_if = text.count('else if')
            if n_else_if >= 4:
                findings.append(Finding(
                    rule="STARC_if_chain", severity="info",
                    file=file, line=_node_line(cs),
                    message=f"if/else-if chain with {n_else_if+1} branches: use case",
                    synth_impact="Long if-chain: priority-encoded, case may be faster",
                ))
                break
    return findings


def STARC_register_output(tree, file, signals):
    """STARC: Module output driven directly from combinational logic."""
    findings = []
    reg_driven = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for nba in _find_nodes(always, 'nonblocking_assignment'):
            s = _get_lhs_signal(nba)
            if s:
                reg_driven.add(s)

    comb_driven = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue
        for ba in _find_nodes(always, 'blocking_assignment'):
            s = _get_lhs_signal(ba)
            if s:
                comb_driven.add(s)
    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        for na in _find_nodes(ca, 'net_assignment'):
            s = _get_lhs_signal(na)
            if s:
                comb_driven.add(s)

    for sig_name, si in signals.items():
        if si.direction != 'output':
            continue
        if sig_name in comb_driven and sig_name not in reg_driven:
            findings.append(Finding(
                rule="STYLE_comb_output", severity="info",
                file=file, line=si.line,
                message=f"Output '{sig_name}' is combinational: consider registering",
                synth_impact="Comb output: glitch-prone, timing depends on upstream logic",
            ))
    return findings


def STARC_no_casex(tree, file, signals):
    """STARC: casex should not be used."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bcasex\b', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="STARC_no_casex", severity="warning",
            file=file, line=line,
            message="STARC: casex is dangerous, use casez or case inside",
            synth_impact="casex X-matching hides bugs in simulation",
        ))
    return findings


def STARC_reset_constant(tree, file, signals):
    """STARC: Reset value should be a constant, not expression."""
    findings = []
    enum_members = set()
    text_all = _node_text(tree.root_node)
    for em in re.finditer(r'typedef\s+enum\s+[^{]*\{([^}]+)\}', text_all):
        for member in em.group(1).split(','):
            name = member.strip().split('=')[0].strip()
            if name:
                enum_members.add(name)
    param_names = {s for s, si in signals.items() if si.is_param}
    constants = param_names | enum_members

    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        conds = _find_nodes(always, 'conditional_statement')
        for cs in conds:
            text = _node_text(cs)
            m_if = re.search(r'if\s*\(!?\s*(\w+)', text)
            if not m_if:
                continue
            cond_sig = m_if.group(1)
            if 'rst' not in cond_sig.lower() and 'reset' not in cond_sig.lower():
                continue
            # Only check assignments in the reset (true) branch.
            # The true branch is the first child statement after the condition.
            reset_assigns = []
            for child in cs.named_children:
                if child.type == 'statement_or_null':
                    for atype in ('blocking_assignment', 'nonblocking_assignment'):
                        reset_assigns.extend(_find_nodes(child, atype))
                    break  # only the first statement_or_null is the true branch

            for asgn in reset_assigns[:5]:
                children = asgn.named_children
                if len(children) < 2:
                    continue
                rhs = _node_text(children[-1]).strip()
                if re.fullmatch(r"[\d'hHbBdDoO_a-fA-FxXzZ\s]+", rhs) or \
                   rhs in ('0', '1') or rhs in constants:
                    continue
                ids = _get_identifiers(children[-1])
                non_const = [i for i in ids if i not in constants]
                if non_const:
                    findings.append(Finding(
                        rule="STARC_reset_const", severity="info",
                        file=file, line=_node_line(asgn),
                        message=f"Reset value '{rhs[:30]}' is not a constant",
                        synth_impact="Non-constant reset: may not map to async reset cell",
                    ))
            break
    return findings


def STARC_one_always_one_signal(tree, file, signals):
    """STARC: Each sequential always block should drive only one signal."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        driven = set()
        for nba in _all_nb_assignments(always):
            s = _get_lhs_from_any(nba) if nba.type == 'clocking_drive' else _get_lhs_signal(nba)
            if s:
                driven.add(s)
        for ba in _find_nodes(always, 'blocking_assignment'):
            s = _get_lhs_signal(ba)
            if s:
                driven.add(s)
        if len(driven) > 8:
            findings.append(Finding(
                rule="STARC_many_regs", severity="info",
                file=file, line=_node_line(always),
                message=f"Sequential block drives {len(driven)} signals: consider splitting",
                synth_impact="Large always block: harder to constrain and optimize",
            ))
    return findings


# -------------------------------------------------------------------
# Category 17: ADDITIONAL STRUCT
# -------------------------------------------------------------------

def STRUCT_multiple_modules(tree, file, signals):
    """Multiple modules defined in one file."""
    findings = []
    mods = _find_nodes(tree.root_node, 'module_declaration')
    if len(mods) > 1:
        names = []
        for mod in mods:
            mn = _module_name(mod)
            if mn:
                names.append(mn)
        findings.append(Finding(
            rule="STRUCT_multi_module", severity="info",
            file=file, line=1,
            message=f"{len(mods)} modules in one file: {', '.join(names[:4])}",
            synth_impact="No impact, but one-module-per-file is standard practice",
        ))
    return findings


def STRUCT_deep_nesting(tree, file, signals):
    """Very deep begin/end or if/case nesting (>6 levels)."""
    findings = []

    def _depth(node, d=0):
        best = d
        for child in node.children:
            inc = 1 if child.type in ('seq_block', 'conditional_statement',
                                       'case_statement') else 0
            best = max(best, _depth(child, d + inc))
        return best

    for always in _find_nodes(tree.root_node, 'always_construct'):
        d = _depth(always)
        if d >= 6:
            findings.append(Finding(
                rule="STRUCT_deep_nesting", severity="warning",
                file=file, line=_node_line(always),
                message=f"Nesting depth {d}: hard to read and maintain",
                synth_impact="Deep nesting may create long combinational paths",
            ))
    return findings


def STRUCT_generate_no_label(tree, file, signals):
    """Generate block without label."""
    findings = []
    for gen in _find_nodes(tree.root_node, 'generate_region'):
        text = _strip_comments(_node_text(gen))
        # Labeled if any generate/for/if block carries `begin : <name>`.
        if re.search(r'\bbegin\s*:\s*\w+', text):
            continue
        findings.append(Finding(
            rule="STRUCT_gen_no_label", severity="info",
            file=file, line=_node_line(gen),
            message="Generate block without label: harder to reference in hierarchy",
            synth_impact="Unnamed generate: synthesis assigns auto-names",
        ))
    return findings


def STRUCT_positional_port(tree, file, signals):
    """Positional (ordered) port connections on module instantiation."""
    findings = []
    for inst in _find_nodes(tree.root_node, 'module_instantiation'):
        text = _node_text(inst)
        if '.' not in text and '(' in text:
            has_params = '#(' in text
            paren_text = text[text.find('(', text.find('(') + 1 if has_params else 0):]
            if paren_text and '.' not in paren_text:
                findings.append(Finding(
                    rule="STRUCT_positional_port", severity="warning",
                    file=file, line=_node_line(inst),
                    message="Positional port connections: use named (.port(sig)) instead",
                    synth_impact="Positional: wrong order silently connects wrong signals",
                ))
    return findings


def STRUCT_task_in_synth(tree, file, signals):
    """Task declaration in synthesizable module."""
    findings = []
    for task in _find_nodes(tree.root_node, 'task_declaration'):
        text = _node_text(task)
        if '#' in text or 'wait' in text or '@' in text:
            findings.append(Finding(
                rule="STRUCT_task_timing", severity="warning",
                file=file, line=_node_line(task),
                message="Task with timing controls: not synthesizable",
                synth_impact="Tasks with delays/waits are simulation-only",
            ))
    return findings


def STRUCT_function_void(tree, file, signals):
    """Function without return value assignment."""
    findings = []
    for func in _find_nodes(tree.root_node, 'function_declaration'):
        text = _node_text(func)
        ids = _find_nodes(func, 'simple_identifier')
        if ids:
            fname = _node_text(ids[0])
            if fname + ' =' not in text and 'return' not in text:
                findings.append(Finding(
                    rule="STRUCT_func_no_return", severity="warning",
                    file=file, line=_node_line(func),
                    message=f"Function '{fname}' may not assign return value",
                    synth_impact="Unassigned function return: X in simulation",
                ))
    return findings


def STRUCT_wire_reg_conflict(tree, file, signals):
    """Signal declared as both wire and reg."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    wires = set()
    regs = set()
    for m in re.finditer(r'\bwire\s+(?:\[[^\]]+\]\s*)?(\w+)', text):
        wires.add(m.group(1))
    for m in re.finditer(r'\breg\s+(?:\[[^\]]+\]\s*)?(\w+)', text):
        regs.add(m.group(1))
    for sig in wires & regs:
        findings.append(Finding(
            rule="STRUCT_wire_reg", severity="error",
            file=file, line=1,
            message=f"'{sig}' declared as both wire and reg",
            synth_impact="Type conflict: synthesis or simulation error",
        ))
    return findings


# -------------------------------------------------------------------
# Category 18: ADDITIONAL STYLE
# -------------------------------------------------------------------

def STYLE_param_uppercase(tree, file, signals):
    """Parameters should be UPPERCASE."""
    findings = []
    for sig_name, si in signals.items():
        if not si.is_param:
            continue
        if sig_name != sig_name.upper() and not sig_name[0].isupper():
            findings.append(Finding(
                rule="STYLE_param_case", severity="info",
                file=file, line=si.line,
                message=f"Parameter '{sig_name}' should be UPPERCASE",
                synth_impact="No impact, naming convention for readability",
            ))
    return findings


def STYLE_begin_end_single(tree, file, signals):
    """Single-statement if/else without begin/end."""
    findings = []
    count = 0
    for cs in _find_nodes(tree.root_node, 'conditional_statement'):
        text = _node_text(cs)
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        if len(lines) >= 2:
            for l in lines[1:3]:
                if l and not l.startswith('begin') and not l.startswith('end') \
                        and not l.startswith('else') and not l.startswith('if') \
                        and 'begin' not in lines[0]:
                    parent = cs.parent
                    if parent and parent.type != 'generate_region':
                        count += 1
                    break
    if count >= 3:
        findings.append(Finding(
            rule="STYLE_no_begin_end", severity="info",
            file=file, line=1,
            message=f"{count} if/else statement(s) without begin/end blocks",
            synth_impact="No impact, but omitting begin/end can cause bugs when adding lines",
        ))
    return findings


def STYLE_trailing_whitespace(tree, file, signals):
    """Trailing whitespace on lines."""
    findings = []
    text = _node_text(tree.root_node)
    count = sum(1 for line in text.split('\n') if line != line.rstrip())
    if count > 5:
        findings.append(Finding(
            rule="STYLE_trailing_ws", severity="info",
            file=file, line=1,
            message=f"{count} line(s) have trailing whitespace",
            synth_impact="No impact, code hygiene issue",
        ))
    return findings


def STYLE_port_direction_group(tree, file, signals):
    """Ports should be grouped by direction (inputs then outputs)."""
    findings = []
    dirs = []
    for sig_name, si in sorted(signals.items(), key=lambda x: x[1].line):
        if si.direction in ('input', 'output', 'inout'):
            dirs.append((si.direction, si.line))
    if len(dirs) < 4:
        return findings
    seen_output = False
    for d, line in dirs:
        if d == 'output':
            seen_output = True
        elif d == 'input' and seen_output:
            findings.append(Finding(
                rule="STYLE_port_order", severity="info",
                file=file, line=line,
                message="Input port declared after output: group by direction",
                synth_impact="No impact, readability convention",
            ))
            break
    return findings


# -------------------------------------------------------------------
# Category 19: ADDITIONAL SIM
# -------------------------------------------------------------------

def SIM_wait_statement(tree, file, signals):
    """wait() statement is simulation-only."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bwait\s*\(', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SIM_wait", severity="error",
            file=file, line=line,
            message="'wait()' is simulation-only, not synthesizable",
            synth_impact="Synthesis ignores wait: use clock-based state machine",
        ))
    return findings


def SIM_fork_join(tree, file, signals):
    """fork/join is simulation-only."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bfork\b', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SIM_fork_join", severity="error",
            file=file, line=line,
            message="'fork/join' is simulation-only",
            synth_impact="Parallel threads have no hardware mapping",
        ))
    return findings


def SIM_disable_statement(tree, file, signals):
    """disable statement is typically simulation-only."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bdisable\s+(?!iff)\w+', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SIM_disable", severity="warning",
            file=file, line=line,
            message=f"'disable' statement is typically simulation-only",
            synth_impact="Some synthesis tools support disable for loop control",
        ))
    return findings


def SIM_timeformat(tree, file, signals):
    """$timeformat is simulation-only."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(r'\$timeformat\b', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SIM_timeformat", severity="info",
            file=file, line=line,
            message="'$timeformat' is simulation-only",
            synth_impact="Synthesis ignores $timeformat",
        ))
    return findings


# -------------------------------------------------------------------
# Category 20: CROSS-MODULE / INSTANTIATION
# -------------------------------------------------------------------

def CROSS_unconnected_port(tree, file, signals):
    """Module instantiation with unconnected ports (empty parens)."""
    findings = []
    for inst in _find_nodes(tree.root_node, 'module_instantiation'):
        text = _node_text(inst)
        for m in re.finditer(r'\.(\w+)\s*\(\s*\)', text):
            port_name = m.group(1)
            line = _node_line(inst)
            findings.append(Finding(
                rule="CROSS_unconnected", severity="warning",
                file=file, line=line,
                message=f"Port '.{port_name}()' left unconnected in instantiation",
                synth_impact="Unconnected port: may cause warnings or missing connections",
            ))
    return findings


def CROSS_width_override(tree, file, signals):
    """Parameter override may change signal widths in instantiated module."""
    findings = []
    for inst in _find_nodes(tree.root_node, 'module_instantiation'):
        text = _node_text(inst)
        if '#(' in text:
            overrides = re.findall(r'\.(\w+)\s*\(\s*(\d+)\s*\)', text[:text.find(')')+1])
            for param, val in overrides:
                if 'WIDTH' in param.upper() or 'SIZE' in param.upper() or 'DEPTH' in param.upper():
                    findings.append(Finding(
                        rule="CROSS_width_override", severity="info",
                        file=file, line=_node_line(inst),
                        message=f"Parameter override '.{param}({val})' changes sizing",
                        synth_impact="Verify port widths match at integration level",
                    ))
    return findings


# -------------------------------------------------------------------
# Category 21: FSM (ADDITIONAL)
# -------------------------------------------------------------------

def _is_fsm_enum(type_name, tree):
    """Check if a typedef enum is used as a registered state variable (not just a decoder)."""
    text = _node_text(tree.root_node)
    decl_pat = re.compile(r'\b' + re.escape(type_name) + r'\s+(\w+(?:\s*,\s*\w+)*)\s*;')
    var_names = []
    for dm in decl_pat.finditer(text):
        var_names.extend(v.strip() for v in dm.group(1).split(','))
    if not var_names:
        return False
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        at = _node_text(always)
        for vn in var_names:
            if re.search(r'\b' + re.escape(vn) + r'\s*<=', at):
                return True
    return False


def FSM_unreachable_state(tree, file, signals):
    """FSM state not reachable from reset state (graph-based)."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(
            r'typedef\s+enum\s+logic\s*\[\d+:\d+\]\s*\{([^}]+)\}\s*(\w+)',
            text):
        states_text = re.sub(r'//[^\n]*', '', m.group(1))
        type_name = m.group(2)
        states = [s.strip().split('=')[0].strip()
                  for s in states_text.split(',')
                  if s.strip() and re.fullmatch(r'[a-zA-Z_]\w*',
                     s.strip().split('=')[0].strip())]
        if len(states) < 3:
            continue
        if not _is_fsm_enum(type_name, tree):
            continue

        # Build transition graph: state -> set of next states
        graph: dict[str, set[str]] = {s: set() for s in states}
        for cs in _find_nodes(tree.root_node, 'case_statement'):
            ce_nodes = [c for c in cs.named_children
                        if c.type == 'case_expression']
            if not ce_nodes:
                continue
            ce_ids = _get_identifiers(ce_nodes[0])
            if not any('state' in i.lower() or i in ('cs', 'ns') for i in ce_ids):
                continue
            for item in _find_direct_case_items(cs):
                item_text = _node_text(item).strip()
                colon = item_text.find(':')
                if colon < 0:
                    continue
                label = item_text[:colon].strip()
                if label not in graph:
                    continue
                for atype in ('blocking_assignment', 'nonblocking_assignment'):
                    for asgn in _find_nodes(item, atype):
                        target = asgn
                        oa = _find_nodes(asgn, 'operator_assignment')
                        if oa:
                            target = oa[0]
                        children = target.named_children
                        if len(children) >= 2:
                            rhs_ids = _extract_rhs_identifiers(children[-1])
                            for rid in rhs_ids:
                                if rid in graph:
                                    graph[label].add(rid)

        # Find reset state (first state assigned in reset branch or first enum member)
        reset_state = states[0]
        for always in _find_nodes(tree.root_node, 'always_construct'):
            if _always_type(always) != 'always_ff':
                continue
            at = _node_text(always)
            if 'rst' not in at.lower() and 'reset' not in at.lower():
                continue
            for atype in ('blocking_assignment', 'nonblocking_assignment'):
                for asgn in _find_nodes(always, atype):
                    children = asgn.named_children
                    if len(children) >= 2:
                        rhs_ids = _extract_rhs_identifiers(children[-1])
                        for rid in rhs_ids:
                            if rid in graph:
                                reset_state = rid
                                break

        # BFS from reset state
        reachable = set()
        queue = [reset_state]
        while queue:
            s = queue.pop(0)
            if s in reachable:
                continue
            reachable.add(s)
            for ns in graph.get(s, set()):
                if ns not in reachable:
                    queue.append(ns)

        line = text[:m.start()].count('\n') + 1
        for state in states:
            if state not in reachable:
                findings.append(Finding(
                    rule="FSM_unreachable", severity="warning",
                    file=file, line=line,
                    message=f"FSM state '{state}' not reachable from reset state '{reset_state}'",
                    synth_impact="Unreachable state: dead logic optimized away",
                ))
    return findings


def FSM_no_default_transition(tree, file, signals):
    """FSM case statement without default transition."""
    findings = []
    text = _node_text(tree.root_node)
    has_fsm = bool(re.search(r'typedef\s+enum', text))
    if not has_fsm:
        for m in re.finditer(r'\b(state|fsm_state|cs|current_state|next_state)\b', text):
            has_fsm = True
            break
    if not has_fsm:
        return findings

    for cs in _find_nodes(tree.root_node, 'case_statement'):
        ce_nodes = [c for c in cs.named_children
                    if c.type == 'case_expression']
        if not ce_nodes:
            continue
        ce_text = _node_text(ce_nodes[0]).strip()
        if 'state' in ce_text.lower() or 'fsm' in ce_text.lower() or \
           ce_text in ('cs', 'ns', 'current_state', 'next_state', 'state_q'):
            items = _find_direct_case_items(cs)
            has_default = any('default' in _node_text(i)[:20] for i in items)
            if not has_default:
                findings.append(Finding(
                    rule="FSM_no_default", severity="error",
                    file=file, line=_node_line(cs),
                    message=f"FSM case({ce_text}) has no default: may get stuck",
                    synth_impact="Missing default: FSM can enter illegal state permanently",
                ))
    return findings


# -------------------------------------------------------------------
# Category 22: ADDITIONAL MEMORY
# -------------------------------------------------------------------

def MEM_no_reset(tree, file, signals):
    """Memory array written in sequential block without reset."""
    findings = []
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}
    text = _node_text(tree.root_node)
    mem_sigs = set()
    for m in re.finditer(
            r'(?:logic|reg)\s*\[[^\]]+\]\s*(\w+)\s*\[', text):
        mem_sigs.add(m.group(1))

    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        at = _node_text(always)
        for mem in mem_sigs:
            if mem in at:
                if 'rst' not in at.lower() and 'reset' not in at.lower():
                    findings.append(Finding(
                        rule="MEM_no_reset", severity="info",
                        file=file, line=_node_line(always),
                        message=f"Memory '{mem}' written without reset initialization",
                        synth_impact="Memory contents unknown at power-up in ASIC",
                    ))
    return findings


def MEM_read_write_conflict(tree, file, signals):
    """Memory read and write in same always block without bypass."""
    findings = []
    text = _node_text(tree.root_node)
    mem_sigs = set()
    for m in re.finditer(
            r'(?:logic|reg)\s*\[[^\]]+\]\s*(\w+)\s*\[', text):
        mem_sigs.add(m.group(1))

    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        at = _node_text(always)
        for mem in mem_sigs:
            writes = len(re.findall(rf'{re.escape(mem)}\s*\[.*?\]\s*<=', at))
            reads = len(re.findall(rf'=\s*{re.escape(mem)}\s*\[', at))
            if writes > 0 and reads > 0:
                findings.append(Finding(
                    rule="MEM_rw_conflict", severity="info",
                    file=file, line=_node_line(always),
                    message=f"Memory '{mem}': read+write in same block, check read-during-write behavior",
                    synth_impact="Read-during-write: old-data vs new-data depends on synthesis",
                ))
    return findings


# -------------------------------------------------------------------
# Category 23: MORE W-SERIES
# -------------------------------------------------------------------

def W116_inout_usage(tree, file, signals):
    """W116: Inout port usage (bidirectional bus)."""
    findings = []
    for sig_name, si in signals.items():
        if si.direction == 'inout':
            findings.append(Finding(
                rule="W116_inout", severity="info",
                file=file, line=si.line,
                message=f"Inout port '{sig_name}': bidirectional bus",
                synth_impact="Inout requires tri-state: only at chip-level pads",
            ))
    return findings


def W484_operator_precedence(tree, file, signals):
    """W484: Operator precedence ambiguity (missing parentheses)."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'(\w+)\s*&\s*(\w+)\s*\|\s*(\w+)', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="W484_precedence", severity="warning",
            file=file, line=line,
            message=f"'a & b | c' without parentheses: & binds tighter than |",
            synth_impact="Ambiguous precedence: add parentheses for clarity",
        ))
    for m in re.finditer(r'(\w+)\s*\|\s*(\w+)\s*&\s*(\w+)', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="W484_precedence", severity="warning",
            file=file, line=line,
            message=f"'a | b & c' without parentheses: & binds tighter than |",
            synth_impact="Ambiguous precedence: add parentheses for clarity",
        ))
    return findings


def W529_port_width_connect(tree, file, signals):
    """W529: Port connection width mismatch in module instantiation."""
    findings = []
    for inst in _find_nodes(tree.root_node, 'module_instantiation'):
        text = _node_text(inst)
        for m in re.finditer(r'\.(\w+)\s*\(\s*(\w+)\s*\)', text):
            port_name, sig_name = m.group(1), m.group(2)
            si = signals.get(sig_name)
            if not si or si.width <= 0:
                continue
            pi = signals.get(port_name)
            if pi and pi.width > 0 and pi.width != si.width:
                findings.append(Finding(
                    rule="W529_port_width", severity="warning",
                    file=file, line=_node_line(inst),
                    message=f"Port '.{port_name}'({pi.width}b) connected to "
                            f"'{sig_name}'({si.width}b): width mismatch",
                    synth_impact="Implicit truncation or extension at port boundary",
                ))
    return findings


def W293_block_label_mismatch(tree, file, signals):
    """W293: Named block begin/end labels don't match."""
    findings = []
    for sb in _find_nodes(tree.root_node, 'seq_block'):
        text = _node_text(sb)
        begins = re.findall(r'begin\s*:\s*(\w+)', text)
        ends = re.findall(r'end\s*:\s*(\w+)', text)
        if begins and ends:
            if begins[0] != ends[-1]:
                findings.append(Finding(
                    rule="W293_label_mismatch", severity="warning",
                    file=file, line=_node_line(sb),
                    message=f"Block label mismatch: begin:{begins[0]} vs end:{ends[-1]}",
                    synth_impact="No synthesis impact, but confusing and error-prone",
                ))
    return findings


def W182_signal_redeclared(tree, file, signals):
    """W182: Signal name declared multiple times."""
    findings = []
    text = _node_text(tree.root_node)
    text_clean = re.sub(r'//.*?$', '', text, flags=re.MULTILINE)
    decl_count: dict[str, list[int]] = {}
    for m in re.finditer(
            r'\b(?:logic|reg|wire)\s+(?:\[[^\]]+\]\s*)?(\w+)\s*[;,=\[]', text_clean):
        name = m.group(1)
        if name in ('input', 'output', 'inout', 'logic', 'reg', 'wire'):
            continue
        line = text_clean[:m.start()].count('\n') + 1
        decl_count.setdefault(name, []).append(line)
    for name, lines in decl_count.items():
        if len(lines) >= 2:
            findings.append(Finding(
                rule="W182_redeclared", severity="warning",
                file=file, line=lines[0],
                message=f"'{name}' declared {len(lines)} times (lines {', '.join(str(l) for l in lines[:3])})",
                synth_impact="Multiple declarations: may shadow or conflict",
            ))
    return findings


def W192_unused_function(tree, file, signals):
    """W192: Function or task declared but never called."""
    findings = []
    text = _node_text(tree.root_node)
    param_names = {n for n, s in signals.items() if s.is_param}
    for func in _find_nodes(tree.root_node, 'function_declaration'):
        # Get function name from function_body_declaration child
        fname = None
        for child in func.named_children:
            if child.type == 'function_body_declaration':
                body_ids = _find_nodes(child, 'simple_identifier')
                for bid in body_ids:
                    name = _node_text(bid)
                    if name not in param_names:
                        fname = name
                        break
                break
        if not fname:
            ids = _find_nodes(func, 'simple_identifier')
            for i in ids:
                name = _node_text(i)
                if name not in param_names:
                    fname = name
                    break
        if fname:
            calls = len(re.findall(rf'\b{re.escape(fname)}\s*\(', text))
            if calls <= 1:
                findings.append(Finding(
                    rule="W192_unused_func", severity="info",
                    file=file, line=_node_line(func),
                    message=f"Function '{fname}' declared but never called",
                    synth_impact="Dead code: synthesis ignores uncalled functions",
                ))
    for task in _find_nodes(tree.root_node, 'task_declaration'):
        tname = _func_task_name(task, signals)
        if tname:
            calls = len(re.findall(rf'\b{re.escape(tname)}\b', text))
            if calls <= 1:
                findings.append(Finding(
                    rule="W192_unused_task", severity="info",
                    file=file, line=_node_line(task),
                    message=f"Task '{tname}' declared but never called",
                    synth_impact="Dead code: synthesis ignores uncalled tasks",
                ))
    return findings


# -------------------------------------------------------------------
# Category 24: MORE FUNC
# -------------------------------------------------------------------

def FUNC_compare_self(tree, file, signals):
    """Signal compared to itself (a == a is always true)."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'(\w+)\s*==\s*(\w+)', text):
        if m.group(1) == m.group(2):
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="FUNC_compare_self", severity="warning",
                file=file, line=line,
                message=f"'{m.group(1)} == {m.group(2)}' is always TRUE",
                synth_impact="Constant condition: dead branch eliminated",
            ))
    for m in re.finditer(r'(\w+)\s*!=\s*(\w+)', text):
        if m.group(1) == m.group(2):
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="FUNC_compare_self", severity="warning",
                file=file, line=line,
                message=f"'{m.group(1)} != {m.group(2)}' is always FALSE",
                synth_impact="Constant condition: dead branch eliminated",
            ))
    return findings


def FUNC_reduction_1bit(tree, file, signals):
    """Reduction operator on 1-bit signal (no-op)."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(r'([&|^~])\s*([a-zA-Z_]\w*)', text):
        op, sig = m.group(1), m.group(2)
        si = signals.get(sig)
        if si and si.width == 1 and not si.is_param:
            pos = m.start() - 1
            while pos >= 0 and text[pos] in ' \t':
                pos -= 1
            if pos >= 0 and text[pos] not in '=({,?:;':
                continue
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="FUNC_reduction_1bit", severity="info",
                file=file, line=line,
                message=f"Reduction '{op}{sig}' on 1-bit signal: no-op",
                synth_impact="No impact, but suggests width confusion",
            ))
    return findings


def FUNC_bitwise_vs_logical(tree, file, signals):
    """Single & or | where && or || likely intended in if condition."""
    findings = []
    for cs in _find_nodes(tree.root_node, 'conditional_statement'):
        conds = _find_nodes(cs, 'cond_predicate')
        for c in conds:
            text = _node_text(c)
            cleaned = text.replace('&&', '').replace('||', '').replace('&', '', 0)
            if re.search(r'[^&]&[^&]', text) and '==' in text:
                findings.append(Finding(
                    rule="FUNC_bitwise_logical", severity="warning",
                    file=file, line=_node_line(c),
                    message=f"Single '&' in condition: did you mean '&&'?",
                    synth_impact="Bitwise & vs logical &&: different semantics for multi-bit",
                ))
                break
            if re.search(r'[^|]\|[^|]', text) and '==' in text:
                findings.append(Finding(
                    rule="FUNC_bitwise_logical", severity="warning",
                    file=file, line=_node_line(c),
                    message=f"Single '|' in condition: did you mean '||'?",
                    synth_impact="Bitwise | vs logical ||: different semantics for multi-bit",
                ))
                break
    return findings


def _underflow_guard_text(asgn) -> str:
    """Concatenated text of all if/case conditions enclosing an assignment —
    used to detect a guard that prevents underflow (`!empty`, `count > 0`)."""
    parts = []
    node = asgn.parent
    while node is not None and node.type != 'always_construct':
        if node.type == 'conditional_statement':
            for cp in _find_nodes(node, 'cond_predicate'):
                parts.append(_node_text(cp))
                break
        elif node.type in ('case_item', 'case_statement'):
            for ce in _find_nodes(node, 'case_expression'):
                parts.append(_node_text(ce))
                break
        node = node.parent
    return ' '.join(parts)


def FUNC_unsigned_subtraction(tree, file, signals):
    """Unsigned subtraction may underflow (wrap around)."""
    findings = []
    for atype in ('blocking_assignment', 'nonblocking_assignment'):
        for asgn in _find_nodes(tree.root_node, atype):
            lhs = _get_lhs_signal(asgn)
            if not lhs:
                continue
            children = asgn.named_children
            if len(children) < 2:
                continue
            rhs_text = _node_text(children[-1]).strip()
            rhs_no_brackets = re.sub(r'\[[^\]]*\]', '', rhs_text)
            if '-' in rhs_no_brackets and '+' not in rhs_no_brackets:
                li = signals.get(lhs)
                if li and li.width > 0 and li.width <= 8:
                    text = _node_text(tree.root_node)
                    is_signed = bool(re.search(
                        rf'\bsigned\b[^;]*\b{re.escape(lhs)}\b', text))
                    # Guarded decrement (e.g. `if (rd_en && !empty)` or a case
                    # arm meaning not-empty) cannot underflow in practice.
                    guard = _underflow_guard_text(asgn)
                    guarded = bool(re.search(
                        r'\bempty\b|!=\s*0|!=\s*\'0|>\s*0|>=\s*1', guard))
                    if not is_signed and not guarded:
                        findings.append(Finding(
                            rule="FUNC_unsigned_sub", severity="info",
                            file=file, line=_node_line(asgn),
                            message=f"'{lhs}' ({li.width}b unsigned) = ... - ...: may underflow",
                            synth_impact="Unsigned underflow wraps to max value",
                        ))
                        break
    return findings


def FUNC_full_case_overlap(tree, file, signals):
    """Case items with overlapping wildcard patterns (casez)."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bcasez\b', text):
        line = text[:m.start()].count('\n') + 1
        cs_text = text[m.start():]
        end = cs_text.find('endcase')
        if end < 0:
            continue
        cs_text = cs_text[:end]
        patterns = re.findall(r"(\d+'b[01?zZ]+)", cs_text)
        if len(patterns) >= 2:
            for i, p1 in enumerate(patterns):
                for p2 in patterns[i+1:]:
                    if len(p1) == len(p2):
                        overlap = True
                        for c1, c2 in zip(p1, p2):
                            if c1 in '?zZ' or c2 in '?zZ':
                                continue
                            if c1 != c2:
                                overlap = False
                                break
                        if overlap and p1 != p2:
                            findings.append(Finding(
                                rule="FUNC_casez_overlap", severity="warning",
                                file=file, line=line,
                                message=f"casez patterns '{p1}' and '{p2}' overlap",
                                synth_impact="Overlapping: priority-dependent, not parallel",
                            ))
                            return findings
    return findings


def FUNC_index_variable_width(tree, file, signals):
    """Array index variable width may not cover full array depth."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}
    mem_sigs: dict[str, int] = {}
    for m in re.finditer(
            r'(?:logic|reg)\s*\[[^\]]+\]\s*(\w+)\s*\[([^\]]+?)(?::([^\]]+))?\]',
            text):
        name = m.group(1)
        arr_hi = _eval_param_expr(m.group(2), params)
        arr_lo = _eval_param_expr(m.group(3), params) if m.group(3) else None
        if arr_hi is not None:
            depth = abs(arr_hi - (arr_lo or 0)) + 1 if arr_lo is not None else arr_hi
            mem_sigs[name] = depth

    for m in re.finditer(r'(\w+)\s*\[\s*(\w+)\s*\]', text):
        mem_name, idx_name = m.group(1), m.group(2)
        if mem_name not in mem_sigs:
            continue
        depth = mem_sigs[mem_name]
        idx_si = signals.get(idx_name)
        if not idx_si or idx_si.width <= 0:
            continue
        max_idx = (1 << idx_si.width) - 1
        if max_idx < depth - 1:
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="FUNC_idx_narrow", severity="warning",
                file=file, line=line,
                message=f"Index '{idx_name}'({idx_si.width}b, max={max_idx}) "
                        f"can't reach full depth of '{mem_name}'[{depth}]",
                synth_impact="Upper array entries unreachable: dead memory",
            ))
            break
    return findings


# -------------------------------------------------------------------
# Category 25: MORE CLK
# -------------------------------------------------------------------

def CLK_negedge_data(tree, file, signals):
    """Data latched on negedge clock (unusual convention)."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            text = _node_text(ev)
            for m_e in re.finditer(r'negedge\s+(\w+)', text):
                sig = m_e.group(1)
                if 'clk' in sig.lower() or 'clock' in sig.lower():
                    findings.append(Finding(
                        rule="CLK_negedge_data", severity="info",
                        file=file, line=_node_line(ev),
                        message=f"Data latched on negedge '{sig}': unusual convention",
                        synth_impact="Negedge clocking: verify timing constraints match",
                    ))
    return findings


def CLK_clock_divider(tree, file, signals):
    """Clock divider: toggling a signal creates derived clock."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for nba in _find_nodes(always, 'nonblocking_assignment'):
            lhs = _get_lhs_signal(nba)
            if not lhs:
                continue
            children = nba.named_children
            if len(children) >= 2:
                rhs = _node_text(children[-1]).strip()
                if rhs == f'~{lhs}' or rhs == f'!{lhs}':
                    findings.append(Finding(
                        rule="CLK_generated", severity="info",
                        file=file, line=_node_line(nba),
                        message=f"'{lhs} <= ~{lhs}': generated clock "
                                f"(toggle-FF divide-by-2 from parent clock)",
                        synth_impact="Generated clock domain: constrain with "
                                     "create_generated_clock; drive downstream "
                                     "logic on this derived clock",
                    ))
    return findings


# -------------------------------------------------------------------
# Category 26: MORE SYNTH
# -------------------------------------------------------------------

def SYNTH_program_block(tree, file, signals):
    """program block is verification-only, not synthesizable."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(r'\bprogram\s+(\w+)', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SYNTH_program", severity="error",
            file=file, line=line,
            message=f"'program {m.group(1)}' is verification-only",
            synth_impact="Program blocks have no hardware mapping",
        ))
    return findings


def SYNTH_chandle_type(tree, file, signals):
    """chandle type is not synthesizable."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(r'\bchandle\s+(\w+)', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SYNTH_chandle", severity="error",
            file=file, line=line,
            message=f"'chandle {m.group(1)}' is not synthesizable",
            synth_impact="C handle type: DPI-only, no hardware mapping",
        ))
    return findings


def SYNTH_semaphore(tree, file, signals):
    """semaphore/mailbox are verification-only."""
    findings = []
    text = _node_text(tree.root_node)
    for kw in ['semaphore', 'mailbox']:
        for m in re.finditer(rf'\b{kw}\s+(\w+)', text):
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="SYNTH_" + kw, severity="error",
                file=file, line=line,
                message=f"'{kw} {m.group(1)}' is verification-only",
                synth_impact=f"{kw}: no hardware equivalent",
            ))
    return findings


def SYNTH_covergroup(tree, file, signals):
    """covergroup is verification-only."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(r'\bcovergroup\s+(\w+)', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SYNTH_covergroup", severity="info",
            file=file, line=line,
            message=f"'covergroup {m.group(1)}': verification-only construct",
            synth_impact="Coverage: must be excluded from synthesis",
        ))
    return findings


def SYNTH_assert_no_translate(tree, file, signals):
    """Assertion without synthesis translate_off pragma."""
    findings = []
    text = _node_text(tree.root_node)
    has_translate_off = 'translate_off' in text or 'synthesis off' in text
    for m in re.finditer(r'\b(assert|assume|cover)\s+property\b', text):
        if not has_translate_off:
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="SYNTH_assert_pragma", severity="info",
                file=file, line=line,
                message=f"'{m.group(1)} property' without translate_off pragma",
                synth_impact="Some tools synthesize assertions as checkers",
            ))
    return findings


# -------------------------------------------------------------------
# Category 27: MORE STYLE
# -------------------------------------------------------------------

def STYLE_wildcard_import(tree, file, signals):
    """Wildcard package import (import pkg::*)."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(r'\bimport\s+(\w+)::\*', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="STYLE_wildcard_import", severity="info",
            file=file, line=line,
            message=f"Wildcard import '{m.group(0)}': import specific items instead",
            synth_impact="No impact, but pollutes namespace and hides dependencies",
        ))
    return findings


def STYLE_magic_delay(tree, file, signals):
    """#delay with magic number instead of parameter."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(r'#(\d+)\b', text):
        val = int(m.group(1))
        if val > 1:
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="STYLE_magic_delay", severity="info",
                file=file, line=line,
                message=f"#delay with magic number #{val}: use parameter",
                synth_impact="Not synthesizable regardless, but use parameter for sim consistency",
            ))
    return findings


def STYLE_consistent_reset(tree, file, signals):
    """Multiple different reset signal names in same module."""
    findings = []
    reset_names = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            text = _node_text(ev)
            for m_e in re.finditer(r'(?:pos|neg)edge\s+(\w+)', text):
                sig = m_e.group(1)
                if 'rst' in sig.lower() or 'reset' in sig.lower():
                    reset_names.add(sig)
    if len(reset_names) >= 2:
        findings.append(Finding(
            rule="STYLE_reset_names", severity="info",
            file=file, line=1,
            message=f"Multiple reset names: {', '.join(sorted(reset_names))}",
            synth_impact="Inconsistent naming: verify all are the same reset tree",
        ))
    return findings


def STYLE_endmodule_comment(tree, file, signals):
    """endmodule without module name comment."""
    findings = []
    text = _node_text(tree.root_node)
    mods = _find_nodes(tree.root_node, 'module_declaration')
    for mod in mods:
        mod_text = _node_text(mod)
        mod_name = _module_name(mod)
        if not mod_name:
            continue
        lines = mod_text.rstrip().split('\n')
        last_line = lines[-1].strip() if lines else ''
        if last_line == 'endmodule':
            findings.append(Finding(
                rule="STYLE_endmod_label", severity="info",
                file=file, line=_node_line(mod) + len(lines) - 1,
                message=f"endmodule without comment: add // {mod_name}",
                synth_impact="No impact, readability for large files",
            ))
    return findings


# -------------------------------------------------------------------
# Category 28: MORE STRUCT
# -------------------------------------------------------------------

def STRUCT_empty_module(tree, file, signals):
    """Module with no always blocks, assigns, or instantiations."""
    findings = []
    for mod in _find_nodes(tree.root_node, 'module_declaration'):
        has_logic = bool(
            _find_nodes(mod, 'always_construct') or
            _find_nodes(mod, 'continuous_assign') or
            _find_nodes(mod, 'module_instantiation'))
        if not has_logic:
            port_count = sum(1 for s in signals.values()
                             if s.direction in ('input', 'output', 'inout'))
            if port_count > 0:
                findings.append(Finding(
                    rule="STRUCT_empty_module", severity="warning",
                    file=file, line=_node_line(mod),
                    message="Module has ports but no logic, assigns, or instantiations",
                    synth_impact="Empty module: outputs will be undriven",
                ))
    return findings


def STRUCT_recursive_instance(tree, file, signals):
    """Module instantiates itself (direct recursion)."""
    findings = []
    mods = _find_nodes(tree.root_node, 'module_declaration')
    for mod in mods:
        mod_name = _module_name(mod)
        if not mod_name:
            continue
        for inst in _find_nodes(mod, 'module_instantiation'):
            inst_ids = _find_nodes(inst, 'simple_identifier')
            if inst_ids and _node_text(inst_ids[0]) == mod_name:
                findings.append(Finding(
                    rule="STRUCT_recursive", severity="error",
                    file=file, line=_node_line(inst),
                    message=f"Module '{mod_name}' instantiates itself",
                    synth_impact="Recursive instantiation: infinite elaboration",
                ))
    return findings


def STRUCT_ifdef_balance(tree, file, signals):
    """Unbalanced `ifdef/`endif."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    opens = len(re.findall(r'`ifdef\b', text)) + len(re.findall(r'`ifndef\b', text))
    closes = len(re.findall(r'`endif\b', text))
    if opens != closes:
        findings.append(Finding(
            rule="STRUCT_ifdef_balance", severity="error",
            file=file, line=1,
            message=f"Unbalanced preprocessor: {opens} `ifdef/`ifndef vs {closes} `endif",
            synth_impact="Missing `endif: compilation error or wrong code included",
        ))
    return findings


def STRUCT_timescale(tree, file, signals):
    """`timescale directive in RTL file."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(r'`timescale\b', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="STRUCT_timescale", severity="info",
            file=file, line=line,
            message="`timescale in RTL: synthesis ignores, use separate file",
            synth_impact="Synthesis ignores `timescale: place in testbench only",
        ))
    return findings


def STRUCT_implicit_net(tree, file, signals):
    """Implicit net type (default_nettype not set to none)."""
    findings = []
    text = _node_text(tree.root_node)
    if '`default_nettype' not in text:
        has_instances = bool(_find_nodes(tree.root_node, 'module_instantiation'))
        if has_instances:
            findings.append(Finding(
                rule="STRUCT_implicit_net", severity="info",
                file=file, line=1,
                message="No `default_nettype none: implicit wires can hide typos",
                synth_impact="Typos in port names silently create 1-bit wires",
            ))
    return findings


# -------------------------------------------------------------------
# Category 29: MORE FSM
# -------------------------------------------------------------------

def FSM_dead_state(tree, file, signals):
    """FSM state that only transitions to itself (infinite loop)."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(
            r'typedef\s+enum\s+logic\s*\[\d+:\d+\]\s*\{([^}]+)\}\s*(\w+)',
            text):
        states_text = re.sub(r'//[^\n]*', '', m.group(1))
        type_name = m.group(2)
        states = [s.strip().split('=')[0].strip()
                  for s in states_text.split(',')
                  if s.strip() and re.fullmatch(r'[a-zA-Z_]\w*',
                     s.strip().split('=')[0].strip())]
        if len(states) < 3:
            continue
        if not _is_fsm_enum(type_name, tree):
            continue

        # Build full transition graph (including self-loops)
        graph: dict[str, set[str]] = {s: set() for s in states}
        for cs in _find_nodes(tree.root_node, 'case_statement'):
            ce_nodes = [c for c in cs.named_children
                        if c.type == 'case_expression']
            if not ce_nodes:
                continue
            ce_ids = _get_identifiers(ce_nodes[0])
            if not any('state' in i.lower() or i in ('cs', 'ns') for i in ce_ids):
                continue
            for item in _find_direct_case_items(cs):
                item_text = _node_text(item).strip()
                colon = item_text.find(':')
                if colon < 0:
                    continue
                label = item_text[:colon].strip()
                if label not in graph:
                    continue
                for atype in ('blocking_assignment', 'nonblocking_assignment'):
                    for asgn in _find_nodes(item, atype):
                        target = asgn
                        oa = _find_nodes(asgn, 'operator_assignment')
                        if oa:
                            target = oa[0]
                        children = target.named_children
                        if len(children) >= 2:
                            rhs_ids = _extract_rhs_identifiers(children[-1])
                            for rid in rhs_ids:
                                if rid in graph:
                                    graph[label].add(rid)

        line = text[:m.start()].count('\n') + 1
        for state, nexts in graph.items():
            # Dead state: only exit is back to itself (or no exits at all)
            exits_to_other = nexts - {state}
            if not exits_to_other and nexts:
                findings.append(Finding(
                    rule="FSM_dead_state", severity="warning",
                    file=file, line=line,
                    message=f"FSM state '{state}' only loops to itself (dead-end)",
                    synth_impact="Infinite loop: FSM gets stuck in this state permanently",
                ))
    return findings


def FSM_no_idle_reset(tree, file, signals):
    """FSM without reset to initial/idle state."""
    findings = []
    text = _node_text(tree.root_node)
    has_enum = bool(re.search(r'typedef\s+enum', text))
    if not has_enum:
        return findings

    has_fsm_reset = False
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        at = _node_text(always)
        if ('rst' in at.lower() or 'reset' in at.lower()) and \
           ('state' in at.lower() or 'fsm' in at.lower()):
            has_fsm_reset = True
            break

    if not has_fsm_reset:
        for m in re.finditer(r'typedef\s+enum\s+[^{]*\{[^}]+\}\s*(\w+)', text):
            if not _is_fsm_enum(m.group(1), tree):
                continue
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="FSM_no_idle_reset", severity="warning",
                file=file, line=line,
                message=f"FSM '{m.group(1)}' has no reset to initial state",
                synth_impact="FSM starts in unknown state in ASIC without reset",
            ))
    return findings


# -------------------------------------------------------------------
# Category 30: MORE MEM
# -------------------------------------------------------------------

def MEM_async_read(tree, file, signals):
    """Memory with asynchronous read (not clocked)."""
    findings = []
    text = _node_text(tree.root_node)
    mem_sigs = set()
    for m in re.finditer(
            r'(?:logic|reg)\s*\[[^\]]+\]\s*(\w+)\s*\[', text):
        mem_sigs.add(m.group(1))

    for mem in mem_sigs:
        in_comb = False
        for always in _find_nodes(tree.root_node, 'always_construct'):
            if _always_type(always) != 'always_comb':
                continue
            at = _node_text(always)
            if re.search(rf'=\s*{re.escape(mem)}\s*\[', at):
                in_comb = True
                break
        for ca in _find_nodes(tree.root_node, 'continuous_assign'):
            ct = _node_text(ca)
            if re.search(rf'{re.escape(mem)}\s*\[', ct):
                in_comb = True
                break
        if in_comb:
            findings.append(Finding(
                rule="MEM_async_read", severity="info",
                file=file, line=1,
                message=f"Memory '{mem}' read asynchronously: infers distributed RAM or LUT-RAM",
                synth_impact="Async read: cannot use block RAM (BRAM), uses LUTs instead",
            ))
    return findings


# -------------------------------------------------------------------
# Category 31: MORE STARC
# -------------------------------------------------------------------

def STARC_sync_reset_preference(tree, file, signals):
    """STARC: Synchronous reset preferred for data path registers."""
    findings = []
    data_async = 0
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        events = _find_nodes(always, 'event_expression')
        is_async = False
        for ev in events:
            et = _node_text(ev)
            for m_e in re.finditer(r'(?:pos|neg)edge\s+(\w+)', et):
                sig = m_e.group(1)
                if 'rst' in sig.lower() or 'reset' in sig.lower():
                    is_async = True
        if is_async:
            nbas = _find_nodes(always, 'nonblocking_assignment')
            data_sigs = set()
            for nba in nbas:
                s = _get_lhs_signal(nba)
                if s and 'state' not in s.lower() and 'fsm' not in s.lower():
                    data_sigs.add(s)
            if len(data_sigs) >= 4:
                data_async += 1
    if data_async >= 2:
        findings.append(Finding(
            rule="STARC_sync_reset", severity="info",
            file=file, line=1,
            message=f"{data_async} blocks use async reset for data regs: consider sync reset",
            synth_impact="Async reset on data: adds routing, sync reset is smaller for data path",
        ))
    return findings


def STARC_clock_enable_pattern(tree, file, signals):
    """STARC: Use clock enable instead of clock gating."""
    findings = []
    clk_sigs = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for ev in _find_nodes(always, 'event_expression'):
            for m_e in re.finditer(r'posedge\s+(\w+)', _node_text(ev)):
                sig = m_e.group(1)
                if 'rst' not in sig.lower() and 'reset' not in sig.lower():
                    clk_sigs.add(sig)

    text = _node_text(tree.root_node)
    for clk in clk_sigs:
        if re.search(rf'\b{re.escape(clk)}\s*&\s*\w+', text) or \
           re.search(rf'\w+\s*&\s*{re.escape(clk)}\b', text):
            findings.append(Finding(
                rule="STARC_use_clock_en", severity="warning",
                file=file, line=1,
                message=f"Clock '{clk}' ANDed with signal: use clock enable or ICG cell",
                synth_impact="Manual clock gating: glitch-prone without proper cell",
            ))
    return findings


# -------------------------------------------------------------------
# Category 32: MORE PWR
# -------------------------------------------------------------------

def PWR_shift_vs_multiply(tree, file, signals):
    """Multiplication by constant power-of-2: use shift instead."""
    findings = []
    elab = _current_elab(tree, signals)
    params = elab.constants          # names of compile-time constants
    loop_vars = elab.loop_vars       # elaboration-time indices (`8*i`)
    text = _node_text(tree.root_node)
    text_no_comments = re.sub(r'//[^\n]*', '', text)
    text_no_comments = re.sub(r'/\*.*?\*/', '', text_no_comments, flags=re.DOTALL)

    param_lines = set()
    for pm in re.finditer(r'(?:parameter|localparam)\b[^;]*;',
                          text_no_comments, re.DOTALL):
        start_line = text_no_comments[:pm.start()].count('\n') + 1
        end_line = text_no_comments[:pm.end()].count('\n') + 1
        for ln in range(start_line, end_line + 1):
            param_lines.add(ln)
    for m in re.finditer(r'(\w+)\s*\*\s*(\d+)', text_no_comments):
        val = int(m.group(2))
        if val > 1 and (val & (val - 1)) == 0:
            line = text_no_comments[:m.start()].count('\n') + 1
            if line in param_lines or m.group(1) in params or m.group(1) in loop_vars:
                continue
            shift = int(math.log2(val))
            findings.append(Finding(
                rule="PWR_shift_vs_mult", severity="info",
                file=file, line=line,
                message=f"'{m.group(1)} * {val}': use '<< {shift}' instead",
                synth_impact="Multiplier vs shifter: shift is smaller and faster",
            ))
    for m in re.finditer(r'(\d+)\s*\*\s*(\w+)', text_no_comments):
        val = int(m.group(1))
        if val > 1 and (val & (val - 1)) == 0:
            line = text_no_comments[:m.start()].count('\n') + 1
            if line in param_lines or m.group(2) in params or m.group(2) in loop_vars:
                continue
            shift = int(math.log2(val))
            findings.append(Finding(
                rule="PWR_shift_vs_mult", severity="info",
                file=file, line=line,
                message=f"'{val} * {m.group(2)}': use '{m.group(2)} << {shift}' instead",
                synth_impact="Multiplier vs shifter: shift is smaller and faster",
            ))
    return findings


def PWR_unnecessary_wide_op(tree, file, signals):
    """Arithmetic on signals wider than needed for the result."""
    findings = []
    for atype in ('blocking_assignment', 'nonblocking_assignment'):
        for asgn in _find_nodes(tree.root_node, atype):
            lhs = _get_lhs_signal(asgn)
            if not lhs:
                continue
            li = signals.get(lhs)
            if not li or li.width <= 0 or li.width > 8:
                continue
            children = asgn.named_children
            if len(children) < 2:
                continue
            rhs_ids = _get_identifiers(children[-1])
            for rid in rhs_ids:
                ri = signals.get(rid)
                if ri and ri.width > 0 and ri.width > li.width * 2:
                    rhs_text = _node_text(children[-1]).strip()
                    rhs_no_brk = re.sub(r'\[[^\]]*\]', '', rhs_text)
                    if re.search(r'[=!<>]=|[<>](?!=)', rhs_no_brk):
                        continue
                    if '+' in rhs_no_brk or '-' in rhs_no_brk or '*' in rhs_no_brk:
                        findings.append(Finding(
                            rule="PWR_wide_op", severity="info",
                            file=file, line=_node_line(asgn),
                            message=f"Wide operand '{rid}'({ri.width}b) in "
                                    f"narrow result '{lhs}'({li.width}b)",
                            synth_impact="Wider-than-needed arithmetic: wasted area and power",
                        ))
                        break
    return findings


# -------------------------------------------------------------------
# Category 33: MORE SIM
# -------------------------------------------------------------------

def SIM_specify_block(tree, file, signals):
    """specify block is for timing annotation, not synthesizable logic."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bspecify\b', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SIM_specify", severity="info",
            file=file, line=line,
            message="'specify' block: timing annotation only",
            synth_impact="Synthesis ignores specify blocks",
        ))
    return findings


def SIM_assert_immediate(tree, file, signals):
    """Immediate assertion (assert()) in RTL."""
    findings = []
    text = _node_text(tree.root_node)
    text = _strip_comments(text)
    for m in re.finditer(r'\bassert\s*\(', text):
        line = text[:m.start()].count('\n') + 1
        findings.append(Finding(
            rule="SIM_assert_imm", severity="info",
            file=file, line=line,
            message="Immediate assert(): simulation-only check",
            synth_impact="Most tools strip immediate assertions during synthesis",
        ))
    return findings


# -------------------------------------------------------------------
# Category 34: MORE CROSS-MODULE
# -------------------------------------------------------------------

def CROSS_instance_array(tree, file, signals):
    """Array of module instances."""
    findings = []
    for inst in _find_nodes(tree.root_node, 'module_instantiation'):
        text = _node_text(inst)
        if re.search(r'\w+\s*\[[^\]]+\]\s*\(', text):
            findings.append(Finding(
                rule="CROSS_inst_array", severity="info",
                file=file, line=_node_line(inst),
                message="Array of instances: verify generate loop for parameterization",
                synth_impact="Instance arrays are synthesizable but less flexible than generate",
            ))
    return findings


def CROSS_missing_connection(tree, file, signals):
    """Module instantiation with .* (implicit port connections)."""
    findings = []
    for inst in _find_nodes(tree.root_node, 'module_instantiation'):
        text = _node_text(inst)
        if '.*' in text:
            findings.append(Finding(
                rule="CROSS_implicit_conn", severity="info",
                file=file, line=_node_line(inst),
                message="Implicit port connection '.*': all ports connected by name match",
                synth_impact="Implicit: adding ports to child won't cause compile error",
            ))
    return findings


# ===================================================================
# CDC (Clock Domain Crossing) Analysis — SpyGlass Ac_cdc equivalent
# ===================================================================

def _build_clock_domain_map(tree):
    """Build a comprehensive map of clock domains from all always_ff blocks.

    Returns:
        domains: dict mapping clock_name -> {
            'written':    {sig: line},
            'read':       set of signal names,
            'blocks':     list of always_construct nodes,
            'sync_pairs': {original_source: final_dest} for 2-FF chains,
            'sync_meta':  {original_source: meta_stage} intermediate FF,
            'ff_chains':  {dest: src} raw single-stage assignments,
        }
    """
    domains = {}
    port_to_clk = _ACTIVE_SDC.port_to_clock() if _ACTIVE_SDC else {}

    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        clk = _get_ff_clock(always)
        if not clk:
            continue
        clk = port_to_clk.get(clk, clk)

        if clk not in domains:
            domains[clk] = {'written': {}, 'read': set(), 'blocks': [],
                            'sync_pairs': {}, 'sync_meta': {},
                            'ff_chains': {}}
        domain = domains[clk]
        domain['blocks'].append(always)

        written_here = {}
        assignments = {}
        for asgn in (_all_nb_assignments(always)
                     + _find_nodes(always, 'blocking_assignment')):
            lhs = _get_lhs_from_any(asgn) if asgn.type == 'clocking_drive' else _get_lhs_signal(asgn)
            if not lhs:
                continue
            written_here[lhs] = _node_line(asgn)
            rhs_text = _node_text(asgn)
            m = re.search(r'<=\s*([a-zA-Z_]\w*)', rhs_text)
            if m:
                assignments[lhs] = m.group(1)

        for sig, line in written_here.items():
            domain['written'][sig] = line

        domain['ff_chains'].update(assignments)

        for dst, src in assignments.items():
            if src in assignments and assignments[src] != src:
                domain['sync_pairs'][assignments[src]] = dst
                domain['sync_meta'][assignments[src]] = src

    for clk, domain in domains.items():
        for always in domain['blocks']:
            for ident in _find_nodes(always, 'simple_identifier'):
                name = ident.text.decode()
                if name not in domain['written'] and name != clk:
                    domain['read'].add(name)

    return domains


def _get_comb_drivers(tree):
    """Map combinational outputs to their source signals."""
    comb_driven = {}
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue
        for asgn in (_find_nodes(always, 'nonblocking_assignment')
                     + _find_nodes(always, 'blocking_assignment')):
            lhs = _get_lhs_signal(asgn)
            if lhs:
                for ident in _find_nodes(asgn, 'simple_identifier'):
                    name = ident.text.decode()
                    if name != lhs:
                        comb_driven.setdefault(lhs, set()).add(name)

    for assign_node in _find_nodes(tree.root_node, 'continuous_assign'):
        lhs = _get_lhs_signal(assign_node)
        if lhs:
            for ident in _find_nodes(assign_node, 'simple_identifier'):
                name = ident.text.decode()
                if name != lhs:
                    comb_driven.setdefault(lhs, set()).add(name)

    return comb_driven


def _detect_gray_encoding(tree):
    """Detect signals with binary-to-gray conversion."""
    text = _node_text(tree.root_node)
    gray_sigs = set()
    for m in re.finditer(r'(\w+)\s*\^\s*\(\s*\1\s*>>\s*1\s*\)', text):
        gray_sigs.add(m.group(1))
    for m in re.finditer(r'(\w+)\s*\^\s*\(\s*\1\s*>>>\s*1\s*\)', text):
        gray_sigs.add(m.group(1))
    for m in re.finditer(r'(\w+)\s*\^\s*\1\s*\[\s*[^]]+\s*:\s*1\s*\]', text):
        gray_sigs.add(m.group(1))
    # Detect function-based gray encoding: (bin >> 1) ^ bin inside functions
    has_gray_func = bool(re.search(
        r'function\b[^;]*\bgray\b[^;]*;.*?\b(\w+)\s*>>\s*1\s*\)\s*\^\s*\1'
        r'|\b(\w+)\s*>>\s*1\s*\)\s*\^\s*\2[^;]*\bendfunction\b',
        text, re.DOTALL))
    if not has_gray_func:
        has_gray_func = bool(re.search(
            r'return\s*\(\s*(\w+)\s*>>\s*1\s*\)\s*\^\s*\1', text))
    if has_gray_func:
        # All signals whose names contain 'gray' are gray-encoded
        for m in re.finditer(r'\b(\w*gray\w*)\b', text):
            gray_sigs.add(m.group(1))
    # Signals assigned from a bin2gray/to_gray function call
    for m in re.finditer(r'(\w+)\s*<=?\s*\w*(?:bin2gray|to_gray|gray)\s*\(', text):
        gray_sigs.add(m.group(1))
    # Signals with _gray suffix that are used in 2-FF sync chains
    for m in re.finditer(r'\b(\w+_gray)\b', text):
        gray_sigs.add(m.group(1))
    return gray_sigs


def _detect_handshake_pairs(tree, domains):
    """Detect req/ack handshake pairs crossing clock domains."""
    pairs = []
    all_written = {}
    for clk, d in domains.items():
        for sig, line in d['written'].items():
            all_written[sig] = clk

    text = _node_text(tree.root_node)
    req_names = set()
    ack_names = set()
    for sig in all_written:
        low = sig.lower()
        if any(r in low for r in ('req', 'valid', 'start', 'send')):
            req_names.add(sig)
        if any(a in low for a in ('ack', 'ready', 'done', 'grant')):
            ack_names.add(sig)

    for req in req_names:
        req_clk = all_written.get(req)
        for ack in ack_names:
            ack_clk = all_written.get(ack)
            if req_clk and ack_clk and req_clk != ack_clk:
                pairs.append({
                    'req': req, 'req_clk': req_clk,
                    'ack': ack, 'ack_clk': ack_clk,
                })
    return pairs


def _detect_async_fifo(tree, domains, signals):
    """Detect async FIFO patterns (separate R/W clocks, pointer signals)."""
    fifos = []
    text = _node_text(tree.root_node)
    ptr_sigs = {}
    for sig in signals:
        low = sig.lower()
        if any(p in low for p in ('wr_ptr', 'wptr', 'write_ptr',
                                   'rd_ptr', 'rptr', 'read_ptr')):
            for clk, d in domains.items():
                if sig in d['written']:
                    ptr_sigs[sig] = clk
                    break

    wr_ptrs = {s: c for s, c in ptr_sigs.items()
               if any(w in s.lower() for w in ('wr', 'write', 'wptr'))}
    rd_ptrs = {s: c for s, c in ptr_sigs.items()
               if any(r in s.lower() for r in ('rd', 'read', 'rptr'))}

    if wr_ptrs and rd_ptrs:
        wr_clks = set(wr_ptrs.values())
        rd_clks = set(rd_ptrs.values())
        if wr_clks != rd_clks:
            fifos.append({
                'wr_ptrs': wr_ptrs, 'rd_ptrs': rd_ptrs,
                'wr_clks': wr_clks, 'rd_clks': rd_clks,
            })
    return fifos


def _cdc_is_false_path(src_clk: str, dst_clk: str) -> bool:
    if _ACTIVE_SDC is None:
        return False
    return _ACTIVE_SDC.is_false_path(src_clk, dst_clk)


def _cdc_is_multicycle(src_clk: str, dst_clk: str) -> int | None:
    if _ACTIVE_SDC is None:
        return None
    return _ACTIVE_SDC.is_multicycle(src_clk, dst_clk)


# --- Ac_cdc01: Missing synchronizer ---
def CDC_missing_synchronizer(tree, file, signals):
    """Ac_cdc01: Single-bit signal crosses clock domains without 2-FF synchronizer."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    for dst_clk, dst_domain in domains.items():
        synced = set(dst_domain['sync_pairs'].keys())

        for sig in dst_domain['read']:
            for src_clk, src_domain in domains.items():
                if src_clk == dst_clk:
                    continue
                if sig not in src_domain['written']:
                    continue
                si = signals.get(sig)
                if si and si.width != 1:
                    continue
                if sig in synced:
                    continue
                if _cdc_is_false_path(src_clk, dst_clk):
                    continue
                src_line = src_domain['written'][sig]
                sdc_note = ""
                mc = _cdc_is_multicycle(src_clk, dst_clk)
                if mc:
                    sdc_note = f" [SDC: multicycle x{mc}]"
                findings.append(Finding(
                    rule="CDC_no_sync", severity="error",
                    file=file, line=src_line,
                    message=(
                        f"Ac_cdc01: '{sig}' driven by '{src_clk}' "
                        f"(line {src_line}) sampled in '{dst_clk}' domain "
                        f"without 2-FF synchronizer{sdc_note}"),
                    synth_impact=(
                        "Metastability: signal may settle to unknown value "
                        "in destination domain; add 2-FF synchronizer"),
                ))
    return findings


# --- Ac_cdc02: Multi-bit CDC without gray code ---
def CDC_multi_bit_crossing(tree, file, signals):
    """Ac_cdc02: Multi-bit signal crosses clock domains — needs gray code or handshake."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    gray_sigs = _detect_gray_encoding(tree)

    # Detect memory arrays (dual-port RAM) — safe for CDC when pointers are synced
    mem_arrays = set()
    text = _node_text(tree.root_node)
    for m in re.finditer(r'\b(?:logic|reg)\s+\[[^\]]+\]\s+(\w+)\s*\[', text):
        mem_arrays.add(m.group(1))

    for dst_clk, dst_domain in domains.items():
        for sig in dst_domain['read']:
            for src_clk, src_domain in domains.items():
                if src_clk == dst_clk:
                    continue
                if sig not in src_domain['written']:
                    continue
                if _cdc_is_false_path(src_clk, dst_clk):
                    continue
                si = signals.get(sig)
                if not si or si.width <= 1:
                    continue

                if sig in gray_sigs:
                    continue

                if sig in mem_arrays:
                    continue

                gray_variant = sig + '_gray'
                if gray_variant in src_domain['written']:
                    continue

                src_line = src_domain['written'][sig]
                findings.append(Finding(
                    rule="CDC_multi_bit", severity="error",
                    file=file, line=src_line,
                    message=(
                        f"Ac_cdc02: multi-bit signal '{sig}' "
                        f"({si.width}b) crosses from '{src_clk}' to "
                        f"'{dst_clk}' without gray encoding — 2-FF sync "
                        f"is insufficient; use gray code, handshake, "
                        f"or MUX-sync scheme"),
                    synth_impact=(
                        f"Data corruption: {si.width}-bit value may be "
                        f"sampled mid-transition; individual bits arrive "
                        f"at different times across domains"),
                ))
    return findings


# --- Ac_cdc03: Combinational logic before synchronizer ---
def CDC_combo_logic_before_sync(tree, file, signals):
    """Ac_cdc03: Combinational logic on crossing path before synchronizer."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    comb_driven = _get_comb_drivers(tree)

    for dst_clk, dst_domain in domains.items():
        for sig in dst_domain['read']:
            if sig not in comb_driven:
                continue
            sources = comb_driven[sig]
            for source in sources:
                for src_clk, src_domain in domains.items():
                    if src_clk == dst_clk:
                        continue
                    if _cdc_is_false_path(src_clk, dst_clk):
                        continue
                    if source not in src_domain['written']:
                        continue
                    src_line = src_domain['written'][source]
                    findings.append(Finding(
                        rule="CDC_glitch_combo", severity="error",
                        file=file, line=src_line,
                        message=(
                            f"Ac_cdc03: '{source}' ({src_clk}) passes "
                            f"through combinational logic '{sig}' before "
                            f"entering '{dst_clk}' domain — combo output "
                            f"can glitch and be captured by synchronizer"),
                        synth_impact=(
                            "Glitch: combinational output can produce "
                            "transient pulses that get latched by "
                            "destination FF; synchronize BEFORE combo "
                            "logic"),
                    ))
    return findings


# --- Ac_cdc04: Reconvergence ---
def CDC_reconvergence(tree, file, signals):
    """Ac_cdc04: Same source signal synchronized independently then reconverges."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    for dst_clk, dst_domain in domains.items():
        synced_from = {}
        for orig_src, final_dst in dst_domain['sync_pairs'].items():
            synced_from.setdefault(orig_src, []).append(final_dst)

        for orig_src, sync_dests in synced_from.items():
            if len(sync_dests) < 2:
                continue
            for src_clk, src_domain in domains.items():
                if src_clk == dst_clk:
                    continue
                if _cdc_is_false_path(src_clk, dst_clk):
                    continue
                if orig_src not in src_domain['written']:
                    continue
                src_line = src_domain['written'][orig_src]
                dest_list = ', '.join(sync_dests)
                findings.append(Finding(
                    rule="CDC_reconvergence", severity="error",
                    file=file, line=src_line,
                    message=(
                        f"Ac_cdc04: signal '{orig_src}' ({src_clk}) "
                        f"is independently synchronized into '{dst_clk}' "
                        f"via {len(sync_dests)} paths ({dest_list}) — "
                        f"reconvergence can cause inconsistent values"),
                    synth_impact=(
                        "Data coherence: independently synchronized copies "
                        "may arrive at different cycles; downstream logic "
                        "sees contradictory state; use single synchronizer "
                        "and fan out from its output"),
                ))

        all_synced_outputs = set()
        sync_origin = {}
        for orig_src, final_dst in dst_domain['sync_pairs'].items():
            all_synced_outputs.add(final_dst)
            sync_origin[final_dst] = orig_src
            meta = dst_domain['sync_meta'].get(orig_src)
            if meta:
                all_synced_outputs.add(meta)
                sync_origin[meta] = orig_src

        comb_driven = _get_comb_drivers(tree)
        for comb_out, comb_srcs in comb_driven.items():
            crossing_srcs = comb_srcs & all_synced_outputs
            if len(crossing_srcs) >= 2:
                origins = {sync_origin.get(s, s) for s in crossing_srcs}
                if len(origins) >= 2:
                    continue
                for orig in origins:
                    if orig in [d['written'] for d in domains.values()
                                if d is not dst_domain]:
                        src_line = dst_domain['written'].get(
                            list(crossing_srcs)[0], 1)
                        findings.append(Finding(
                            rule="CDC_convergence", severity="warning",
                            file=file, line=src_line,
                            message=(
                                f"Ac_conv: independently synchronized signals "
                                f"{crossing_srcs} converge at '{comb_out}' "
                                f"— may see inconsistent values from same "
                                f"source '{orig}'"),
                            synth_impact=(
                                "Synchronizer skew: two FFs capturing the "
                                "same asynchronous signal may resolve to "
                                "different values on the same cycle"),
                        ))
    return findings


# --- Ac_cdc05: Reset domain crossing ---
def CDC_reset_crossing(tree, file, signals):
    """Ac_cdc05: Reset signal used in a different clock domain without reset synchronizer."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    reset_names = set()
    for sig_name in signals:
        low = sig_name.lower()
        if any(r in low for r in ('rst', 'reset')):
            reset_names.add(sig_name)

    for src_clk, src_domain in domains.items():
        for rst_sig in reset_names:
            if rst_sig not in src_domain['written']:
                continue
            for dst_clk, dst_domain in domains.items():
                if dst_clk == src_clk:
                    continue
                for always in dst_domain['blocks']:
                    text = _node_text(always)
                    if re.search(r'\b' + re.escape(rst_sig) + r'\b', text):
                        if rst_sig not in dst_domain['sync_pairs']:
                            findings.append(Finding(
                                rule="CDC_async_reset", severity="error",
                                file=file,
                                line=src_domain['written'][rst_sig],
                                message=(
                                    f"Ac_cdc05: reset '{rst_sig}' generated "
                                    f"in '{src_clk}' domain used in "
                                    f"'{dst_clk}' domain without reset "
                                    f"synchronizer (async-assert "
                                    f"sync-deassert)"),
                                synth_impact=(
                                    "Reset metastability: destination FFs "
                                    "may exit reset on different clock edges; "
                                    "use reset synchronizer with async-assert "
                                    "sync-deassert pattern"),
                            ))
    return findings


# --- Ac_cdc06: FIFO pointer crossing ---
def CDC_fifo_pointer(tree, file, signals):
    """Ac_cdc06: Async FIFO pointer crossing without gray code encoding."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    gray_sigs = _detect_gray_encoding(tree)
    fifos = _detect_async_fifo(tree, domains, signals)

    for fifo in fifos:
        for ptr_sig, ptr_clk in fifo['wr_ptrs'].items():
            for rd_clk in fifo['rd_clks']:
                if rd_clk == ptr_clk:
                    continue
                si = signals.get(ptr_sig)
                width = si.width if si else -1
                if ptr_sig in gray_sigs or (ptr_sig + '_gray') in domains.get(ptr_clk, {}).get('written', {}):
                    continue
                line = domains[ptr_clk]['written'].get(ptr_sig, 1)
                findings.append(Finding(
                    rule="CDC_fifo_ptr", severity="error",
                    file=file, line=line,
                    message=(
                        f"Ac_cdc06: FIFO write pointer '{ptr_sig}' "
                        f"({width}b, {ptr_clk}) crosses to read domain "
                        f"'{rd_clk}' without gray code encoding — "
                        f"binary pointer can cause FIFO overflow/underflow "
                        f"on multi-bit transition"),
                    synth_impact=(
                        "FIFO corruption: binary pointer sampled mid-"
                        "transition can jump multiple positions; use gray "
                        "code (only 1 bit changes per increment)"),
                ))

        for ptr_sig, ptr_clk in fifo['rd_ptrs'].items():
            for wr_clk in fifo['wr_clks']:
                if wr_clk == ptr_clk:
                    continue
                si = signals.get(ptr_sig)
                width = si.width if si else -1
                if ptr_sig in gray_sigs or (ptr_sig + '_gray') in domains.get(ptr_clk, {}).get('written', {}):
                    continue
                line = domains[ptr_clk]['written'].get(ptr_sig, 1)
                findings.append(Finding(
                    rule="CDC_fifo_ptr", severity="error",
                    file=file, line=line,
                    message=(
                        f"Ac_cdc06: FIFO read pointer '{ptr_sig}' "
                        f"({width}b, {ptr_clk}) crosses to write domain "
                        f"'{wr_clk}' without gray code encoding"),
                    synth_impact=(
                        "FIFO corruption: binary pointer sampled mid-"
                        "transition can cause incorrect full/empty; use "
                        "gray code for pointer crossing"),
                ))
    return findings


# --- Ac_cdc07: MUX synchronizer scheme ---
def CDC_mux_sync(tree, file, signals):
    """Ac_cdc07: MUX synchronizer — data must be held stable while select is synchronized."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    gray_sigs = _detect_gray_encoding(tree)

    for dst_clk, dst_domain in domains.items():
        synced = set(dst_domain['sync_pairs'].keys())
        for always in dst_domain['blocks']:
            for cond in _find_nodes(always, 'conditional_statement'):
                cond_text = _node_text(cond)
                for sel_sig in synced:
                    if re.search(r'\b' + re.escape(sel_sig) + r'\b',
                                 cond_text):
                        for asgn in _find_nodes(cond, 'nonblocking_assignment'):
                            rhs_text = _node_text(asgn)
                            m = re.search(r'<=\s*([a-zA-Z_]\w*)', rhs_text)
                            if not m:
                                continue
                            data_sig = m.group(1)
                            if data_sig in gray_sigs:
                                continue
                            for src_clk, src_domain in domains.items():
                                if src_clk == dst_clk:
                                    continue
                                if _cdc_is_false_path(src_clk, dst_clk):
                                    continue
                                if data_sig in src_domain['written']:
                                    si = signals.get(data_sig)
                                    if si and si.width > 1:
                                        src_line = src_domain['written'][data_sig]
                                        findings.append(Finding(
                                            rule="CDC_mux_data_unstable",
                                            severity="warning",
                                            file=file, line=src_line,
                                            message=(
                                                f"Ac_cdc07: MUX-sync scheme — "
                                                f"'{sel_sig}' is synchronized "
                                                f"but data '{data_sig}' "
                                                f"({si.width}b, {src_clk}) "
                                                f"must be held stable for "
                                                f"2+ destination clock cycles "
                                                f"when select transitions"),
                                            synth_impact=(
                                                "Data integrity: if multi-bit "
                                                "data changes while "
                                                "synchronized select "
                                                "transitions, destination "
                                                "captures partial update"),
                                        ))
    return findings


# --- Ac_cdc08: Handshake protocol crossing ---
def CDC_handshake(tree, file, signals):
    """Ac_cdc08: Handshake req/ack crossing — both directions need synchronizers."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    pairs = _detect_handshake_pairs(tree, domains)

    for pair in pairs:
        req, req_clk = pair['req'], pair['req_clk']
        ack, ack_clk = pair['ack'], pair['ack_clk']

        ack_domain = domains.get(req_clk, {})
        ack_synced = ack in ack_domain.get('sync_pairs', {})

        req_domain = domains.get(ack_clk, {})
        req_synced = req in req_domain.get('sync_pairs', {})

        req_line = domains[req_clk]['written'].get(req, 1)

        if not req_synced:
            findings.append(Finding(
                rule="CDC_handshake_req", severity="error",
                file=file, line=req_line,
                message=(
                    f"Ac_cdc08: handshake request '{req}' ({req_clk}) "
                    f"not synchronized into ack domain '{ack_clk}' — "
                    f"req must pass through 2-FF synchronizer"),
                synth_impact=(
                    "Metastability on handshake request: receiver may "
                    "see glitched req causing double-acceptance or missed "
                    "transaction"),
            ))

        if not ack_synced:
            ack_line = domains[ack_clk]['written'].get(ack, 1)
            findings.append(Finding(
                rule="CDC_handshake_ack", severity="error",
                file=file, line=ack_line,
                message=(
                    f"Ac_cdc08: handshake acknowledge '{ack}' ({ack_clk}) "
                    f"not synchronized back into req domain '{req_clk}' — "
                    f"ack must pass through 2-FF synchronizer"),
                synth_impact=(
                    "Metastability on handshake ack: sender may release "
                    "data before receiver has captured it, or hold data "
                    "too long"),
            ))
    return findings


# --- Ac_cdc09: Pulse synchronizer ---
def CDC_pulse_crossing(tree, file, signals):
    """Ac_cdc09: Single-cycle pulse may be missed by slower destination clock."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    pulse_sigs = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        text = _node_text(always)
        for asgn in _find_nodes(always, 'nonblocking_assignment'):
            lhs = _get_lhs_signal(asgn)
            if not lhs:
                continue
            si = signals.get(lhs)
            if not si or si.width != 1:
                continue
            set_count = len(re.findall(
                re.escape(lhs) + r"\s*<=\s*1", text))
            clr_count = len(re.findall(
                re.escape(lhs) + r"\s*<=\s*(?:1'b)?0", text))
            if set_count >= 1 and clr_count >= 1:
                pulse_sigs.add(lhs)

    for dst_clk, dst_domain in domains.items():
        for sig in dst_domain['read']:
            if sig not in pulse_sigs:
                continue
            for src_clk, src_domain in domains.items():
                if src_clk == dst_clk:
                    continue
                if _cdc_is_false_path(src_clk, dst_clk):
                    continue
                if sig not in src_domain['written']:
                    continue
                toggle_name = sig.replace('pulse', 'toggle').replace(
                    'req', 'toggle')
                has_toggle = toggle_name in src_domain['written']
                src_line = src_domain['written'][sig]
                findings.append(Finding(
                    rule="CDC_pulse_sync", severity="warning",
                    file=file, line=src_line,
                    message=(
                        f"Ac_cdc09: pulse signal '{sig}' ({src_clk}) may "
                        f"be missed by '{dst_clk}' if destination clock "
                        f"is slower — use pulse synchronizer "
                        f"(toggle + 2FF + XOR) or handshake"),
                    synth_impact=(
                        "Missed event: single-cycle pulse shorter than "
                        "destination clock period will not be captured "
                        "by 2-FF synchronizer; convert to level (toggle) "
                        "before crossing"),
                ))
    return findings


# --- Ac_cdc10: Clock gating on CDC path ---
def CDC_clock_gating(tree, file, signals):
    """Ac_cdc10: Gated clock feeding a synchronizer can mask transitions."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    text = _node_text(tree.root_node)
    gated_clocks = set()
    for m in re.finditer(r'assign\s+(\w+)\s*=\s*(\w+)\s*&\s*(\w+)', text):
        out, a, b = m.group(1), m.group(2), m.group(3)
        for clk in domains:
            if a == clk or b == clk:
                gated_clocks.add(out)

    for m in re.finditer(
            r'(\w+)\s*=\s*(\w+)\s*\?\s*(\w+)\s*:\s*1\'b0', text):
        out, sel, clk_sig = m.group(1), m.group(2), m.group(3)
        if clk_sig in domains:
            gated_clocks.add(out)

    for gated in gated_clocks:
        if gated in domains:
            for sig in domains[gated].get('sync_pairs', {}).keys():
                findings.append(Finding(
                    rule="CDC_gated_sync_clk", severity="warning",
                    file=file, line=1,
                    message=(
                        f"Ac_cdc10: synchronizer in '{gated}' domain "
                        f"uses a gated clock — if the gate disables the "
                        f"clock, the synchronizer misses transitions of "
                        f"'{sig}'"),
                    synth_impact=(
                        "Missed CDC event: gated clock may prevent "
                        "synchronizer from sampling the asynchronous "
                        "input for multiple cycles; use ungated clock "
                        "for synchronizer FFs"),
                ))
    return findings


# --- CDC crossing summary (stored in measurements) ---
def _cdc_crossing_summary(tree, file, signals):
    """Generate SpyGlass-style CDC crossing report."""
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return []

    gray_sigs = _detect_gray_encoding(tree)
    crossings = []
    crossing_id = 0

    for dst_clk, dst_domain in domains.items():
        synced = dst_domain['sync_pairs']
        for sig in dst_domain['read']:
            for src_clk, src_domain in domains.items():
                if src_clk == dst_clk:
                    continue
                if sig not in src_domain['written']:
                    continue

                crossing_id += 1
                si = signals.get(sig)
                width = si.width if si else -1

                if sig in synced:
                    sync_type = "2-FF"
                elif sig in gray_sigs:
                    sync_type = "Gray"
                elif width > 1:
                    sync_type = "NONE (multi-bit)"
                else:
                    sync_type = "NONE"

                crossings.append({
                    'id': f"CDC_{crossing_id:03d}",
                    'signal': sig,
                    'width': width,
                    'src_domain': src_clk,
                    'dst_domain': dst_clk,
                    'src_line': src_domain['written'][sig],
                    'sync_type': sync_type,
                    'status': 'PASS' if sync_type not in (
                        'NONE', 'NONE (multi-bit)') else 'FAIL',
                })
    return crossings


# ===================================================================
# RDC (Reset Domain Crossing) Analysis
# ===================================================================

def _build_reset_map(tree, signals):
    """Map reset signals to their source clock domain and properties.

    Returns:
        resets: dict mapping reset_name -> {
            'line': int, 'clock': str, 'is_async': bool,
            'is_active_low': bool, 'driven_by_ff': bool,
            'used_by': list of clock domain names,
        }
    """
    domains = _build_clock_domain_map(tree)
    resets = {}

    for sig_name, si in signals.items():
        low = sig_name.lower()
        if not any(r in low for r in ('rst', 'reset')):
            continue
        is_active_low = low.endswith('_n') or low.endswith('_b') or \
            low.endswith('_l') or 'n_' in low

        src_clk = ''
        driven_by_ff = False
        for clk, d in domains.items():
            if sig_name in d['written']:
                src_clk = clk
                driven_by_ff = True
                break

        is_async = False
        used_by = []
        for always in _find_nodes(tree.root_node, 'always_construct'):
            if _always_type(always) != 'always_ff':
                continue
            text = _node_text(always)
            if not re.search(r'\b' + re.escape(sig_name) + r'\b', text):
                continue
            clk = _get_ff_clock(always)
            if clk and clk not in used_by:
                used_by.append(clk)
            for ev in _find_nodes(always, 'event_expression'):
                ev_text = _node_text(ev)
                if re.search(r'(?:posedge|negedge)\s+' +
                             re.escape(sig_name), ev_text):
                    is_async = True

        resets[sig_name] = {
            'line': si.line, 'clock': src_clk,
            'is_async': is_async, 'is_active_low': is_active_low,
            'driven_by_ff': driven_by_ff, 'used_by': used_by,
        }
    return resets


def RDC_async_reset_no_sync(tree, file, signals):
    """RDC: Async reset generated in one clock domain used in another without synchronizer."""
    findings = []
    resets = _build_reset_map(tree, signals)
    domains = _build_clock_domain_map(tree)

    for rst_name, rst_info in resets.items():
        if not rst_info['driven_by_ff'] or not rst_info['clock']:
            continue
        for used_clk in rst_info['used_by']:
            if used_clk == rst_info['clock']:
                continue
            dst_domain = domains.get(used_clk, {})
            if rst_name in dst_domain.get('sync_pairs', {}):
                continue
            findings.append(Finding(
                rule="RDC_async_no_sync", severity="error",
                file=file, line=rst_info['line'],
                message=(
                    f"RDC: reset '{rst_name}' generated in "
                    f"'{rst_info['clock']}' domain used in '{used_clk}' "
                    f"domain without reset synchronizer"),
                synth_impact=(
                    "Reset metastability: destination FFs may exit "
                    "reset asynchronously; use async-assert "
                    "sync-deassert reset bridge"),
            ))
    return findings


def RDC_combo_reset_path(tree, file, signals):
    """RDC: Reset driven by combinational logic (glitch risk)."""
    findings = []
    comb_driven = _get_comb_drivers(tree)

    for sig_name, si in signals.items():
        low = sig_name.lower()
        if not any(r in low for r in ('rst', 'reset')):
            continue
        if sig_name in comb_driven:
            sources = comb_driven[sig_name]
            line = si.line
            for always in _find_nodes(tree.root_node, 'always_construct'):
                if _always_type(always) != 'always_comb':
                    continue
                text = _node_text(always)
                if re.search(r'\b' + re.escape(sig_name) + r'\s*=',
                             text):
                    line = _node_line(always)
                    break
            findings.append(Finding(
                rule="RDC_combo_reset", severity="error",
                file=file, line=line,
                message=(
                    f"RDC: reset '{sig_name}' driven by combinational "
                    f"logic (sources: {', '.join(list(sources)[:5])}) "
                    f"— glitch on reset can corrupt sequential state"),
                synth_impact=(
                    "Reset glitch: combinational reset path can produce "
                    "transient assertion/deassertion; register the reset "
                    "output or use dedicated reset controller"),
            ))
    return findings


def RDC_reset_glitch(tree, file, signals):
    """RDC: Reset signal derived from gated or muxed logic."""
    findings = []
    text = _node_text(tree.root_node)

    for sig_name, si in signals.items():
        low = sig_name.lower()
        if not any(r in low for r in ('rst', 'reset')):
            continue
        for m in re.finditer(
                r'assign\s+' + re.escape(sig_name) +
                r'\s*=\s*(.+?)(?:;|$)', text):
            expr = m.group(1).strip()
            if re.search(r'[&|^?:]', expr):
                line = text[:m.start()].count('\n') + 1
                findings.append(Finding(
                    rule="RDC_glitch_reset", severity="warning",
                    file=file, line=line,
                    message=(
                        f"RDC: reset '{sig_name}' derived from logic "
                        f"expression '{expr[:50]}' — may glitch during "
                        f"input transitions"),
                    synth_impact=(
                        "Reset integrity: logic-derived reset can glitch; "
                        "register the reset or use a clean reset tree"),
                ))
    return findings


def RDC_missing_reset_filter(tree, file, signals):
    """RDC: External async reset input used directly without debounce/filter."""
    findings = []
    resets = _build_reset_map(tree, signals)

    for rst_name, rst_info in resets.items():
        if rst_info['driven_by_ff']:
            continue
        if not rst_info['is_async']:
            continue
        si = signals.get(rst_name)
        if not si:
            continue
        is_input = False
        text = _node_text(tree.root_node)
        if re.search(r'input\s+(?:logic\s+)?(?:wire\s+)?' +
                     re.escape(rst_name), text):
            is_input = True
        if not is_input:
            continue

        filter_patterns = [rst_name + '_sync', rst_name + '_d',
                           rst_name + '_meta', rst_name + '_filtered']
        has_filter = any(fp in text for fp in filter_patterns)
        if not has_filter:
            findings.append(Finding(
                rule="RDC_no_filter", severity="warning",
                file=file, line=si.line,
                message=(
                    f"RDC: external async reset '{rst_name}' used "
                    f"directly in {len(rst_info['used_by'])} clock "
                    f"domain(s) without debounce or synchronizer filter"),
                synth_impact=(
                    "Reset bounce: external reset can have contact "
                    "bounce or ringing; add reset synchronizer "
                    "(async-assert, sync-deassert)"),
            ))
    return findings


def RDC_mixed_reset_polarity(tree, file, signals):
    """RDC: Module uses both active-high and active-low resets."""
    findings = []
    resets = _build_reset_map(tree, signals)
    if len(resets) < 2:
        return findings

    active_high = [r for r, info in resets.items()
                   if not info['is_active_low'] and info['used_by']]
    active_low = [r for r, info in resets.items()
                  if info['is_active_low'] and info['used_by']]

    if active_high and active_low:
        findings.append(Finding(
            rule="RDC_mixed_polarity", severity="warning",
            file=file, line=1,
            message=(
                f"RDC: module uses both active-high reset "
                f"({', '.join(active_high[:3])}) and active-low reset "
                f"({', '.join(active_low[:3])}) — inconsistent reset "
                f"polarity increases integration risk"),
            synth_impact=(
                "Reset confusion: mixed polarities require explicit "
                "reset polarity documentation and increase the risk "
                "of inverted reset connections at integration"),
        ))
    return findings


# ===================================================================
# DFT (Design for Testability) Analysis — SpyGlass DFT equivalent
# ===================================================================

def _detect_scan_signals(tree, signals):
    """Identify scan-related signals by naming convention."""
    scan_sigs = {'scan_en': [], 'scan_in': [], 'scan_out': [],
                 'test_mode': [], 'scan_clk': []}
    for sig_name in signals:
        low = sig_name.lower()
        if 'scan_en' in low or low == 'se' or 'scan_enable' in low:
            scan_sigs['scan_en'].append(sig_name)
        elif 'scan_in' in low or low == 'si':
            scan_sigs['scan_in'].append(sig_name)
        elif 'scan_out' in low or low == 'so':
            scan_sigs['scan_out'].append(sig_name)
        elif 'test_mode' in low or 'tm' == low or 'test_en' in low:
            scan_sigs['test_mode'].append(sig_name)
        elif 'scan_clk' in low or 'tck' == low:
            scan_sigs['scan_clk'].append(sig_name)
    return scan_sigs


def DFT_non_scannable_ff(tree, file, signals):
    """DFT: Sequential element without scan mux (not scannable)."""
    findings = []
    scan_sigs = _detect_scan_signals(tree, signals)
    if not scan_sigs['scan_en']:
        return findings

    scan_en_names = set(scan_sigs['scan_en'])
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        text = _node_text(always)
        has_scan_mux = any(
            re.search(r'\b' + re.escape(se) + r'\b', text)
            for se in scan_en_names)
        if has_scan_mux:
            continue
        written = set()
        for m in re.finditer(r'(\w+)\s*<=', text):
            written.add(m.group(1))
        for sig in written:
            if sig in signals and signals[sig].width > 0:
                findings.append(Finding(
                    rule="DFT_nonscan_ff", severity="warning",
                    file=file, line=_node_line(always),
                    message=(
                        f"DFT: register '{sig}' has no scan mux — "
                        f"not scannable for ATPG. Add scan_en-gated "
                        f"mux for scan_in path"),
                    synth_impact=(
                        "Test coverage: non-scannable FFs reduce "
                        "structural test coverage; synthesis scan "
                        "insertion may auto-fix if tool supports it"),
                ))
                break
    return findings


def DFT_clock_not_controllable(tree, file, signals):
    """DFT: Clock gating not bypassable during scan mode."""
    findings = []
    scan_sigs = _detect_scan_signals(tree, signals)
    if not scan_sigs['scan_en'] and not scan_sigs['test_mode']:
        return findings
    bypass_names = set(scan_sigs['scan_en'] + scan_sigs['test_mode'])
    text = _node_text(tree.root_node)

    for m in re.finditer(
            r'assign\s+(\w*clk\w*)\s*=\s*(.+?)(?:;|$)', text,
            re.IGNORECASE):
        gate_name = m.group(1)
        gate_expr = m.group(2)
        if not re.search(r'[&|]', gate_expr):
            continue
        has_bypass = any(re.search(r'\b' + re.escape(bp) + r'\b',
                                   gate_expr) for bp in bypass_names)
        if not has_bypass:
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="DFT_clock_no_bypass", severity="error",
                file=file, line=line,
                message=(
                    f"DFT: gated clock '{gate_name}' has no scan/test "
                    f"bypass — clock is uncontrollable during scan shift. "
                    f"Gate expression: {gate_expr.strip()[:60]}"),
                synth_impact=(
                    "Scan failure: uncontrollable clock prevents scan "
                    "chain shift; add test_mode bypass to clock gate"),
            ))
    return findings


def DFT_async_set_reset_scan(tree, file, signals):
    """DFT: Async set/reset not controllable during scan (blocks shift)."""
    findings = []
    scan_sigs = _detect_scan_signals(tree, signals)
    if not scan_sigs['scan_en'] and not scan_sigs['test_mode']:
        return findings
    bypass_names = set(scan_sigs['scan_en'] + scan_sigs['test_mode'])

    seen_lines = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        text = _node_text(always)
        reported_sigs = set()
        for ev in _find_nodes(always, 'event_expression'):
            ev_text = _node_text(ev)
            for em in re.finditer(
                    r'(?:posedge|negedge)\s+(\w+)', ev_text):
                sig = em.group(1)
                low = sig.lower()
                if 'clk' in low or 'clock' in low:
                    continue
                if sig in reported_sigs:
                    continue
                line_key = (_node_line(always), sig)
                if line_key in seen_lines:
                    continue
                has_bypass = any(
                    re.search(r'\b' + re.escape(bp) + r'\b', text)
                    for bp in bypass_names)
                if not has_bypass:
                    reported_sigs.add(sig)
                    seen_lines.add(line_key)
                    findings.append(Finding(
                        rule="DFT_async_ctrl_scan", severity="warning",
                        file=file, line=_node_line(always),
                        message=(
                            f"DFT: async set/reset '{sig}' not gated "
                            f"by test_mode during scan — may corrupt "
                            f"scan chain during shift"),
                        synth_impact=(
                            "Scan corruption: async set/reset firing "
                            "during shift corrupts captured data; gate "
                            "with test_mode OR during scan"),
                    ))
    return findings


def DFT_memory_no_bist(tree, file, signals):
    """DFT: Large memory array without BIST wrapper."""
    findings = []
    text = _node_text(tree.root_node)

    for sig_name, si in signals.items():
        if si.width <= 1:
            continue
        for m in re.finditer(
                r'(\w+)\s*\[(\d+):(\d+)\]\s*\[(\d+):(\d+)\]', text):
            name = m.group(1)
            if name != sig_name:
                continue
            dim1 = abs(int(m.group(2)) - int(m.group(3))) + 1
            dim2 = abs(int(m.group(4)) - int(m.group(5))) + 1
            total_bits = dim1 * dim2
            if total_bits < 256:
                continue
            bist_patterns = [name + '_bist', 'bist_' + name,
                             'mbist', 'mem_bist']
            has_bist = any(bp in text.lower() for bp in bist_patterns)
            if not has_bist:
                findings.append(Finding(
                    rule="DFT_mem_no_bist", severity="warning",
                    file=file, line=si.line,
                    message=(
                        f"DFT: memory '{name}' ({dim1}x{dim2} = "
                        f"{total_bits} bits) has no BIST wrapper — "
                        f"not testable by scan ATPG"),
                    synth_impact=(
                        "Test gap: large memories need MBIST for "
                        "manufacturing test; scan ATPG cannot reach "
                        "internal memory cells"),
                ))

    for m in re.finditer(
            r'(?:logic|reg)\s*\[(\d+):(\d+)\]\s+(\w+)\s*\[(\d+)(?::(\d+))?\]',
            text):
        width = abs(int(m.group(1)) - int(m.group(2))) + 1
        name = m.group(3)
        if m.group(5):
            depth = abs(int(m.group(4)) - int(m.group(5))) + 1
        else:
            depth = int(m.group(4))
        total_bits = width * depth
        if total_bits < 256:
            continue
        bist_patterns = [name + '_bist', 'bist_' + name, 'mbist']
        has_bist = any(bp in text.lower() for bp in bist_patterns)
        if not has_bist:
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="DFT_mem_no_bist", severity="warning",
                file=file, line=line,
                message=(
                    f"DFT: memory '{name}' ({depth}x{width} = "
                    f"{total_bits} bits) has no BIST wrapper — "
                    f"not testable by scan ATPG"),
                synth_impact=(
                    "Test gap: large memories need MBIST for "
                    "manufacturing test; scan ATPG cannot reach "
                    "internal memory cells"),
            ))
    return findings


def DFT_tristate_in_scan(tree, file, signals):
    """DFT: Tristate driver active during scan mode causes bus contention."""
    findings = []
    scan_sigs = _detect_scan_signals(tree, signals)
    if not scan_sigs['scan_en'] and not scan_sigs['test_mode']:
        return findings

    text = _node_text(tree.root_node)
    bypass_names = set(scan_sigs['scan_en'] + scan_sigs['test_mode'])

    for m in re.finditer(r"assign\s+(\w+)\s*=\s*.*\?\s*.*:\s*\d+'bz",
                         text, re.IGNORECASE):
        tri_sig = m.group(1)
        expr = m.group(0)
        has_scan_guard = any(
            re.search(r'\b' + re.escape(bp) + r'\b', expr)
            for bp in bypass_names)
        if not has_scan_guard:
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="DFT_tristate_scan", severity="error",
                file=file, line=line,
                message=(
                    f"DFT: tristate driver on '{tri_sig}' not gated "
                    f"by test_mode — bus contention during scan shift "
                    f"can damage silicon"),
                synth_impact=(
                    "Bus contention: multiple tristates driving during "
                    "scan causes current spike and potential damage; "
                    "disable tristates in test_mode"),
            ))
    return findings


def DFT_combo_loop_scan(tree, file, signals):
    """DFT: Combinational loop blocks scan ATPG."""
    findings = []
    comb_driven = _get_comb_drivers(tree)
    for sig, sources in comb_driven.items():
        if sig in sources:
            findings.append(Finding(
                rule="DFT_combo_loop", severity="error",
                file=file, line=signals[sig].line
                if sig in signals else 1,
                message=(
                    f"DFT: combinational loop on '{sig}' — blocks "
                    f"scan ATPG fault simulation. Break with "
                    f"registered stage or test point"),
                synth_impact=(
                    "ATPG blockage: combo loops prevent controllability "
                    "and observability analysis; zero test coverage on "
                    "logic in the loop"),
            ))
    return findings


def DFT_observe_internal(tree, file, signals):
    """DFT: Large module with no observation test point."""
    findings = []
    scan_sigs = _detect_scan_signals(tree, signals)
    if not scan_sigs['scan_en']:
        return findings

    ff_count = 0
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) == 'always_ff':
            ff_count += 1

    outputs = set()
    for sig_name, si in signals.items():
        if si.direction == 'output':
            outputs.add(sig_name)

    internal = set()
    for sig_name, si in signals.items():
        if si.direction == '' and si.width > 0:
            internal.add(sig_name)

    if ff_count > 50 and len(outputs) < 4 and len(internal) > 20:
        findings.append(Finding(
            rule="DFT_low_observe", severity="info",
            file=file, line=1,
            message=(
                f"DFT: {ff_count} FFs with only {len(outputs)} outputs "
                f"— low observability ratio. Consider adding "
                f"observation test points for higher fault coverage"),
            synth_impact=(
                "Coverage gap: low output-to-FF ratio limits "
                "fault propagation; observation scan points or "
                "compression can improve coverage"),
        ))
    return findings


def DFT_gated_clock_mux(tree, file, signals):
    """DFT: Clock mux without glitch-free switching (DFT risk)."""
    findings = []
    text = _node_text(tree.root_node)

    for m in re.finditer(
            r'assign\s+(\w*clk\w*)\s*=\s*(\w+)\s*\?\s*(\w+)\s*:\s*(\w+)',
            text, re.IGNORECASE):
        mux_out = m.group(1)
        sel = m.group(2)
        clk0 = m.group(3)
        clk1 = m.group(4)
        line = text[:m.start()].count('\n') + 1
        sel_low = sel.lower()
        if any(k in sel_low for k in ('clk', 'clock')):
            continue
        icg_patterns = ['icg_', 'glitch_free', 'clk_mux', 'clkmux']
        if any(p in text.lower() for p in icg_patterns):
            continue
        findings.append(Finding(
            rule="DFT_clk_mux_glitch", severity="warning",
            file=file, line=line,
            message=(
                f"DFT: clock mux '{mux_out}' = {sel}?{clk0}:{clk1} "
                f"may glitch during switching — use integrated clock "
                f"gate cell with glitch-free mux"),
            synth_impact=(
                "Clock glitch: bare clock mux can produce runt pulse "
                "during select transition; use library ICG cell or "
                "glitch-free clock mux"),
        ))
    return findings


# ===================================================================
# SVA / Formal Intent Analysis — SpyGlass assertion checks
# ===================================================================

def _extract_assertions(tree):
    """Extract all concurrent and immediate assertions from the tree.

    Returns list of dicts:
        type: 'assert'|'assume'|'cover'
        name: label or ''
        text: full assertion text
        line: line number
        has_clock: bool
        has_disable: bool
        clock_sig: str
        property_text: str (the property body)
    """
    assertions = []
    for node in _find_nodes(tree.root_node, 'concurrent_assertion_item'):
        text = _node_text(node)
        line = _node_line(node)

        atype = 'assert'
        if text.lstrip().startswith('assume') or \
                re.match(r'\w+:\s*assume', text):
            atype = 'assume'
        elif text.lstrip().startswith('cover') or \
                re.match(r'\w+:\s*cover', text):
            atype = 'cover'

        name_m = re.match(r'(\w+)\s*:', text)
        name = name_m.group(1) if name_m else ''

        has_clock = bool(re.search(
            r'@\s*\(\s*(?:posedge|negedge)\s+\w+', text))
        clock_m = re.search(
            r'@\s*\(\s*(?:posedge|negedge)\s+(\w+)', text)
        clock_sig = clock_m.group(1) if clock_m else ''

        has_disable = bool(re.search(r'disable\s+iff', text))

        prop_m = re.search(
            r'property\s*\((.*)\)\s*;?\s*$', text, re.DOTALL)
        prop_text = prop_m.group(1) if prop_m else text

        assertions.append({
            'type': atype, 'name': name, 'text': text,
            'line': line, 'has_clock': has_clock,
            'has_disable': has_disable, 'clock_sig': clock_sig,
            'property_text': prop_text,
        })
    return assertions


def SVA_missing_clock(tree, file, signals):
    """SVA: Concurrent assertion without clock event — may not be checked."""
    findings = []
    for a in _extract_assertions(tree):
        if a['type'] == 'cover':
            continue
        if 'property' in a['text'] and not a['has_clock']:
            label = f"'{a['name']}'" if a['name'] else 'unnamed'
            findings.append(Finding(
                rule="SVA_no_clock", severity="error",
                file=file, line=a['line'],
                message=(
                    f"SVA: concurrent assertion {label} has no clock "
                    f"event (@posedge/@negedge) — property will not be "
                    f"sampled and may vacuously pass"),
                synth_impact=(
                    "Formal hole: unclocked concurrent assertion is "
                    "never evaluated by simulators or formal tools; "
                    "add @(posedge clk) to the property"),
            ))
    return findings


def SVA_no_reset_disable(tree, file, signals):
    """SVA: Assertion without disable iff — fires spuriously during reset."""
    findings = []
    has_reset = any(
        any(r in s.lower() for r in ('rst', 'reset'))
        for s in signals)
    if not has_reset:
        return findings

    for a in _extract_assertions(tree):
        if a['type'] != 'assert':
            continue
        if not a['has_clock']:
            continue
        if a['has_disable']:
            continue
        label = f"'{a['name']}'" if a['name'] else 'unnamed'
        findings.append(Finding(
            rule="SVA_no_disable_iff", severity="warning",
            file=file, line=a['line'],
            message=(
                f"SVA: assertion {label} has no 'disable iff' clause "
                f"— will fire during reset when signals are in "
                f"undefined state"),
            synth_impact=(
                "False failures: assertions without disable iff "
                "report false violations during reset; add "
                "'disable iff (!rst_n)' for async reset designs"),
        ))
    return findings


def SVA_vacuous_implication(tree, file, signals):
    """SVA: Assertion with implication whose antecedent may never be true."""
    findings = []
    for a in _extract_assertions(tree):
        if a['type'] != 'assert':
            continue
        prop = a['property_text']
        imp_m = re.search(r'(\w+)\s*\|[-=]>', prop)
        if not imp_m:
            continue
        antecedent = imp_m.group(1)
        ant_low = antecedent.lower()
        if ant_low in ('1', '0', "1'b0", "1'b1"):
            if ant_low in ('0', "1'b0"):
                label = f"'{a['name']}'" if a['name'] else 'unnamed'
                findings.append(Finding(
                    rule="SVA_vacuous", severity="error",
                    file=file, line=a['line'],
                    message=(
                        f"SVA: assertion {label} has constant-false "
                        f"antecedent '{antecedent}' — implication "
                        f"vacuously passes (never checked)"),
                    synth_impact=(
                        "Dead assertion: vacuous implication provides "
                        "zero verification value; the consequent is "
                        "never evaluated"),
                ))
    return findings


def SVA_unbounded_liveness(tree, file, signals):
    """SVA: Unbounded liveness property ##[0:$] may never complete in sim."""
    findings = []
    for a in _extract_assertions(tree):
        prop = a['property_text']
        if '##[0:$]' in prop or '##[1:$]' in prop or \
                '##[0 :$]' in prop or '##[1 :$]' in prop:
            label = f"'{a['name']}'" if a['name'] else 'unnamed'
            findings.append(Finding(
                rule="SVA_unbounded", severity="warning",
                file=file, line=a['line'],
                message=(
                    f"SVA: property {label} uses unbounded delay "
                    f"##[0:$] — liveness property cannot complete in "
                    f"bounded simulation; only checkable by formal"),
                synth_impact=(
                    "Verification gap: unbounded delays make the "
                    "property un-checkable in simulation; constrain "
                    "the upper bound or use formal verification"),
            ))
    return findings


def SVA_assume_in_design(tree, file, signals):
    """SVA: assume property in design (not testbench) constrains formal."""
    findings = []
    text = _node_text(tree.root_node)
    is_tb = bool(re.search(
        r'(?:program|class|initial\s+begin|#\d+)', text))
    if is_tb:
        return findings

    for a in _extract_assertions(tree):
        if a['type'] != 'assume':
            continue
        label = f"'{a['name']}'" if a['name'] else 'unnamed'
        findings.append(Finding(
            rule="SVA_assume_in_rtl", severity="warning",
            file=file, line=a['line'],
            message=(
                f"SVA: 'assume property' {label} in RTL module — "
                f"constrains formal engine inputs, may mask real bugs "
                f"if overly restrictive"),
            synth_impact=(
                "Formal constraint: assumptions reduce the formal "
                "search space; over-constraining can hide reachable "
                "bug states. Move to bind file or testbench"),
        ))
    return findings


def SVA_missing_else_action(tree, file, signals):
    """SVA: assert without else clause — failure is silent in simulation."""
    findings = []
    for a in _extract_assertions(tree):
        if a['type'] != 'assert':
            continue
        text = a['text']
        if 'else' in text:
            continue
        if not a['has_clock']:
            continue
        label = f"'{a['name']}'" if a['name'] else 'unnamed'
        findings.append(Finding(
            rule="SVA_no_else_action", severity="info",
            file=file, line=a['line'],
            message=(
                f"SVA: assertion {label} has no 'else' action — "
                f"failure may be silently ignored depending on "
                f"simulator settings"),
            synth_impact=(
                "Silent failure: some simulators don't report "
                "assertion failures without explicit else $error; "
                "add 'else $error(...)' for reliable detection"),
        ))
    return findings


def SVA_cover_missing(tree, file, signals):
    """SVA: Module has assertions but no cover properties — no reachability proof."""
    findings = []
    assertions = _extract_assertions(tree)
    if not assertions:
        return findings

    has_assert = any(a['type'] == 'assert' for a in assertions)
    has_cover = any(a['type'] == 'cover' for a in assertions)

    if has_assert and not has_cover:
        findings.append(Finding(
            rule="SVA_no_cover", severity="info",
            file=file, line=1,
            message=(
                "SVA: module has assertions but no cover properties "
                "— cannot prove assertions are reachable, not "
                "vacuously passing due to unreachable states"),
            synth_impact=(
                "Verification gap: cover properties are essential "
                "to prove assertion antecedents are reachable; "
                "without them, all assertions may vacuously pass"),
        ))
    return findings


def SVA_fsm_no_assertions(tree, file, signals):
    """SVA: FSM detected but no assertions covering state transitions."""
    findings = []
    text = _node_text(tree.root_node)

    state_enums = re.findall(
        r'typedef\s+enum\s+(?:logic\s*\[.*?\]\s*)?{([^}]+)}\s*(\w+)',
        text, re.DOTALL)
    state_enums = [(body, name) for body, name in state_enums
                   if _is_fsm_enum(name, tree)]
    if not state_enums:
        state_patterns = re.findall(
            r"parameter\s+(\w*[Ss](?:TATE|t)\w*)\s*=", text)
        case_m = re.search(r'case\s*\(\s*(\w*state\w*)\s*\)',
                           text, re.IGNORECASE)
        if not (state_patterns or case_m):
            return findings

    assertions = _extract_assertions(tree)
    state_asserted = False
    for a in assertions:
        prop = a['property_text'].lower()
        if any(k in prop for k in ('state', 'fsm', 'idle',
                                    'next_state')):
            state_asserted = True
            break

    if not state_asserted:
        findings.append(Finding(
            rule="SVA_fsm_uncovered", severity="info",
            file=file, line=1,
            message=(
                "SVA: FSM detected but no assertions cover state "
                "transitions — state reachability and transition "
                "legality are unverified"),
            synth_impact=(
                "Verification gap: FSMs are the #1 source of "
                "corner-case bugs; add assertions for illegal "
                "state, transition legality, and liveness"),
        ))
    return findings


def SVA_conflicting_assumptions(tree, file, signals):
    """SVA: Multiple assume properties on same signal may conflict."""
    findings = []
    assertions = _extract_assertions(tree)
    assumes = [a for a in assertions if a['type'] == 'assume']
    if len(assumes) < 2:
        return findings

    sig_assumes = {}
    for a in assumes:
        prop = a['property_text']
        sigs_in_prop = set(re.findall(r'\b([a-zA-Z_]\w*)\b', prop))
        sigs_in_prop -= {'posedge', 'negedge', 'disable', 'iff',
                         'property', 'assume', 'logic', 'bit'}
        for sig in sigs_in_prop:
            if sig not in sig_assumes:
                sig_assumes[sig] = []
            sig_assumes[sig].append(a)

    for sig, assume_list in sig_assumes.items():
        if len(assume_list) < 2:
            continue
        if sig not in signals:
            continue
        lines = [str(a['line']) for a in assume_list[:4]]
        findings.append(Finding(
            rule="SVA_conflict_assume", severity="warning",
            file=file, line=assume_list[0]['line'],
            message=(
                f"SVA: signal '{sig}' constrained by "
                f"{len(assume_list)} assume properties (lines "
                f"{', '.join(lines)}) — check for conflicting "
                f"constraints that may over-constrain formal"),
            synth_impact=(
                "Over-constraint: conflicting assumptions on the "
                "same signal can make the formal problem "
                "unsatisfiable, hiding all real bugs"),
        ))
    return findings


# ===================================================================
# Hierarchical / Multi-module Analysis — SpyGlass structural checks
# ===================================================================

def _extract_instances(tree):
    """Extract module instantiations from tree.

    Returns list of dicts:
        module: instantiated module name
        instance: instance name
        line: line number
        connections: dict of port_name -> connected_signal
        text: full instantiation text
    """
    instances = []
    for node in _find_nodes(tree.root_node, 'module_instantiation'):
        text = _node_text(node)
        line = _node_line(node)

        parts = text.split()
        if not parts:
            continue
        mod_name = parts[0]
        if mod_name in ('assign', 'always', 'always_ff',
                        'always_comb', 'wire', 'logic', 'reg',
                        'input', 'output', 'inout'):
            continue

        inst_m = re.search(r'(\w+)\s*\(', text)
        if not inst_m:
            continue
        inst_name_candidates = text.split()
        inst_name = ''
        for i, tok in enumerate(inst_name_candidates):
            if '(' in tok and i > 0:
                inst_name = inst_name_candidates[i].split('(')[0]
                if not inst_name and i > 1:
                    inst_name = inst_name_candidates[i - 1]
                break
        if inst_name == mod_name:
            inst_m2 = re.search(
                re.escape(mod_name) + r'\s+(?:#\(.*?\)\s+)?(\w+)\s*\(',
                text, re.DOTALL)
            inst_name = inst_m2.group(1) if inst_m2 else mod_name + '_i'

        connections = {}
        for pm in re.finditer(r'\.(\w+)\s*\(\s*(\w*)\s*\)', text):
            port = pm.group(1)
            sig = pm.group(2)
            connections[port] = sig

        instances.append({
            'module': mod_name, 'instance': inst_name or 'unknown',
            'line': line, 'connections': connections,
            'text': text,
        })
    return instances


def HIER_cdc_at_boundary(tree, file, signals):
    """HIER: Clock domain crossing at module boundary without documentation."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    input_signals = {s: si for s, si in signals.items()
                     if si.direction == 'input'}
    output_signals = {s: si for s, si in signals.items()
                      if si.direction == 'output'}

    for sig_name, si in input_signals.items():
        low = sig_name.lower()
        if any(k in low for k in ('clk', 'clock', 'rst', 'reset')):
            continue
        used_in_domains = []
        for clk, d in domains.items():
            if sig_name in d['read']:
                used_in_domains.append(clk)
        if len(used_in_domains) > 1:
            findings.append(Finding(
                rule="HIER_cdc_boundary", severity="warning",
                file=file, line=si.line,
                message=(
                    f"HIER: input '{sig_name}' used in multiple clock "
                    f"domains ({', '.join(used_in_domains)}) — CDC "
                    f"crossing at module boundary requires synchronizer "
                    f"or constraint documentation"),
                synth_impact=(
                    "Integration risk: input port feeding multiple "
                    "clock domains may need synchronizers that the "
                    "instantiating module cannot see"),
            ))
    return findings


def HIER_unconnected_clock_reset(tree, file, signals):
    """HIER: Clock or reset port left unconnected in instantiation."""
    findings = []
    instances = _extract_instances(tree)

    for inst in instances:
        for port, sig in inst['connections'].items():
            if not sig:
                low = port.lower()
                if any(k in low for k in ('clk', 'clock')):
                    findings.append(Finding(
                        rule="HIER_unconnected_clk", severity="error",
                        file=file, line=inst['line'],
                        message=(
                            f"HIER: clock port '.{port}' unconnected "
                            f"on instance '{inst['instance']}' "
                            f"({inst['module']}) — design will not "
                            f"function"),
                        synth_impact=(
                            "Fatal: unconnected clock port leaves "
                            "all downstream FFs unclocked"),
                    ))
                elif any(k in low for k in ('rst', 'reset')):
                    findings.append(Finding(
                        rule="HIER_unconnected_rst", severity="error",
                        file=file, line=inst['line'],
                        message=(
                            f"HIER: reset port '.{port}' unconnected "
                            f"on instance '{inst['instance']}' "
                            f"({inst['module']}) — FFs will not "
                            f"initialize"),
                        synth_impact=(
                            "No reset: unconnected reset port leaves "
                            "downstream FFs in unknown state after "
                            "power-on"),
                    ))
    return findings


def HIER_floating_output(tree, file, signals):
    """HIER: Instance output port not connected — wasted logic."""
    findings = []
    instances = _extract_instances(tree)

    for inst in instances:
        for port, sig in inst['connections'].items():
            if sig == '':
                low = port.lower()
                if any(k in low for k in ('clk', 'clock', 'rst',
                                           'reset')):
                    continue
                findings.append(Finding(
                    rule="HIER_floating_output", severity="info",
                    file=file, line=inst['line'],
                    message=(
                        f"HIER: port '.{port}' unconnected on "
                        f"instance '{inst['instance']}' "
                        f"({inst['module']}) — if output, logic "
                        f"driving it will be optimized away"),
                    synth_impact=(
                        "Area waste: unconnected output ports cause "
                        "synthesis to optimize away driving logic, "
                        "which may be intentional or a connection bug"),
                ))
    return findings


def HIER_feedthrough_signal(tree, file, signals):
    """HIER: Signal passes through module unmodified (feedthrough)."""
    findings = []
    text = _node_text(tree.root_node)

    inputs = {s: si for s, si in signals.items()
              if si.direction == 'input'}
    outputs = {s: si for s, si in signals.items()
               if si.direction == 'output'}

    for out_name, out_si in outputs.items():
        for m in re.finditer(
                r'assign\s+' + re.escape(out_name) +
                r'\s*=\s*(\w+)\s*;', text):
            src = m.group(1)
            if src in inputs:
                findings.append(Finding(
                    rule="HIER_feedthrough", severity="info",
                    file=file, line=out_si.line,
                    message=(
                        f"HIER: output '{out_name}' is direct "
                        f"feedthrough of input '{src}' — consider "
                        f"connecting at parent level instead"),
                    synth_impact=(
                        "Hierarchy overhead: feedthrough signals add "
                        "hierarchy crossings without logic function; "
                        "flatten or connect at instantiation"),
                ))
    return findings


def HIER_multi_clock_module(tree, file, signals):
    """HIER: Module uses multiple clocks without documenting domains."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) <= 1:
        return findings

    clk_ports = [s for s, si in signals.items()
                 if si.direction == 'input' and
                 any(k in s.lower() for k in ('clk', 'clock'))]

    if len(clk_ports) < len(domains):
        undocumented = set(domains.keys()) - set(clk_ports)
        if undocumented:
            findings.append(Finding(
                rule="HIER_implicit_clock", severity="warning",
                file=file, line=1,
                message=(
                    f"HIER: module has {len(domains)} clock domains "
                    f"but only {len(clk_ports)} clock ports — "
                    f"domains {', '.join(list(undocumented)[:3])} "
                    f"derived internally without port-level "
                    f"documentation"),
                synth_impact=(
                    "Integration risk: internally generated clocks "
                    "are invisible to parent-level CDC analysis and "
                    "timing constraints"),
            ))
    return findings


def HIER_clock_domain_port_doc(tree, file, signals):
    """HIER: Multi-clock module ports lack clock domain annotation."""
    findings = []
    domains = _build_clock_domain_map(tree)
    if len(domains) < 2:
        return findings

    text = _node_text(tree.root_node)
    io_sigs = {s: si for s, si in signals.items()
               if si.direction in ('input', 'output')
               and not any(k in s.lower()
                           for k in ('clk', 'clock', 'rst', 'reset'))}

    for sig_name, si in io_sigs.items():
        used_domains = []
        for clk, d in domains.items():
            if sig_name in d['read'] or sig_name in d['written']:
                used_domains.append(clk)
        if len(used_domains) == 1:
            clk = used_domains[0]
            above = text[:text.find(sig_name)]
            if above:
                last_comment_idx = above.rfind('//')
                if last_comment_idx >= 0:
                    comment_line = above[last_comment_idx:
                                         above.find('\n',
                                                    last_comment_idx)]
                    if clk.lower() in comment_line.lower() or \
                            'domain' in comment_line.lower():
                        continue
    return findings


def HIER_reset_domain_port(tree, file, signals):
    """HIER: Reset ports not documented with reset domain."""
    findings = []
    resets = _build_reset_map(tree, signals)
    if len(resets) < 2:
        return findings

    text = _node_text(tree.root_node)
    for rst_name, rst_info in resets.items():
        si = signals.get(rst_name)
        if not si or si.direction != 'input':
            continue
        if len(rst_info['used_by']) > 1:
            findings.append(Finding(
                rule="HIER_reset_multi_domain", severity="info",
                file=file, line=si.line,
                message=(
                    f"HIER: reset input '{rst_name}' used in "
                    f"{len(rst_info['used_by'])} clock domains "
                    f"({', '.join(rst_info['used_by'])}) — document "
                    f"reset domain mapping for integration"),
                synth_impact=(
                    "Integration: multi-domain reset inputs need "
                    "per-domain synchronizers at the parent level; "
                    "document which domain each reset serves"),
            ))
    return findings


def HIER_param_at_boundary(tree, file, signals):
    """HIER: Instance uses parameter override that changes port widths."""
    findings = []
    instances = _extract_instances(tree)
    text = _node_text(tree.root_node)

    for inst in instances:
        itext = inst['text']
        param_m = re.search(r'#\s*\((.+?)\)', itext, re.DOTALL)
        if not param_m:
            continue
        params = param_m.group(1)
        width_params = re.findall(
            r'\.(\w*(?:WIDTH|SIZE|DEPTH|ADDR_W|DATA_W)\w*)\s*\(\s*(\w+)',
            params, re.IGNORECASE)
        if not width_params:
            continue
        for pname, pval in width_params:
            try:
                val = int(pval)
                if val > 128:
                    findings.append(Finding(
                        rule="HIER_wide_param", severity="info",
                        file=file, line=inst['line'],
                        message=(
                            f"HIER: instance '{inst['instance']}' "
                            f"overrides .{pname}({pval}) — verify "
                            f"connected signal widths match at "
                            f"boundary"),
                        synth_impact=(
                            "Width mismatch risk: large parameter "
                            "overrides may cause implicit truncation "
                            "or zero-extension at port boundaries"),
                    ))
            except ValueError:
                pass
    return findings


# ===================================================================
# UPF Power-Intent Checks — verify RTL against power_soc.upf intent
# ===================================================================

def _upf_boundary_assigns(tree):
    """Continuous assigns: (lhs, rhs_text, {rhs_ids}, line).
    These are the domain-boundary nets where iso/level-shifter cells sit."""
    out = []
    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        for na in _find_nodes(ca, 'net_assignment'):
            lhs = _get_lhs_signal(na)
            if not lhs:
                continue
            kids = na.named_children
            rhs_text = _node_text(kids[-1]) if len(kids) >= 2 else ""
            rhs_ids = set()
            if len(kids) >= 2:
                for ident in _find_nodes(kids[-1], 'simple_identifier'):
                    rhs_ids.add(ident.text.decode())
            out.append((lhs, rhs_text, rhs_ids, _node_line(na)))
    return out


def _upf_self_ref_regs(tree):
    """Registers assigned from themselves in always_ff (accumulators/stateful
    signals that carry value across cycles → retention candidates)."""
    regs = set()
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_ff':
            continue
        for asgn in _find_nodes(always, 'nonblocking_assignment'):
            lhs = _get_lhs_signal(asgn)
            if not lhs:
                continue
            kids = asgn.named_children
            if len(kids) >= 2:
                rhs_ids = {i.text.decode()
                           for i in _find_nodes(kids[-1], 'simple_identifier')}
                if lhs in rhs_ids:
                    regs.add(lhs)
    return regs


def UPF_missing_isolation(tree, file, signals):
    """A switchable-domain signal crosses to another domain without an
    isolation clamp, though the domain's isolation strategy requires it."""
    findings = []
    upf = _ACTIVE_UPF
    if not upf or not upf.domains:
        return findings
    for lhs, rhs_text, rhs_ids, line in _upf_boundary_assigns(tree):
        dst_dom = upf.domain_of(lhs)
        for rid in rhs_ids:
            src_dom = upf.domain_of(rid)
            if src_dom == dst_dom or not upf.is_switchable(src_dom):
                continue
            iso = upf.isolation_for(src_dom)
            if iso and iso.applies_to in ('outputs', 'both'):
                # Protected if the clamp control appears in the RHS.
                if iso.isolation_signal and iso.isolation_signal in rhs_text:
                    continue
                findings.append(Finding(
                    rule="UPF_missing_isolation", severity="error",
                    file=file, line=line,
                    message=(
                        f"'{rid}' crosses from switchable domain {src_dom} "
                        f"to {dst_dom} (via '{lhs}') without isolation clamp; "
                        f"strategy '{iso.name}' requires "
                        f"'{iso.isolation_signal}' gating"),
                    synth_impact=(
                        "Floating/corrupt value: when the source domain is "
                        "powered down, an unclamped crossing propagates X into "
                        "the always-on domain; add the isolation cell"),
                ))
            elif not iso:
                findings.append(Finding(
                    rule="UPF_missing_isolation", severity="error",
                    file=file, line=line,
                    message=(
                        f"'{rid}' crosses from switchable domain {src_dom} "
                        f"to {dst_dom} (via '{lhs}') but no isolation strategy "
                        f"is defined for {src_dom}"),
                    synth_impact=(
                        "No isolation intent: outputs of a power-gated domain "
                        "must be clamped; add set_isolation for " + src_dom),
                ))
    return findings


def UPF_missing_level_shifter(tree, file, signals):
    """A crossing between domains at different voltages has no level shifter."""
    findings = []
    upf = _ACTIVE_UPF
    if not upf or not upf.domains:
        return findings
    for lhs, rhs_text, rhs_ids, line in _upf_boundary_assigns(tree):
        dst_dom = upf.domain_of(lhs)
        dst_v = upf.domain_voltage(dst_dom)
        for rid in rhs_ids:
            src_dom = upf.domain_of(rid)
            if src_dom == dst_dom:
                continue
            src_v = upf.domain_voltage(src_dom)
            if src_v is None or dst_v is None or abs(src_v - dst_v) < 1e-6:
                continue
            if upf.level_shifter_for(src_dom):
                continue
            direction = "low_to_high" if src_v < dst_v else "high_to_low"
            findings.append(Finding(
                rule="UPF_missing_level_shifter", severity="error",
                file=file, line=line,
                message=(
                    f"'{rid}' crosses {src_dom} ({src_v:.2g}V) -> {dst_dom} "
                    f"({dst_v:.2g}V) via '{lhs}' with no level shifter "
                    f"({direction})"),
                synth_impact=(
                    "Voltage mismatch: a signal driven at one rail and sampled "
                    "at another needs a level shifter or it may not switch "
                    "reliably; add set_level_shifter for " + src_dom),
            ))
    return findings


def UPF_retention_gap(tree, file, signals):
    """A stateful register in a switchable domain is excluded from the
    domain's retention strategy, so its value is lost across power-down."""
    findings = []
    upf = _ACTIVE_UPF
    if not upf or not upf.domains:
        return findings
    self_regs = _upf_self_ref_regs(tree)
    for dname, dom in upf.domains.items():
        if not upf.is_switchable(dname):
            continue
        ret = upf.retention_for(dname)
        if not ret or not ret.elements:
            continue  # whole-domain retention (or none) — not a partial gap
        retained = set(ret.elements)
        for elem in dom.elements:
            if elem in retained:
                continue
            if elem not in self_regs:
                continue  # transient reg (not self-accumulating) — losing it is fine
            si = signals.get(elem)
            findings.append(Finding(
                rule="UPF_retention_gap", severity="warning",
                file=file, line=si.line if si else 0,
                message=(
                    f"stateful register '{elem}' in switchable domain {dname} "
                    f"is not in retention strategy '{ret.name}' "
                    f"(-elements {{{' '.join(ret.elements)}}}); its value is "
                    f"lost on power-down"),
                synth_impact=(
                    "State loss: the AON controller save/restores this domain, "
                    "but this accumulator is not retained; add it to the "
                    "retention element list"),
            ))
    return findings


def UPF_unused_iso_control(tree, file, signals):
    """An RTL isolation-enable signal is driven but no UPF isolation strategy
    consumes it (dead power-control intent)."""
    findings = []
    upf = _ACTIVE_UPF
    if not upf or not upf.domains:
        return findings
    used = {iso.isolation_signal for iso in upf.isolations.values()
            if iso.isolation_signal}
    written, _read = _build_signal_rw_map(tree.root_node)
    for sig in written:
        low = sig.lower()
        if 'iso' not in low:
            continue
        if sig in used:
            continue
        si = signals.get(sig)
        line = min(written[sig]) if written.get(sig) else (si.line if si else 0)
        findings.append(Finding(
            rule="UPF_unused_iso_control", severity="warning",
            file=file, line=line,
            message=(
                f"isolation-control signal '{sig}' is driven in RTL but no "
                f"set_isolation strategy references it"),
            synth_impact=(
                "Dead power intent: this looks like an isolation enable, but "
                "the UPF defines no matching isolation cell — either wire it "
                "into a strategy or remove it"),
        ))
    return findings


UPF_CHECKS = [
    UPF_missing_isolation,
    UPF_missing_level_shifter,
    UPF_retention_gap,
    UPF_unused_iso_control,
]


# ===================================================================
# ALL CHECKS — master list
# ===================================================================

ALL_CHECKS = [
    # W-series Lint (original)
    W336_blocking_in_sequential,
    W505_mixed_assignments,
    W263_case_no_default,
    W402_latch_inference,
    W164_width_mismatch,
    W528_unused_signal,
    W120_unread_variable,
    W240_input_not_read,
    W287_output_not_driven,
    W289_assign_to_input,
    W391_no_output,
    W415_sensitivity_list,
    W446_part_select_oob,
    W480_loop_var_type,
    W494_non_full_case,
    W_duplicate_case,
    W_multiple_drivers,
    W_multi_seq_assign,
    # W-series Lint (additional)
    W213_signed_unsigned,
    W224_multibit_boolean,
    W443_casex_usage,
    W456_many_ports,
    W468_arith_overflow,
    W497_undriven_net,
    W362_output_partial,
    W341_non_constant_case,
    # Synthesis (original)
    SYNTH_initial_block,
    SYNTH_delay,
    SYNTH_real_type,
    SYNTH_integer_type,
    SYNTH_system_tasks,
    SYNTH_tristate,
    SYNTH_latch_always,
    # Synthesis (additional)
    SYNTH_while_loop,
    SYNTH_forever_loop,
    SYNTH_for_non_constant,
    SYNTH_string_type,
    SYNTH_time_type,
    SYNTH_class_usage,
    SYNTH_unique_priority,
    SYNTH_disable_iff,
    SYNTH_event_in_comb,
    SYNTH_recursive_func,
    # Functional / Semantic (original)
    FUNC_comparison_oor,
    FUNC_counter_overflow,
    FUNC_comb_loop,
    FUNC_comb_depth,
    FUNC_case_width_mismatch,
    FUNC_magic_numbers,
    FUNC_nonblocking_in_comb,
    FUNC_assign_x,
    # Functional (additional)
    FUNC_shift_overflow,
    FUNC_divide_power2,
    FUNC_constant_if,
    FUNC_async_set_reset,
    FUNC_read_before_write,
    FUNC_truncation_assign,
    FUNC_zero_width_concat,
    # Clock / Reset (original)
    CLK_no_reset,
    CLK_async_reset_data,
    CLK_mixed_edge,
    CLK_gated_clock,
    # Clock / Reset (additional)
    CLK_multiple_clocks,
    CLK_data_as_clock,
    CLK_reset_polarity_mix,
    # Power (original)
    PWR_clock_gating_opp,
    PWR_large_mux,
    # Power (additional)
    PWR_wide_bus_no_enable,
    PWR_constant_output,
    PWR_redundant_assign,
    # Memory (original + additional)
    MEM_array_size,
    MEM_no_reset,
    MEM_read_write_conflict,
    # FSM (original + additional)
    FSM_encoding,
    FSM_unreachable_state,
    FSM_no_default_transition,
    # STARC methodology
    STARC_if_else_chain,
    STARC_register_output,
    STARC_no_casex,
    STARC_reset_constant,
    STARC_one_always_one_signal,
    # Style / Naming (original)
    STYLE_module_name,
    STYLE_active_low_suffix,
    STYLE_clk_naming,
    STYLE_tab_indent,
    STYLE_line_length,
    # Style (additional)
    STYLE_param_uppercase,
    STYLE_begin_end_single,
    STYLE_trailing_whitespace,
    STYLE_port_direction_group,
    # Simulation (original)
    SIM_force_release,
    SIM_deassign,
    SIM_event_trigger,
    # Simulation (additional)
    SIM_wait_statement,
    SIM_fork_join,
    SIM_disable_statement,
    SIM_timeformat,
    # Structural (original)
    STRUCT_empty_block,
    STRUCT_nested_ternary,
    STRUCT_assign_in_cond,
    STRUCT_recursive_assign,
    STRUCT_param_no_default,
    STRUCT_unused_param,
    STRUCT_always_star_recommend,
    STRUCT_continuous_assign_to_reg,
    # Structural (additional)
    STRUCT_multiple_modules,
    STRUCT_deep_nesting,
    STRUCT_generate_no_label,
    STRUCT_positional_port,
    STRUCT_task_in_synth,
    STRUCT_function_void,
    STRUCT_wire_reg_conflict,
    # Cross-module / Instantiation
    CROSS_unconnected_port,
    CROSS_width_override,
    # W-series (batch 3)
    W116_inout_usage,
    W484_operator_precedence,
    W529_port_width_connect,
    W293_block_label_mismatch,
    W182_signal_redeclared,
    W192_unused_function,
    # Functional (batch 3)
    FUNC_compare_self,
    FUNC_reduction_1bit,
    FUNC_bitwise_vs_logical,
    FUNC_unsigned_subtraction,
    FUNC_full_case_overlap,
    FUNC_index_variable_width,
    # Clock (batch 3)
    CLK_negedge_data,
    CLK_clock_divider,
    # Synthesis (batch 3)
    SYNTH_program_block,
    SYNTH_chandle_type,
    SYNTH_semaphore,
    SYNTH_covergroup,
    SYNTH_assert_no_translate,
    # Style (batch 3)
    STYLE_wildcard_import,
    STYLE_magic_delay,
    STYLE_consistent_reset,
    STYLE_endmodule_comment,
    # Structural (batch 3)
    STRUCT_empty_module,
    STRUCT_recursive_instance,
    STRUCT_ifdef_balance,
    STRUCT_timescale,
    STRUCT_implicit_net,
    # FSM (batch 3)
    FSM_dead_state,
    FSM_no_idle_reset,
    # Memory (batch 3)
    MEM_async_read,
    # STARC (batch 3)
    STARC_sync_reset_preference,
    STARC_clock_enable_pattern,
    # Power (batch 3)
    PWR_shift_vs_multiply,
    PWR_unnecessary_wide_op,
    # Simulation (batch 3)
    SIM_specify_block,
    SIM_assert_immediate,
    # Cross-module (batch 3)
    CROSS_instance_array,
    CROSS_missing_connection,
    # CDC (Clock Domain Crossing) — SpyGlass Ac_cdc equivalent
    CDC_missing_synchronizer,       # Ac_cdc01
    CDC_multi_bit_crossing,         # Ac_cdc02
    CDC_combo_logic_before_sync,    # Ac_cdc03
    CDC_reconvergence,              # Ac_cdc04 + Ac_conv
    CDC_reset_crossing,             # Ac_cdc05
    CDC_fifo_pointer,               # Ac_cdc06
    CDC_mux_sync,                   # Ac_cdc07
    CDC_handshake,                  # Ac_cdc08
    CDC_pulse_crossing,             # Ac_cdc09
    CDC_clock_gating,               # Ac_cdc10
    # RDC (Reset Domain Crossing)
    RDC_async_reset_no_sync,
    RDC_combo_reset_path,
    RDC_reset_glitch,
    RDC_missing_reset_filter,
    RDC_mixed_reset_polarity,
    # DFT (Design for Testability) — SpyGlass DFT equivalent
    DFT_non_scannable_ff,
    DFT_clock_not_controllable,
    DFT_async_set_reset_scan,
    DFT_memory_no_bist,
    DFT_tristate_in_scan,
    DFT_combo_loop_scan,
    DFT_observe_internal,
    DFT_gated_clock_mux,
    # SVA / Formal Intent — SpyGlass assertion checks
    SVA_missing_clock,
    SVA_no_reset_disable,
    SVA_vacuous_implication,
    SVA_unbounded_liveness,
    SVA_assume_in_design,
    SVA_missing_else_action,
    SVA_cover_missing,
    SVA_fsm_no_assertions,
    SVA_conflicting_assumptions,
    # Hierarchical / Multi-module — SpyGlass cross-hierarchy
    HIER_cdc_at_boundary,
    HIER_unconnected_clock_reset,
    HIER_floating_output,
    HIER_feedthrough_signal,
    HIER_multi_clock_module,
    HIER_clock_domain_port_doc,
    HIER_reset_domain_port,
    HIER_param_at_boundary,
    # UPF power-intent (active only when a .upf is supplied)
    *UPF_CHECKS,
]


# ===================================================================
# Tier-2 Structural Checkers (SpyGlass Phase 5)
# These receive DesignContext with synth data, netlist, and elaboration
# ===================================================================

def STRUCT_mux_chain_deep(tree, file, signals, ctx=None):
    """Deep MUX chain on signal path — timing bottleneck."""
    if not ctx or not ctx.netlist:
        return []
    findings = []
    for net_name, net in ctx.netlist.nets.items():
        depth = ctx.netlist.mux_chain_depth(net_name)
        if depth >= 5:
            driver_cell = ctx.netlist.cells.get(net.driver) if net.driver else None
            line = driver_cell.line if driver_cell else 0
            findings.append(Finding(
                rule="STRUCT_mux_chain_deep", severity="warning",
                file=file, line=line,
                message=f"Signal '{net_name}' passes through {depth} cascaded MUX levels",
                synth_impact=f"Deep MUX chain adds ~{depth * 0.10:.1f}ns combinational delay; "
                             f"consider registering intermediate results or restructuring logic",
            ))
    return findings


def STRUCT_high_fanout_net(tree, file, signals, ctx=None):
    """Net drives too many cells — may need buffering."""
    if not ctx or not ctx.netlist:
        return []
    findings = []
    for net_name, fanout_count in ctx.netlist.high_fanout_nets(threshold=16):
        net = ctx.netlist.nets.get(net_name)
        driver_cell = ctx.netlist.cells.get(net.driver) if net and net.driver else None
        line = driver_cell.line if driver_cell else 0
        findings.append(Finding(
            rule="STRUCT_high_fanout_net", severity="info",
            file=file, line=line,
            message=f"Net '{net_name}' has fanout of {fanout_count} — synthesis will insert buffers",
            synth_impact="High fanout increases routing congestion and may create timing violations; "
                         "consider explicit buffer tree or register duplication",
        ))
    return findings


def STRUCT_comb_path_deep(tree, file, signals, ctx=None):
    """Long combinational path from netlist analysis."""
    if not ctx or not ctx.netlist:
        return []
    findings = []
    checked = set()
    for cell in ctx.netlist.cells_of_type('FF'):
        for inp in cell.inputs:
            if inp in checked:
                continue
            checked.add(inp)
            depth = ctx.netlist.comb_depth_to(inp)
            if depth >= 6:
                findings.append(Finding(
                    rule="STRUCT_comb_path_deep", severity="warning",
                    file=file, line=cell.line,
                    message=f"FF '{cell.name}' has {depth}-level combinational input path",
                    synth_impact=f"Deep combinational path (~{depth * 0.10:.1f}ns) limits clock frequency; "
                                 f"consider pipeline registers",
                ))
    return findings


def STRUCT_latch_feeds_ff(tree, file, signals, ctx=None):
    """Latch output feeds into FF — problematic timing."""
    if not ctx or not ctx.netlist:
        return []
    findings = []
    latch_outputs = set()
    for cell in ctx.netlist.cells_of_type('LATCH'):
        latch_outputs.update(cell.outputs)
    for cell in ctx.netlist.cells_of_type('FF'):
        for inp in cell.inputs:
            chain = ctx.netlist.driver_chain(inp, max_depth=5)
            for c in chain:
                if c.cell_type == 'LATCH':
                    findings.append(Finding(
                        rule="STRUCT_latch_feeds_ff", severity="warning",
                        file=file, line=cell.line,
                        message=f"Latch '{c.name}' feeds FF '{cell.name}' — timing analysis unreliable",
                        synth_impact="Latch-to-FF paths have time-borrowing that complicates STA; "
                                     "replace latch with FF or add pipeline register",
                    ))
                    break
    return findings


def STRUCT_mult_on_critical(tree, file, signals, ctx=None):
    """Multiplier on potential critical path."""
    if not ctx or not ctx.netlist:
        return []
    findings = []
    synth = ctx.synth_data
    if not synth:
        return []
    timing = synth.get('timing', {})
    if timing.get('arith_delay_ns', 0) < 1.0:
        return []
    for cell in ctx.netlist.cells_of_type('MULT'):
        if cell.width > 16:
            findings.append(Finding(
                rule="STRUCT_mult_on_critical", severity="info",
                file=file, line=cell.line,
                message=f"Large multiplier '{cell.name}' ({cell.width}-bit) on datapath — "
                        f"may be critical path",
                synth_impact="FPGA: map to DSP48 block | ASIC: consider pipelined multiplier",
            ))
    return findings


def STRUCT_ungated_wide_bus(tree, file, signals, ctx=None):
    """Wide registered bus without clock enable — wastes dynamic power."""
    if not ctx or not ctx.synth_data:
        return []
    findings = []
    synth = ctx.synth_data
    ce_sigs = set(synth.get('ce_signals', []))
    ff_sigs = synth.get('ff_signals', {})
    for sig, width in ff_sigs.items():
        if width >= 8 and sig not in ce_sigs:
            findings.append(Finding(
                rule="STRUCT_ungated_wide_bus", severity="info",
                file=file, line=0,
                message=f"Register '{sig}' ({width}-bit) has no clock enable — "
                        f"toggles every cycle",
                synth_impact="Add clock enable to gate toggling when data is unchanged; "
                             f"saves ~{width * 0.01:.2f}mW dynamic power (est.)",
            ))
    return findings


def STRUCT_memory_vs_ff(tree, file, signals, ctx=None):
    """Large array synthesized as FFs — should use BRAM/SRAM."""
    if not ctx or not ctx.synth_data:
        return []
    findings = []
    mem = ctx.synth_data.get('memory', {})
    for detail in mem.get('details', []):
        total = detail['width'] * detail['depth']
        if total >= 512 and total < 1024:
            findings.append(Finding(
                rule="STRUCT_memory_vs_ff", severity="info",
                file=file, line=detail['line'],
                message=f"Array ({detail['width']}x{detail['depth']} = {total} bits) "
                        f"near BRAM threshold — synthesizes as {total * 6} gate-equivalent FFs",
                synth_impact="Consider restructuring to meet BRAM inference threshold (1024+ bits)",
            ))
    return findings


def STRUCT_fsm_encoding_waste(tree, file, signals, ctx=None):
    """FSM uses more encoding bits than needed."""
    if not ctx or not ctx.synth_data:
        return []
    findings = []
    fsm = ctx.synth_data.get('fsm', {})
    states = fsm.get('states', 0)
    enc_bits = fsm.get('encoding_bits', 0)
    min_bits = fsm.get('min_bits', 0)
    if states > 0 and enc_bits > min_bits + 1 and enc_bits > 2:
        findings.append(Finding(
            rule="STRUCT_fsm_encoding_waste", severity="info",
            file=file, line=0,
            message=f"FSM uses {enc_bits}-bit encoding for {states} states "
                    f"(minimum {min_bits} bits needed)",
            synth_impact=f"Over-encoded FSM wastes {enc_bits - min_bits} register bits; "
                         f"synthesis may optimize but explicit encoding gives more control",
        ))
    return findings


def STRUCT_const_prop_opportunity(tree, file, signals, ctx=None):
    """Dead branches detected by constant propagation."""
    if not ctx or not ctx.synth_data:
        return []
    findings = []
    opt = ctx.synth_data.get('optimization', {})
    dead = opt.get('dead_branches', 0)
    const_outs = opt.get('constant_outputs', [])
    if dead > 0:
        findings.append(Finding(
            rule="STRUCT_const_prop", severity="info",
            file=file, line=0,
            message=f"Constant propagation found {dead} dead branch(es) — "
                    f"synthesis will optimize away",
            synth_impact="Dead branches waste area until synthesis removes them; "
                         "consider cleaning up RTL for clarity",
        ))
    for sig in const_outs[:3]:
        findings.append(Finding(
            rule="STRUCT_const_output", severity="info",
            file=file, line=0,
            message=f"Output '{sig}' is constant — synthesis will tie to fixed value",
            synth_impact="Constant output may indicate unused feature or parameterization issue",
        ))
    return findings


def STRUCT_cross_module_width(tree, file, signals, ctx=None):
    """Port width mismatches found during cross-module elaboration with resolved params."""
    if not ctx or not ctx.elab_result:
        return []
    elab = ctx.elab_result
    findings = []
    for key, resolved in elab.resolved_instances.items():
        parent_mod = key.split('.')[0]
        parent_mi = elab.module_db.get(parent_mod)
        if not parent_mi or parent_mi.file != file:
            continue
        child_mod = resolved['module']
        child_mi = elab.module_db.get(child_mod)
        if not child_mi:
            continue
        inst_name = key.split('.', 1)[1]
        inst = None
        for i in parent_mi.instances:
            if i.instance_name == inst_name:
                inst = i
                break
        if not inst:
            continue
        for pc in inst.port_connections:
            if not pc.signal_expr or '[' in pc.signal_expr:
                continue
            resolved_port = resolved['ports'].get(pc.port_name)
            default_port = child_mi.ports.get(pc.port_name)
            if not resolved_port or not default_port:
                continue
            if resolved_port.width != default_port.width:
                parent_sig = parent_mi.signals.get(pc.signal_expr)
                if not parent_sig or parent_sig.width <= 0:
                    continue
                if parent_sig.width != resolved_port.width:
                    r_params = resolved.get('params', {})
                    changed = {k: v for k, v in r_params.items()
                               if child_mi.params.get(k) != v}
                    findings.append(Finding(
                        rule="STRUCT_cross_module_width",
                        severity="warning",
                        file=file, line=pc.line,
                        message=(f"After param override "
                                 f"({', '.join(f'{k}={v}' for k, v in changed.items())}), "
                                 f"port '.{pc.port_name}' is {resolved_port.width}b "
                                 f"but signal '{pc.signal_expr}' is {parent_sig.width}b"),
                        synth_impact="Width mismatch with parameterized instance",
                    ))
    return findings


TIER2_CHECKS = [
    STRUCT_mux_chain_deep,
    STRUCT_high_fanout_net,
    STRUCT_comb_path_deep,
    STRUCT_latch_feeds_ff,
    STRUCT_mult_on_critical,
    STRUCT_ungated_wide_bus,
    STRUCT_memory_vs_ff,
    STRUCT_fsm_encoding_waste,
    STRUCT_const_prop_opportunity,
    STRUCT_cross_module_width,
]


# ===================================================================
# Main analyzer
# ===================================================================

_CONFIDENCE_MAP = {
    'W_multi_driver': 100, 'W_multi_seq_assign': 100,
    'W402_latch_inferred': 100, 'SYNTH_12608_latch': 100,
    'W116_width_mismatch': 99, 'W164_width_mismatch': 99,
    'W224_multibit_bool': 99, 'FUNC_truncation': 99,
    'W289_assign_to_input': 99,
    'W336_blocking_in_seq': 95, 'W505_mixed_assign': 95,
    'FUNC_comb_loop': 95,
    'STRUCT_self_assign': 90, 'FUNC_compare_self': 90,
    'CDC_no_sync': 95, 'CDC_multi_bit': 95, 'CDC_async_reset': 95,
    'CDC_glitch_combo': 90, 'CDC_fifo_ptr': 90,
    'CDC_handshake_req': 90, 'CDC_handshake_ack': 90,
    'CDC_reconvergence': 85, 'CDC_convergence': 80,
    'CDC_pulse_sync': 75, 'CDC_mux_data_unstable': 75,
    'CDC_gated_sync_clk': 70,
    'RDC_async_no_sync': 95, 'RDC_glitch_reset': 90,
    'RDC_combo_reset': 90, 'RDC_no_filter': 80,
    'RDC_mixed_polarity': 70,
    'DFT_nonscan_ff': 85, 'DFT_clock_no_bypass': 95,
    'DFT_async_ctrl_scan': 85, 'DFT_mem_no_bist': 80,
    'DFT_tristate_scan': 95, 'DFT_combo_loop': 95,
    'DFT_low_observe': 60, 'DFT_clk_mux_glitch': 80,
    'SVA_no_clock': 99, 'SVA_no_disable_iff': 75,
    'SVA_vacuous': 99, 'SVA_unbounded': 85,
    'SVA_assume_in_rtl': 80, 'SVA_no_else_action': 50,
    'SVA_no_cover': 60, 'SVA_fsm_uncovered': 55,
    'SVA_conflict_assume': 75,
    'HIER_cdc_boundary': 85, 'HIER_unconnected_clk': 99,
    'HIER_unconnected_rst': 99, 'HIER_floating_output': 70,
    'HIER_feedthrough': 90, 'HIER_implicit_clock': 80,
    'HIER_reset_multi_domain': 70, 'HIER_wide_param': 60,
    'FUNC_counter_overflow': 65, 'FUNC_cmp_out_of_range': 65,
    'CLK_multi_domain': 60,
}


def _learned_rule_confidence(rule_name: str) -> int:
    """Get per-rule confidence from tracked accuracy stats."""
    kb = _load_knowledge()
    for r in kb.get("learned_rules", []):
        if r.get("name") == rule_name:
            stats = r.get("stats")
            if stats and stats.get("runs", 0) >= 2:
                acc = stats.get("accuracy", 0.5)
                return max(10, min(80, int(acc * 80)))
    return 50


def _assign_confidence(findings: list[Finding]) -> None:
    for f in findings:
        if f.confidence > 0:
            continue
        if f.rule in _CONFIDENCE_MAP:
            f.confidence = _CONFIDENCE_MAP[f.rule]
        elif f.rule.startswith('STYLE_'):
            f.confidence = 20
        elif f.rule.startswith('STRUCT_'):
            f.confidence = 50
        elif f.rule.startswith('SYNTH_'):
            f.confidence = 85
        elif f.rule.startswith('FUNC_'):
            f.confidence = 75
        elif f.rule.startswith('CLK_'):
            f.confidence = 80
        elif f.rule.startswith('PWR_'):
            f.confidence = 60
        elif f.rule.startswith('MEM_'):
            f.confidence = 70
        elif f.rule.startswith('FSM_'):
            f.confidence = 65
        elif f.rule.startswith('SIM_'):
            f.confidence = 85
        elif f.rule.startswith('STARC_'):
            f.confidence = 55
        elif f.rule.startswith('CROSS_'):
            f.confidence = 80
        elif f.rule.startswith('W'):
            f.confidence = 80
        elif f.rule.startswith('LEARNED_'):
            f.confidence = _learned_rule_confidence(f.rule)
        elif f.rule.startswith('AGENT_'):
            f.confidence = 60
        elif f.rule.startswith('ELAB_'):
            f.confidence = 85
        elif f.rule.startswith('CDC_'):
            f.confidence = 85
        elif f.rule.startswith('UPF_'):
            f.confidence = 85
        elif f.rule.startswith('RDC_'):
            f.confidence = 85
        elif f.rule.startswith('DFT_'):
            f.confidence = 80
        elif f.rule.startswith('SVA_'):
            f.confidence = 70
        elif f.rule.startswith('HIER_'):
            f.confidence = 75
        else:
            f.confidence = 50


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Suppress redundant findings — keep the strongest diagnostic per issue.

    SpyGlass behavior: when a stronger rule already explains the bug,
    suppress weaker/duplicate rules for the same signal or line.
    """
    # Index by signal name and line for cross-rule suppression
    rules_by_line: dict[int, set[str]] = {}
    signals_by_rule: dict[str, set[str]] = {}
    for f in findings:
        rules_by_line.setdefault(f.line, set()).add(f.rule)
        m = re.search(r"'(\w+)'", f.message)
        if m:
            signals_by_rule.setdefault(f.rule, set()).add(m.group(1))

    # Signals that have FUNC_cmp_out_of_range or FUNC_counter_overflow
    strong_cmp_signals = (
        signals_by_rule.get("FUNC_cmp_out_of_range", set()) |
        signals_by_rule.get("FUNC_counter_overflow", set()))

    # Signals with W_multi_driver
    multi_driver_sigs = signals_by_rule.get("W_multi_driver", set())

    # Signals with W402_latch_inferred
    latch_sigs = signals_by_rule.get("W402_latch_inferred", set())

    # Lines with W263_case_no_default
    case_no_default_lines = {f.line for f in findings
                             if f.rule == "W263_case_no_default"}

    out = []
    for f in findings:
        sig = ""
        m = re.search(r"'(\w+)'", f.message)
        if m:
            sig = m.group(1)

        # 1. Suppress FSM_no_default if W263_case_no_default on same line
        if f.rule == "FSM_no_default" and f.line in case_no_default_lines:
            continue

        # 2. Suppress W_multi_seq_assign if W_multi_driver covers same signal
        if f.rule == "W_multi_seq_assign" and sig in multi_driver_sigs:
            continue

        # 3. Suppress SYNTH_12608_latch if W402_latch_inferred on same signal
        if f.rule == "SYNTH_12608_latch" and sig in latch_sigs:
            continue

        # 4. Suppress W362_output_partial if W402 latch on same signal
        if f.rule == "W362_output_partial" and sig in latch_sigs:
            continue

        # 5. Suppress W213_sign_compare if stronger cmp rule on same line
        if f.rule == "W213_sign_compare":
            line_rules = rules_by_line.get(f.line, set())
            if line_rules & {"FUNC_cmp_out_of_range", "FUNC_counter_overflow"}:
                continue

        # 6. Suppress LEARNED_ latch/comb rules when W402 already covers it
        if f.rule.startswith("LEARNED_") and "latch" in f.message.lower():
            if latch_sigs:
                continue
        if f.rule.startswith("LEARNED_COMB"):
            latch_lines = {lf.line for lf in findings
                           if lf.rule == "W402_latch_inferred"}
            if latch_lines:
                continue

        # 7. Suppress CDC_no_sync when a more specific CDC rule covers same signal
        if f.rule == "CDC_no_sync":
            sig_in_msg = re.search(r"'(\w+)'", f.message)
            if sig_in_msg:
                cdc_sig = sig_in_msg.group(1)
                specific_cdc = {
                    ff.rule for ff in findings
                    if ff.rule in ("CDC_multi_bit", "CDC_async_reset")
                    and f"'{cdc_sig}'" in ff.message
                }
                if specific_cdc:
                    continue

        out.append(f)

    # 8. Merge multi-driver findings into richer message
    merged = []
    seen_multi = set()
    for f in out:
        if f.rule == "W_multi_driver":
            if f.message in seen_multi:
                continue
            seen_multi.add(f.message)
        merged.append(f)

    _assign_confidence(merged)
    return merged


def analyze_file(filepath: str) -> list[Finding]:
    with open(filepath, 'rb') as f:
        src = f.read()

    tree = _PARSER.parse(src)
    filename = os.path.basename(filepath)
    signals = _elaborate_from_text(src.decode('utf-8', errors='replace'))

    findings = []
    for check in ALL_CHECKS:
        try:
            findings.extend(check(tree, filename, signals))
        except Exception:
            pass
    return _deduplicate_findings(findings)


def analyze_design(rtl_files: list[str],
                   sdc_files: list[str] | None = None,
                   upf_files: list[str] | None = None) -> AnalysisResult:
    """5-phase SpyGlass-style analysis pipeline:
      Phase 1: Parse (tree-sitter AST + SDC/UPF constraints)
      Phase 2: Elaborate (per-file signals + cross-module hierarchy)
      Phase 3: Tier-1 language rules (fast, no extra context)
      Phase 4: Quick synthesis (SynthesisEstimator + InferredNetlist)
      Phase 5: Tier-2 structural rules (netlist/synth/elab context)
    """
    global _ACTIVE_SDC, _ACTIVE_UPF
    result = AnalysisResult(files=list(rtl_files))

    sdc = SDCConstraints()
    for sdc_path in (sdc_files or []):
        if os.path.isfile(sdc_path):
            try:
                file_sdc = parse_sdc_file(sdc_path)
                sdc.clocks.update(file_sdc.clocks)
                sdc.generated_clocks.update(file_sdc.generated_clocks)
                sdc.false_paths.extend(file_sdc.false_paths)
                sdc.multicycle_paths.extend(file_sdc.multicycle_paths)
                sdc.clock_groups.extend(file_sdc.clock_groups)
                sdc.max_delays.extend(file_sdc.max_delays)
                sdc.min_delays.extend(file_sdc.min_delays)
                sdc.input_delays.extend(file_sdc.input_delays)
                sdc.output_delays.extend(file_sdc.output_delays)
            except Exception:
                pass
    _ACTIVE_SDC = sdc if sdc.clocks or sdc.false_paths or sdc.clock_groups else None

    upf = UPFConstraints()
    for upf_path in (upf_files or []):
        if os.path.isfile(upf_path):
            try:
                file_upf = parse_upf_file(upf_path)
                upf.domains.update(file_upf.domains)
                upf.switches.update(file_upf.switches)
                upf.isolations.update(file_upf.isolations)
                upf.retentions.update(file_upf.retentions)
                upf.level_shifters.update(file_upf.level_shifters)
                upf.supply_voltage.update(file_upf.supply_voltage)
                upf.supply_nets.extend(file_upf.supply_nets)
                if file_upf.top:
                    upf.top = file_upf.top
            except Exception:
                pass
    _ACTIVE_UPF = upf if upf.domains else None

    # ── Phase 1: Parse ──────────────────────────────────────────────
    parsed_trees = {}
    parsed_sources = {}
    parsed_signals = {}
    signal_rw_maps = {}
    parsed_elab = {}

    for f in rtl_files:
        if os.path.isfile(f):
            with open(f, 'rb') as fh:
                src = fh.read()
            tree = _PARSER.parse(src)
            fname = os.path.basename(f)
            text = src.decode('utf-8', errors='replace')
            signals = _elaborate_from_text(text)

            parsed_trees[fname] = tree
            parsed_sources[fname] = src
            parsed_signals[fname] = signals
            signal_rw_maps[fname] = _build_signal_rw_map(tree.root_node)
            # Phase 2a: one elaboration model per file, queried by all phases.
            parsed_elab[fname] = build_elaboration_model(tree, signals, text)

    # ── Phase 2: Elaborate (cross-module + param propagation) ────────
    elab_result = None
    elab_measurements = {}
    has_multiple_modules = sum(
        len(_find_nodes(tree.root_node, 'module_declaration'))
        for tree in parsed_trees.values()
    ) > 1
    if len(rtl_files) > 1 or has_multiple_modules:
        try:
            elaborator = DesignElaborator()
            elaborator.parse_files(rtl_files)
            elab_result = elaborator.elaborate()
            result.findings.extend(elab_result.findings)
            elab_measurements = {
                "cogni.elab.modules": len(elab_result.module_db),
                "cogni.elab.top_modules": elab_result.top_modules,
                "cogni.elab.hierarchy": elaborator.format_hierarchy(elab_result),
            }
        except Exception:
            pass

    # ── Phase 3: Tier-1 language rules ──────────────────────────────
    global _ACTIVE_ELAB
    for fname in parsed_trees:
        tree = parsed_trees[fname]
        signals = parsed_signals[fname]
        _ACTIVE_ELAB = parsed_elab.get(fname)
        builtin_findings = []
        for check in ALL_CHECKS:
            try:
                builtin_findings.extend(check(tree, fname, signals))
            except Exception:
                pass
        learned_findings = []
        try:
            learned_findings = _run_learned_rules(tree, fname, signals)
        except Exception:
            pass
        all_findings = builtin_findings + learned_findings
        result.findings.extend(_deduplicate_findings(all_findings))
        # Score learned rules against built-in coverage
        try:
            _score_learned_rules(all_findings, builtin_findings)
        except Exception:
            pass
    _ACTIVE_ELAB = None

    result.findings = _apply_waivers(result.findings)

    # ── Phase 4: Quick synthesis + inferred netlist ─────────────────
    per_file_synth = {}
    per_file_netlist = {}
    merged_netlist = InferredNetlist()

    for fname in parsed_trees:
        try:
            est = SynthesisEstimator(
                parsed_trees[fname], parsed_sources[fname],
                parsed_signals.get(fname, {}), elab=parsed_elab.get(fname))
            file_synth = est.estimate()
            per_file_synth[fname] = file_synth
            file_nl = est.build_netlist()
            per_file_netlist[fname] = file_nl
            for cell in file_nl.cells.values():
                merged_netlist.add_cell(cell)
        except Exception:
            pass

    # ── Phase 5: Tier-2 structural rules (with context) ─────────────
    for fname in parsed_trees:
        _ACTIVE_ELAB = parsed_elab.get(fname)
        ctx = DesignContext(
            synth_data=per_file_synth.get(fname, {}),
            netlist=per_file_netlist.get(fname),
            signal_rw_map=signal_rw_maps.get(fname),
            elab_result=elab_result,
            per_file_synth=per_file_synth,
            elab=parsed_elab.get(fname),
        )
        tree = parsed_trees[fname]
        signals = parsed_signals[fname]
        t2_findings = []
        for check in TIER2_CHECKS:
            try:
                t2_findings.extend(check(tree, fname, signals, ctx=ctx))
            except Exception:
                pass
        result.findings.extend(_deduplicate_findings(t2_findings))
    _ACTIVE_ELAB = None

    counts: dict[str, int] = {}
    for f in result.findings:
        counts[f.rule] = counts.get(f.rule, 0) + 1

    # Latch count: unique signals
    latch_signals = set()
    for f in result.findings:
        if f.rule in ('W402_latch_inferred', 'SYNTH_12608_latch'):
            m_sig = re.search(r"'(\w+)'", f.message)
            if m_sig:
                latch_signals.add(m_sig.group(1))
    latch_count = len(latch_signals)
    if latch_count == 0 and counts.get("W263_case_no_default", 0) > 0:
        latch_count = counts["W263_case_no_default"]

    result.measurements = {
        "cogni.lint.latch_inference.count": latch_count,
        "cogni.lint.blocking_in_seq.count": counts.get("W336_blocking_in_seq", 0),
        "cogni.lint.mixed_assignments.count": counts.get("W505_mixed_assign", 0),
        "cogni.lint.width_mismatch.count": counts.get("W164_width_mismatch", 0),
        "cogni.lint.comb_loop.count": counts.get("FUNC_comb_loop", 0),
        "cogni.lint.comb_depth.max": max(
            (int(re.search(r'depth (\d+)', f.message).group(1))
             for f in result.findings if f.rule == "FUNC_comb_depth"
             and re.search(r'depth (\d+)', f.message)),
            default=0),
        "cogni.lint.memory_array.count": counts.get("MEM_large_array", 0)
                                        + counts.get("MEM_medium_array", 0)
                                        + counts.get("MEM_small_array", 0),
        "cogni.lint.missing_clock_gating.count": counts.get("PWR_no_clock_gate", 0),
        "cogni.lint.fsm.count": counts.get("FSM_over_encoded", 0)
                               + counts.get("FSM_large", 0),
        "cogni.lint.unused_signal.count": counts.get("W528_unused_signal", 0),
        "cogni.lint.async_reset_misuse.count": counts.get("CLK_async_rst_data", 0),
        "cogni.lint.multiple_drivers.count": counts.get("W_multi_driver", 0)
                                            + counts.get("W_multi_seq_assign", 0),
        "cogni.lint.duplicate_case.count": counts.get("W_duplicate_case", 0),
        "cogni.lint.comparison_out_of_range.count": counts.get("FUNC_cmp_out_of_range", 0),
        "cogni.elab.port_not_found.count": counts.get("ELAB_port_not_found", 0),
        "cogni.elab.unconnected_port.count": counts.get("ELAB_unconnected_port", 0),
        "cogni.elab.missing_port.count": counts.get("ELAB_missing_port", 0),
        "cogni.elab.port_width_mismatch.count": counts.get("ELAB_port_width_mismatch", 0),
        "cogni.elab.missing_module.count": counts.get("ELAB_missing_module", 0),
        "cogni.elab.cdc_crossing.count": counts.get("ELAB_cdc_crossing", 0),
        "cogni.elab.multi_driver.count": counts.get("ELAB_multi_driver", 0),
        "cogni.cdc.no_sync.count": counts.get("CDC_no_sync", 0),
        "cogni.cdc.multi_bit.count": counts.get("CDC_multi_bit", 0),
        "cogni.cdc.glitch.count": counts.get("CDC_glitch_combo", 0),
        "cogni.cdc.reset.count": counts.get("CDC_async_reset", 0),
        "cogni.cdc.fifo_ptr.count": counts.get("CDC_fifo_ptr", 0),
        "cogni.cdc.handshake.count": (counts.get("CDC_handshake_req", 0)
                                      + counts.get("CDC_handshake_ack", 0)),
        "cogni.cdc.reconvergence.count": (counts.get("CDC_reconvergence", 0)
                                          + counts.get("CDC_convergence", 0)),
        "cogni.cdc.pulse.count": counts.get("CDC_pulse_sync", 0),
        "cogni.cdc.total_violations": sum(
            counts.get(r, 0) for r in (
                "CDC_no_sync", "CDC_multi_bit", "CDC_glitch_combo",
                "CDC_async_reset", "CDC_fifo_ptr", "CDC_handshake_req",
                "CDC_handshake_ack", "CDC_reconvergence", "CDC_convergence",
                "CDC_pulse_sync", "CDC_mux_data_unstable",
                "CDC_gated_sync_clk")),
        "cogni.lint.total_issues": len(result.findings),
    }
    result.measurements.update(elab_measurements)

    # CDC crossing summary report
    cdc_crossings = []
    for fname, tree in parsed_trees.items():
        sigs = parsed_signals.get(fname, {})
        try:
            cdc_crossings.extend(_cdc_crossing_summary(tree, fname, sigs))
        except Exception:
            pass
    if cdc_crossings:
        result.measurements["cogni.cdc"] = {
            "total_crossings": len(cdc_crossings),
            "safe": len([c for c in cdc_crossings if c['status'] == 'PASS']),
            "violations": len([c for c in cdc_crossings
                               if c['status'] == 'FAIL']),
            "crossings": cdc_crossings,
        }

    # RDC summary counts
    rdc_rules = ("RDC_async_no_sync", "RDC_combo_reset",
                 "RDC_glitch_reset", "RDC_no_filter",
                 "RDC_mixed_polarity")
    rdc_total = sum(counts.get(r, 0) for r in rdc_rules)
    if rdc_total > 0:
        rdc_items = []
        for f in result.findings:
            if f.rule in rdc_rules:
                sig = (f.message.split("'")[1]
                       if "'" in f.message else
                       f.rule.replace('RDC_', ''))
                rdc_items.append({
                    "rule": f.rule, "signal": sig,
                    "severity": f.severity,
                    "line": f.line,
                    "message": f.message,
                })
        result.measurements["cogni.rdc"] = {
            "total_issues": rdc_total,
            "errors": len([i for i in rdc_items
                           if i['severity'] == 'error']),
            "warnings": len([i for i in rdc_items
                             if i['severity'] == 'warning']),
            "items": rdc_items,
        }

    # DFT summary
    dft_rules = [r for r in ("DFT_nonscan_ff", "DFT_clock_no_bypass",
                              "DFT_async_ctrl_scan", "DFT_mem_no_bist",
                              "DFT_tristate_scan", "DFT_combo_loop",
                              "DFT_low_observe", "DFT_clk_mux_glitch")]
    dft_total = sum(counts.get(r, 0) for r in dft_rules)
    if dft_total > 0:
        dft_items = []
        for f in result.findings:
            if f.rule in dft_rules:
                sig = (f.message.split("'")[1]
                       if "'" in f.message else
                       f.rule.replace('DFT_', ''))
                dft_items.append({
                    "rule": f.rule, "signal": sig,
                    "severity": f.severity,
                    "line": f.line, "message": f.message,
                })
        result.measurements["cogni.dft"] = {
            "total_issues": dft_total,
            "errors": len([i for i in dft_items
                           if i['severity'] == 'error']),
            "warnings": len([i for i in dft_items
                             if i['severity'] == 'warning']),
            "items": dft_items,
        }

    # SVA summary
    sva_rules = [r for r in ("SVA_no_clock", "SVA_no_disable_iff",
                              "SVA_vacuous", "SVA_unbounded",
                              "SVA_assume_in_rtl", "SVA_no_else_action",
                              "SVA_no_cover", "SVA_fsm_uncovered",
                              "SVA_conflict_assume")]
    sva_total = sum(counts.get(r, 0) for r in sva_rules)
    if sva_total > 0:
        sva_items = []
        for f in result.findings:
            if f.rule in sva_rules:
                sig = (f.message.split("'")[1]
                       if "'" in f.message else
                       f.rule.replace('SVA_', ''))
                sva_items.append({
                    "rule": f.rule, "signal": sig,
                    "severity": f.severity,
                    "line": f.line, "message": f.message,
                })
        result.measurements["cogni.sva"] = {
            "total_issues": sva_total,
            "errors": len([i for i in sva_items
                           if i['severity'] == 'error']),
            "warnings": len([i for i in sva_items
                             if i['severity'] == 'warning']),
            "items": sva_items,
        }

    # Hierarchical summary
    hier_rules = [r for r in ("HIER_cdc_boundary",
                               "HIER_unconnected_clk",
                               "HIER_unconnected_rst",
                               "HIER_floating_output",
                               "HIER_feedthrough",
                               "HIER_implicit_clock",
                               "HIER_reset_multi_domain",
                               "HIER_wide_param")]
    hier_total = sum(counts.get(r, 0) for r in hier_rules)
    if hier_total > 0:
        hier_items = []
        for f in result.findings:
            if f.rule in hier_rules:
                sig = (f.message.split("'")[1]
                       if "'" in f.message else
                       f.rule.replace('HIER_', ''))
                hier_items.append({
                    "rule": f.rule, "signal": sig,
                    "severity": f.severity,
                    "line": f.line, "message": f.message,
                })
        result.measurements["cogni.hier"] = {
            "total_issues": hier_total,
            "errors": len([i for i in hier_items
                           if i['severity'] == 'error']),
            "warnings": len([i for i in hier_items
                             if i['severity'] == 'warning']),
            "items": hier_items,
        }

    # ── Synthesis predictions (reuse Phase 4 data) ────────────────────
    result.predictions = _predict_synthesis(
        result,
        trees=parsed_trees,
        sources=parsed_sources,
        all_signals=parsed_signals,
        precomputed_synth=per_file_synth,
    )

    # Add netlist summary to measurements
    if merged_netlist.cells:
        result.measurements["cogni.netlist"] = merged_netlist.summary()

        # Timing back-annotation: trace critical paths through the netlist
        crit_paths = merged_netlist.critical_paths(top_n=5)
        if crit_paths:
            worst = crit_paths[0]
            result.measurements["cogni.timing"] = {
                "worst_path_ns": worst["total_ns"],
                "worst_endpoint": worst["endpoint_signal"],
                "worst_depth": worst["depth"],
                "max_freq_mhz": round(1000.0 / worst["total_ns"], 1)
                                if worst["total_ns"] > 0 else 0.0,
                "critical_paths": crit_paths,
            }

    # SDC constraint summary
    if _ACTIVE_SDC:
        sdc_info = {
            "clocks": {n: {"period_ns": c.period_ns,
                           "freq_mhz": round(1000.0 / c.period_ns, 1),
                           "port": c.port}
                       for n, c in _ACTIVE_SDC.clocks.items()},
            "generated_clocks": {n: {"source": gc.source,
                                     "divide_by": gc.divide_by,
                                     "multiply_by": gc.multiply_by}
                                 for n, gc in _ACTIVE_SDC.generated_clocks.items()},
            "false_paths": len(_ACTIVE_SDC.false_paths),
            "multicycle_paths": len(_ACTIVE_SDC.multicycle_paths),
            "clock_groups": len(_ACTIVE_SDC.clock_groups),
        }
        result.measurements["cogni.sdc"] = sdc_info

        # Per-clock timing slack from synth estimates
        timing_slack = {}
        for fname, synth_data in per_file_synth.items():
            sdc_slack = synth_data.get("timing", {}).get("sdc_slack", {})
            for cname, slack_info in sdc_slack.items():
                if cname not in timing_slack or slack_info["slack_ns"] < timing_slack[cname]["slack_ns"]:
                    timing_slack[cname] = slack_info
                    timing_slack[cname]["file"] = fname
        if timing_slack:
            result.measurements["cogni.sdc.timing"] = timing_slack
            # Attribute the worst path to an RTL endpoint/line for the report
            ep_signal = ep_line = None
            tmeas = result.measurements.get("cogni.timing")
            if tmeas and tmeas.get("critical_paths"):
                worst_p = tmeas["critical_paths"][0]
                ep_signal = worst_p.get("endpoint_signal")
                if worst_p.get("path"):
                    ep_line = worst_p["path"][-1].get("line") or None
            for cname, si in timing_slack.items():
                if not si["met"]:
                    endpoint_note = (f" — worst endpoint '{ep_signal}'"
                                     f" (line {ep_line})"
                                     if ep_signal else "")
                    result.findings.append(Finding(
                        rule="SDC_timing_violation", severity="error",
                        file=si.get("file", ""),
                        line=ep_line or 0,
                        message=(
                            f"Timing violation on clock '{cname}': "
                            f"critical path {si['critical_path_ns']}ns "
                            f"exceeds period {si['period_ns']}ns "
                            f"(slack {si['slack_ns']}ns){endpoint_note}"),
                        synth_impact=(
                            f"Design cannot meet {si['freq_mhz']}MHz target; "
                            f"reduce combinational depth or pipeline"),
                    ))

    # UPF power-intent summary
    if _ACTIVE_UPF:
        upf = _ACTIVE_UPF
        upf_counts = {}
        for f in result.findings:
            if f.rule.startswith("UPF_"):
                upf_counts[f.rule] = upf_counts.get(f.rule, 0) + 1
        result.measurements["cogni.upf"] = {
            "top": upf.top,
            "domains": {n: {"elements": len(d.elements),
                            "power_net": d.primary_power_net,
                            "voltage": upf.domain_voltage(n),
                            "switchable": upf.is_switchable(n)}
                        for n, d in upf.domains.items()},
            "switches": len(upf.switches),
            "isolations": len(upf.isolations),
            "retentions": len(upf.retentions),
            "level_shifters": len(upf.level_shifters),
            "violations": upf_counts,
            "total_violations": sum(upf_counts.values()),
        }

    _ACTIVE_SDC = None
    _ACTIVE_UPF = None
    return result


# ===================================================================
# Phase 4: PREDICT — Real synthesis estimation from AST
# ===================================================================

# Gate-equivalent costs (generic standard-cell library)
_GATE_FF = 6          # DFF = ~6 gate equivalents
_GATE_LATCH = 4       # D-latch = ~4 gate equivalents
_GATE_MUX2 = 4        # 2:1 MUX = ~4 gates per bit
_GATE_ADDER = 5       # per-bit adder gate cost (ripple carry)
_GATE_COMPARATOR = 3  # per-bit comparator
_GATE_MULTIPLIER = 5  # per-bit-pair (N*M * this)
_GATE_DECODER = 2     # per-output decoder gate cost

# Timing estimates (generic 28nm, nanoseconds)
_DELAY_GATE = 0.05    # inverter/buffer
_DELAY_MUX2 = 0.10    # 2:1 mux
_DELAY_ADDER_BIT = 0.08  # per-bit ripple carry
_DELAY_CLA = 0.30     # carry-lookahead (any width)
_DELAY_COMP = 0.15    # comparator
_DELAY_MULT_SMALL = 0.50   # <=8-bit multiplier
_DELAY_MULT_LARGE = 1.50   # >8-bit multiplier
_DELAY_FF_SETUP = 0.05
_DELAY_FF_CKQ = 0.10


class SynthesisEstimator:
    """Walks AST to estimate synthesis gate count, timing, area, and power."""

    def __init__(self, tree, source: bytes, signals: dict, elab=None):
        self.tree = tree
        self.source = source
        self.signals = signals
        self.src_text = source.decode('utf-8', errors='replace')
        # Shared elaboration facts (constants, loop indices, enum members).
        self.elab = elab or build_elaboration_model(tree, signals, self.src_text)

        # Register (FF) tracking: signal_name -> bit_width
        self.ff_map: dict[str, int] = {}
        self.latch_map: dict[str, int] = {}

        # Operator counts: (width, line, expression_text)
        self.adders: list[tuple[int, int, str]] = []
        self.subtractors: list[tuple[int, int]] = []
        self.multipliers: list[tuple[int, int, int]] = []  # (w_a, w_b, line)
        self.comparators: list[tuple[int, int, str]] = []
        self.state_decodes: list[tuple[int, int, str]] = []
        self.mux_bits = 0        # total 2:1-equivalent mux bits
        self.logic_gates = 0     # AND/OR/XOR/NOT gate count
        self.shift_ops: list[tuple[int, int]] = []   # (width, line)

        # Memory arrays: (word_bits, depth, line)
        self.memories: list[tuple[int, int, int]] = []

        # Fanout tracking: signal -> read_count
        self.fanout: dict[str, int] = {}

        # Combinational depth per output signal
        self.comb_depths: dict[str, int] = {}

        # Clock gating candidates
        self.ungated_ff_bits = 0
        self.gated_ff_bits = 0

        # FSM info
        self.fsm_states = 0
        self.fsm_encoding_bits = 0

    def estimate(self) -> dict:
        root = self.tree.root_node
        self._optimize(root)
        self._scan_registers(root)
        self._scan_latches(root)
        self._scan_combinational(root)
        self._scan_operators(root)
        self._scan_memory(root)
        self._scan_fanout(root)
        self._scan_fsm(root)
        return self._build_summary()

    def _sig_width(self, name: str) -> int:
        s = self.signals.get(name)
        if s and s.width > 0:
            return s.width
        return 1

    # --- Optimization passes ---

    def _optimize(self, root):
        """Constant propagation + dead code elimination before gate counting."""
        self._dead_ranges = []
        self._constant_signals = {}
        self._const_outputs = set()
        self._optimizations = []

        self._params = {}
        for name, sig in self.signals.items():
            if getattr(sig, 'is_param', False) and sig.param_value is not None:
                self._params[name] = sig.param_value

        if not self._params:
            return

        for always in _find_nodes(root, 'always_construct'):
            if _always_type(always) != 'always_comb':
                continue
            self._const_prop_conds(always)

        for ca in _find_nodes(root, 'continuous_assign'):
            self._const_prop_conds(ca)

        for name, val in self._constant_signals.items():
            sig = self.signals.get(name)
            if sig and getattr(sig, 'direction', '') == 'output':
                self._const_outputs.add(name)

    def _const_prop_conds(self, block):
        """Find constant-controlled branches and mark dead code."""
        for cond in _find_nodes(block, 'conditional_statement'):
            if self._is_dead(cond):
                continue
            nc = cond.named_children
            if not nc or nc[0].type != 'cond_predicate':
                continue
            cond_node = nc[0]
            val = self._eval_const_expr(cond_node)
            if val is None:
                continue

            true_branch = nc[1] if len(nc) >= 2 else None
            false_branch = nc[2] if len(nc) >= 3 else None
            expr_text = _node_text(cond_node)
            line = _node_line(cond)

            if val:
                if false_branch:
                    self._dead_ranges.append(
                        (false_branch.start_byte, false_branch.end_byte))
                    self._optimizations.append({
                        'type': 'const_prop', 'line': line,
                        'detail': '%s always TRUE — else branch eliminated'
                                  % expr_text,
                    })
                surviving = true_branch
            else:
                if true_branch:
                    self._dead_ranges.append(
                        (true_branch.start_byte, true_branch.end_byte))
                    self._optimizations.append({
                        'type': 'const_prop', 'line': line,
                        'detail': '%s always FALSE — if branch eliminated'
                                  % expr_text,
                    })
                surviving = false_branch

            if surviving:
                self._extract_const_assignments(surviving)

    def _extract_const_assignments(self, node):
        """Track signals assigned constant values in surviving branches."""
        for asgn in (_find_nodes(node, 'blocking_assignment')
                     + _find_nodes(node, 'nonblocking_assignment')):
            lhs = _get_lhs_signal(asgn)
            if not lhs:
                continue
            oa = _find_nodes(asgn, 'operator_assignment')
            target = oa[0] if oa else asgn
            children = target.named_children
            if len(children) >= 2:
                rhs = children[-1]
                rhs_val = self._eval_const_expr(rhs)
                if rhs_val is not None:
                    self._constant_signals[lhs] = rhs_val

    def _eval_const_expr(self, node):
        """Evaluate AST expression to a constant integer, or None."""
        if node is None:
            return None
        text = _node_text(node).strip()

        if node.type == 'simple_identifier':
            if text in self._params:
                return self._params[text]
            return self._constant_signals.get(text)

        if node.type in ('integral_number', 'primary_literal', 'number'):
            m = re.match(r"(\d+)'([bdho])([0-9a-fA-F_]+)", text)
            if m:
                base_map = {'b': 2, 'd': 10, 'h': 16, 'o': 8}
                val_str = m.group(3).replace('_', '')
                try:
                    return int(val_str, base_map.get(m.group(2), 10))
                except ValueError:
                    return None
            try:
                return int(text)
            except ValueError:
                return None

        nc = node.named_children
        # Unary: [operator, operand]
        if len(nc) == 2 and nc[0].type == 'unary_operator':
            op = _node_text(nc[0])
            v = self._eval_const_expr(nc[1])
            if v is not None:
                if op == '!':
                    return 0 if v else 1
                if op == '~':
                    return ~v & 0xFFFFFFFF
            return None

        # Binary: [lhs, operator, rhs]
        if len(nc) == 3 and nc[1].type == 'binary_operator':
            left = self._eval_const_expr(nc[0])
            right = self._eval_const_expr(nc[2])
            if left is not None and right is not None:
                op = _node_text(nc[1])
                ops = {'+': lambda a, b: a + b,
                       '-': lambda a, b: a - b,
                       '*': lambda a, b: a * b,
                       '==': lambda a, b: 1 if a == b else 0,
                       '!=': lambda a, b: 1 if a != b else 0,
                       '<': lambda a, b: 1 if a < b else 0,
                       '>': lambda a, b: 1 if a > b else 0,
                       '<=': lambda a, b: 1 if a <= b else 0,
                       '>=': lambda a, b: 1 if a >= b else 0,
                       '&': lambda a, b: a & b,
                       '|': lambda a, b: a | b,
                       '^': lambda a, b: a ^ b,
                       '&&': lambda a, b: 1 if (a and b) else 0,
                       '||': lambda a, b: 1 if (a or b) else 0}
                fn = ops.get(op)
                if fn:
                    return fn(left, right)
            return None

        # Recurse into single-child wrappers
        if len(nc) == 1:
            return self._eval_const_expr(nc[0])

        return None

    def _is_dead(self, node):
        """Check if node is inside a dead (optimized-away) code region."""
        for start, end in self._dead_ranges:
            if node.start_byte >= start and node.end_byte <= end:
                return True
        return False

    def _scan_registers(self, root):
        """Count flip-flop bits from always_ff assignments."""
        self._enabled_regs = set()
        for always in _find_nodes(root, 'always_construct'):
            if _always_type(always) != 'always_ff':
                continue
            assigned = set()
            for asgn in (_find_nodes(always, 'nonblocking_assignment')
                         + _find_nodes(always, 'blocking_assignment')):
                lhs = _get_lhs_signal(asgn)
                if lhs:
                    assigned.add(lhs)
            # Fallback: inside generate loops, tree-sitter may not emit
            # nonblocking_assignment nodes for genvar-indexed LHS
            # (`pwm_out[i] <= ...`). Recover registered signals from text.
            if not assigned:
                atxt = _strip_comments(_node_text(always))
                for m in re.finditer(r'(\w+)\s*(?:\[[^\]]*\])?\s*<=', atxt):
                    name = m.group(1)
                    if name in self.signals:
                        assigned.add(name)
            ce_sigs = self._detect_clock_enable(always)
            for sig in assigned:
                w = self._sig_width(sig)
                self.ff_map[sig] = w
                if sig in ce_sigs:
                    self.gated_ff_bits += w
                    self._enabled_regs.add(sig)
                else:
                    self.ungated_ff_bits += w

    def _detect_clock_enable(self, always_node):
        """Detect true clock enable: signal assigned in if-branch but holds value in else."""
        ce_signals = set()
        conds = _find_nodes(always_node, 'conditional_statement')
        for cond in conds:
            cond_text = _node_text(cond)
            if re.search(r'if\s*\(\s*!?\s*\w*(rst|reset)\w*',
                         cond_text[:60], re.IGNORECASE):
                continue
            named = cond.named_children
            has_top_else = len(named) >= 3
            if not has_top_else:
                for asgn in (_find_nodes(cond, 'nonblocking_assignment')
                             + _find_nodes(cond, 'blocking_assignment')):
                    lhs = _get_lhs_signal(asgn)
                    if lhs:
                        ce_signals.add(lhs)
            else:
                if_body = named[1] if len(named) > 1 else None
                else_body = named[2] if len(named) > 2 else None
                if_assigned = set()
                else_assigned = set()
                if if_body:
                    for asgn in (_find_nodes(if_body, 'nonblocking_assignment')
                                 + _find_nodes(if_body, 'blocking_assignment')):
                        lhs = _get_lhs_signal(asgn)
                        if lhs:
                            if_assigned.add(lhs)
                if else_body:
                    for asgn in (_find_nodes(else_body, 'nonblocking_assignment')
                                 + _find_nodes(else_body, 'blocking_assignment')):
                        lhs = _get_lhs_signal(asgn)
                        if lhs:
                            else_assigned.add(lhs)
                ce_signals |= (if_assigned - else_assigned)
                ce_signals |= (else_assigned - if_assigned)
        return ce_signals

    def _scan_latches(self, root):
        """Detect latches: signals in always_comb with incomplete assignment."""
        for always in _find_nodes(root, 'always_construct'):
            if _always_type(always) != 'always_comb':
                continue
            text = _node_text(always)
            assigned = set()
            for asgn in _find_nodes(always, 'blocking_assignment'):
                lhs = _get_lhs_signal(asgn)
                if lhs:
                    assigned.add(lhs)
            if not assigned:
                continue
            has_default = ('default' in text or
                           'else' in text.split('\n')[-3:])
            conds = _find_nodes(always, 'conditional_statement')
            cases = _find_nodes(always, 'case_statement')
            if not conds and not cases:
                continue
            for sig in assigned:
                lines = text.split('\n')
                has_init = any(
                    re.match(r'\s*' + re.escape(sig) + r'\s*=', l)
                    for l in lines[:3])
                if has_init:
                    continue
                for cond in conds:
                    nc = cond.named_children
                    has_else = any(
                        c.type == 'statement_or_null' for c in nc
                        if c != nc[0]) if len(nc) > 2 else False
                    if not has_else:
                        w = self._sig_width(sig)
                        self.latch_map[sig] = w
                        break
                for case in cases:
                    if 'default' not in _node_text(case):
                        w = self._sig_width(sig)
                        self.latch_map[sig] = w
                        break

    def _scan_combinational(self, root):
        """Count muxes from if/case in combinational blocks."""
        for always in _find_nodes(root, 'always_construct'):
            if _always_type(always) != 'always_comb':
                continue
            if self._is_dead(always):
                continue
            self._count_muxes(always)
            self._measure_comb_depth(always, 0)

        for ca in _find_nodes(root, 'continuous_assign'):
            if self._is_dead(ca):
                continue
            self._count_muxes(ca)

    def _count_muxes(self, node):
        """Each if/else = 1 mux per assigned bit; each case arm contributes."""
        for cond in _find_nodes(node, 'conditional_statement'):
            if self._is_dead(cond):
                continue
            # If condition is constant, no mux needed
            nc = cond.named_children
            if nc and nc[0].type == 'cond_predicate':
                val = self._eval_const_expr(nc[0])
                if val is not None:
                    continue
            assigned = set()
            for asgn in (_find_nodes(cond, 'blocking_assignment')
                         + _find_nodes(cond, 'nonblocking_assignment')):
                if self._is_dead(asgn):
                    continue
                lhs = _get_lhs_signal(asgn)
                if lhs:
                    assigned.add(lhs)
            for sig in assigned:
                self.mux_bits += self._sig_width(sig)

        for case in _find_nodes(node, 'case_statement'):
            if self._is_dead(case):
                continue
            arms = _find_nodes(case, 'case_item')
            n_arms = max(len(arms), 2)
            assigned = set()
            for asgn in (_find_nodes(case, 'blocking_assignment')
                         + _find_nodes(case, 'nonblocking_assignment')):
                if self._is_dead(asgn):
                    continue
                lhs = _get_lhs_signal(asgn)
                if lhs:
                    assigned.add(lhs)
            for sig in assigned:
                self.mux_bits += self._sig_width(sig) * (n_arms - 1)

    def _measure_comb_depth(self, node, depth, _visited=None):
        """Measure nesting depth of if/case chains."""
        import math
        if _visited is None:
            _visited = set()
        node_id = id(node)
        if node_id in _visited:
            return
        _visited.add(node_id)

        # Find direct-child conditionals (not self)
        for child in node.children:
            if self._is_dead(child):
                continue
            if child.type == 'conditional_statement':
                # Skip constant-controlled conditionals (no mux needed)
                nc = child.named_children
                if nc and nc[0].type == 'cond_predicate':
                    val = self._eval_const_expr(nc[0])
                    if val is not None:
                        # Only measure the surviving branch
                        surviving = nc[1] if val else (nc[2] if len(nc) >= 3 else None)
                        if surviving:
                            self._measure_comb_depth(surviving, depth, _visited)
                        continue
                new_depth = depth + 1
                assigned = set()
                for asgn in (_find_nodes(child, 'blocking_assignment')
                             + _find_nodes(child, 'nonblocking_assignment')):
                    if self._is_dead(asgn):
                        continue
                    lhs = _get_lhs_signal(asgn)
                    if lhs:
                        assigned.add(lhs)
                for sig in assigned:
                    cur = self.comb_depths.get(sig, 0)
                    self.comb_depths[sig] = max(cur, new_depth)
                self._measure_comb_depth(child, new_depth, _visited)
            elif child.type == 'case_statement':
                arms = _find_nodes(child, 'case_item')
                n_arms = max(len(arms), 2)
                mux_depth = max(1, int(math.ceil(math.log2(n_arms))))
                new_depth = depth + mux_depth
                assigned = set()
                for asgn in (_find_nodes(child, 'blocking_assignment')
                             + _find_nodes(child, 'nonblocking_assignment')):
                    lhs = _get_lhs_signal(asgn)
                    if lhs:
                        assigned.add(lhs)
                for sig in assigned:
                    cur = self.comb_depths.get(sig, 0)
                    self.comb_depths[sig] = max(cur, new_depth)
                self._measure_comb_depth(child, new_depth, _visited)
            else:
                self._measure_comb_depth(child, depth, _visited)

    def _scan_operators(self, root):
        """Count arithmetic, comparison, and logic operators."""
        elab = self.elab
        enum_members = elab.enum_members

        for expr in _find_nodes(root, 'expression'):
            if self._is_dead(expr):
                continue
            children = expr.children
            if len(children) != 3:
                continue
            left, op_node, right = children
            if left.type != 'expression' or right.type != 'expression':
                continue
            op_text = _node_text(op_node)

            if op_text in ('<=', '>='):
                p = expr.parent
                if p and p.type == 'nonblocking_assignment':
                    continue

            ids = _get_identifiers(expr)
            ids_left = _get_identifiers(left)
            ids_right = _get_identifiers(right)

            # Elaboration-time expressions infer no hardware:
            #   both sides constant       → folded away (DATA_WIDTH/8)
            #   a direct operand is index → loop unroll (8*i, i+1, i<N)
            # A real datapath op that merely indexes with a loop var
            # (sum + data[i]) is genuine hardware and is kept.
            if elab.is_const_expr(ids_left) and elab.is_const_expr(ids_right):
                continue
            if elab.is_index_expr(ids_left) or elab.is_index_expr(ids_right):
                continue

            def _expr_width(node):
                """Resolve width of an expression operand, accounting for bit/part selects."""
                t = _node_text(node).strip()
                if re.search(r'\[\s*\w+\s*:\s*\w+\s*\]', t):
                    ps = re.search(r'\[\s*(\w+)\s*:\s*(\w+)\s*\]', t)
                    if ps:
                        try:
                            hi = int(ps.group(1)) if ps.group(1).isdigit() else \
                                elab.constants.get(ps.group(1), None)
                            lo = int(ps.group(2)) if ps.group(2).isdigit() else \
                                elab.constants.get(ps.group(2), None)
                            if hi is not None and lo is not None:
                                return abs(int(hi) - int(lo)) + 1
                        except (ValueError, TypeError):
                            pass
                elif re.search(r'\[\s*[^:]+\s*\]', t):
                    return 1
                local_ids = _get_identifiers(node)
                if local_ids:
                    return max(self._sig_width(i) for i in local_ids)
                return 1

            max_w = max(_expr_width(left), _expr_width(right))
            line = _node_line(expr)
            expr_text = _node_text(expr)

            if op_text in ('+', '-'):
                self.adders.append((max_w, line, expr_text))
            elif op_text == '*':
                w_a = _expr_width(left)
                w_b = _expr_width(right)
                self.multipliers.append((w_a, w_b, line))
            elif op_text in ('==', '!=', '<', '>', '<=', '>='):
                has_enum = any(i in enum_members for i in ids)
                if has_enum:
                    self.state_decodes.append((max_w, line, expr_text))
                elif op_text in ('==', '!=') and max_w <= 1:
                    # 1-bit equality (e.g. cpha == 1'b0) is a single XNOR/XOR
                    # gate, not a comparator cell.
                    self.logic_gates += 1
                else:
                    self.comparators.append((max_w, line, expr_text))
            elif op_text in ('<<', '>>', '<<<', '>>>'):
                self.shift_ops.append((max_w, line))
            elif op_text in ('&', '|', '^', '~^', '^~'):
                self.logic_gates += max_w
            elif op_text in ('&&', '||'):
                self.logic_gates += 1

        unary_ops = _find_nodes(root, 'unary_operator')
        for op_node in unary_ops:
            if self._is_dead(op_node):
                continue
            op_text = _node_text(op_node)
            if op_text in ('~', '!'):
                self.logic_gates += 1
            elif op_text in ('&', '|', '^', '~&', '~|', '~^'):
                parent = op_node.parent
                ids = _get_identifiers(parent) if parent else []
                w = max((self._sig_width(i) for i in ids), default=1)
                self.logic_gates += w - 1

    def _scan_memory(self, root):
        """Detect array declarations: logic [W-1:0] name [0:D-1]."""
        for m in re.finditer(
                r'(?:logic|reg)\s*\[([^\]]+):([^\]]+)\]\s*(\w+)\s*'
                r'\[([^\]]+):([^\]]+)\]', self.src_text):
            hi_w, lo_w, name, hi_d, lo_d = m.groups()
            params = {s.name: s.param_value for s in self.signals.values()
                      if getattr(s, 'is_param', False)
                      and s.param_value is not None}
            w_hi = _eval_param_expr(hi_w.strip(), params)
            w_lo = _eval_param_expr(lo_w.strip(), params)
            d_hi = _eval_param_expr(hi_d.strip(), params)
            d_lo = _eval_param_expr(lo_d.strip(), params)
            if all(v is not None for v in (w_hi, w_lo, d_hi, d_lo)):
                word_bits = abs(w_hi - w_lo) + 1
                depth = abs(d_hi - d_lo) + 1
                line_num = self.src_text[:m.start()].count('\n') + 1
                self.memories.append((word_bits, depth, line_num))

    def _scan_fanout(self, root):
        """Count how many times each signal is read on the RHS."""
        lhs_nodes = set()
        for asgn in (_find_nodes(root, 'blocking_assignment')
                     + _find_nodes(root, 'nonblocking_assignment')
                     + _find_nodes(root, 'net_assignment')):
            lv = _find_nodes(asgn, 'variable_lvalue')
            if not lv:
                lv = _find_nodes(asgn, 'net_lvalue')
            for v in lv:
                for ident in _find_nodes(v, 'simple_identifier'):
                    lhs_nodes.add(id(ident))

        decl_nodes = set()
        for decl_type in ('data_declaration', 'net_declaration',
                          'ansi_port_declaration', 'parameter_declaration',
                          'local_parameter_declaration',
                          'type_declaration', 'enum_name_declaration'):
            for decl in _find_nodes(root, decl_type):
                for ident in _find_nodes(decl, 'simple_identifier'):
                    decl_nodes.add(id(ident))

        for ident in _find_nodes(root, 'simple_identifier'):
            if id(ident) in lhs_nodes or id(ident) in decl_nodes:
                continue
            name = _node_text(ident)
            if name in self.signals and not getattr(
                    self.signals[name], 'is_param', False):
                self.fanout[name] = self.fanout.get(name, 0) + 1

    def _scan_fsm(self, root):
        """Detect FSM enum declarations for encoding analysis."""
        text = _node_text(root)
        for enum in _find_nodes(root, 'enum_base_type'):
            parent = enum.parent
            if not parent:
                continue
            type_decl = parent.parent
            if type_decl:
                td_text = _node_text(type_decl)
                tm = re.search(r'\}\s*(\w+)', td_text)
                if tm and not _is_fsm_enum(tm.group(1),
                                           type('T', (), {'root_node': root})):
                    continue
            items = _find_nodes(parent, 'enum_name_declaration')
            if items:
                self.fsm_states = max(self.fsm_states, len(items))
            txt = _node_text(enum)
            m = re.search(r'\[(\d+):0\]', txt)
            if m:
                self.fsm_encoding_bits = int(m.group(1)) + 1

    def _build_summary(self) -> dict:
        """Compute final gate count, timing, area, and power estimates."""
        import math

        # --- Gate Count ---
        ff_bits = sum(self.ff_map.values())
        ff_gates = ff_bits * _GATE_FF

        latch_bits = sum(self.latch_map.values())
        latch_gates = latch_bits * _GATE_LATCH

        mux_gates = self.mux_bits * _GATE_MUX2

        adder_gates = sum(w * _GATE_ADDER for w, _, _ in self.adders)
        sub_gates = sum(w * _GATE_ADDER for w, _ in self.subtractors)
        mult_gates = sum(wa * wb * _GATE_MULTIPLIER
                         for wa, wb, _ in self.multipliers)
        comp_gates = sum(w * _GATE_COMPARATOR
                         for w, _, _ in self.comparators)
        shift_gates = sum(w * 2 for w, _ in self.shift_ops)

        mem_ff_gates = sum(wbits * depth * _GATE_FF
                           for wbits, depth, _ in self.memories)

        total_comb = (mux_gates + adder_gates + sub_gates + mult_gates
                      + comp_gates + shift_gates + self.logic_gates)
        total_seq = ff_gates + latch_gates
        total_mem = mem_ff_gates
        total_gates = total_comb + total_seq + total_mem

        # --- Timing (Critical Path) ---
        max_comb_depth = max(self.comb_depths.values(), default=0)
        comb_delay = max_comb_depth * _DELAY_MUX2

        # Add arithmetic delays on the critical path
        arith_delay = 0.0
        if self.adders:
            max_adder_w = max(w for w, _, _ in self.adders)
            if max_adder_w <= 16:
                arith_delay += max_adder_w * _DELAY_ADDER_BIT
            else:
                arith_delay += _DELAY_CLA
        if self.multipliers:
            max_mult = max(wa * wb for wa, wb, _ in self.multipliers)
            arith_delay += (_DELAY_MULT_SMALL if max_mult <= 64
                            else _DELAY_MULT_LARGE)
        if self.comparators:
            arith_delay += _DELAY_COMP

        # Only add FF overhead if there are actual FFs or comb logic
        if ff_bits > 0 and (total_comb > 0 or arith_delay > 0):
            critical_path_ns = _DELAY_FF_CKQ + comb_delay + arith_delay + _DELAY_FF_SETUP
        elif ff_bits > 0:
            critical_path_ns = _DELAY_FF_CKQ + _DELAY_FF_SETUP
        elif total_comb > 0:
            critical_path_ns = comb_delay + arith_delay
        else:
            critical_path_ns = 0.0

        max_freq_mhz = (1000.0 / critical_path_ns) if critical_path_ns > 0 else 0.0

        # --- SDC timing slack ---
        sdc_timing = {}
        if _ACTIVE_SDC and _ACTIVE_SDC.clocks and critical_path_ns > 0:
            for cname, cdef in _ACTIVE_SDC.clocks.items():
                period = cdef.period_ns
                slack = period - critical_path_ns
                sdc_timing[cname] = {
                    "period_ns": period,
                    "freq_mhz": round(1000.0 / period, 1),
                    "critical_path_ns": round(critical_path_ns, 2),
                    "slack_ns": round(slack, 2),
                    "met": slack >= 0,
                }

        # --- Area ---
        # Area in um^2 (rough: 1 gate = ~1 um^2 at 28nm)
        area_um2 = total_gates * 1.0
        mem_total_bits = sum(w * d for w, d, _ in self.memories)

        # --- Power ---
        # Dynamic power proportional to toggle rate * capacitance * V^2 * f
        # Rough: counters/shift_regs toggle every cycle
        high_toggle_bits = 0
        for sig, w in self.ff_map.items():
            # Counters and shift registers toggle frequently
            txt_lower = sig.lower()
            if any(k in txt_lower for k in ('cnt', 'count', 'shift',
                                              'tick', 'timer', 'div')):
                high_toggle_bits += w

        # --- Fanout ---
        high_fanout = [(sig, cnt) for sig, cnt in self.fanout.items()
                       if cnt >= 16]
        high_fanout.sort(key=lambda x: -x[1])

        # --- Resource Mapping (FPGA) ---
        dsp_candidates = len(self.multipliers)
        bram_candidates = [(w, d, ln) for w, d, ln in self.memories
                           if w * d >= 1024]
        lutram_candidates = [(w, d, ln) for w, d, ln in self.memories
                             if w * d < 1024 and w * d > 0]

        return {
            "ff_bits": ff_bits,
            "ff_signals": dict(self.ff_map),
            "latch_bits": latch_bits,
            "total_gates": total_gates,
            "gate_breakdown": {
                "sequential": total_seq,
                "combinational": total_comb,
                "memory": total_mem,
                "mux": mux_gates,
                "adder": adder_gates,
                "multiplier": mult_gates,
                "comparator": comp_gates,
                "logic": self.logic_gates,
            },
            "timing": {
                "critical_path_ns": round(critical_path_ns, 2),
                "max_comb_depth": max_comb_depth,
                "comb_delay_ns": round(comb_delay, 2),
                "arith_delay_ns": round(arith_delay, 2),
                "max_freq_mhz": round(max_freq_mhz, 1),
                "sdc_slack": sdc_timing,
            },
            "area_um2": round(area_um2, 1),
            "power": {
                "high_toggle_bits": high_toggle_bits,
                "ungated_ff_bits": self.ungated_ff_bits,
                "gated_ff_bits": self.gated_ff_bits,
                "enabled_blocks": len([
                    s for s in self.ff_map
                    if s in getattr(self, '_enabled_regs', set())]),
                "total_seq_blocks": len(self.ff_map),
            },
            "operators": {
                "adders": len(self.adders),
                "multipliers": len(self.multipliers),
                "comparators": len(self.comparators),
                "state_decodes": len(self.state_decodes),
                "shifters": len(self.shift_ops),
                "adder_details": [{"width": w, "line": ln, "expr": e}
                                  for w, ln, e in self.adders],
                "comparator_details": [{"width": w, "line": ln, "expr": e}
                                       for w, ln, e in self.comparators],
                "state_decode_details": [{"width": w, "line": ln, "expr": e}
                                         for w, ln, e in self.state_decodes],
            },
            "ce_signals": sorted(getattr(self, '_enabled_regs', set())),
            "non_ce_signals": sorted(
                s for s in self.ff_map
                if s not in getattr(self, '_enabled_regs', set())),
            "memory": {
                "arrays": len(self.memories),
                "total_bits": mem_total_bits,
                "details": [{"width": w, "depth": d, "line": ln}
                            for w, d, ln in self.memories],
                "bram_candidates": len(bram_candidates),
                "lutram_candidates": len(lutram_candidates),
            },
            "fanout": {
                "high_fanout_signals": high_fanout[:10],
                "max_fanout": high_fanout[0] if high_fanout else None,
            },
            "fsm": {
                "states": self.fsm_states,
                "encoding_bits": self.fsm_encoding_bits,
                "min_bits": (int(math.ceil(math.log2(max(self.fsm_states, 1))))
                             if self.fsm_states > 0 else 0),
            },
            "fpga_resources": {
                "dsp_blocks": dsp_candidates,
                "bram_blocks": len(bram_candidates),
                "lut_count": total_comb // 4 if total_comb > 0 else 0,
                "ff_count": ff_bits,
            },
            "optimization": {
                "const_prop_count": len(self._optimizations),
                "dead_branches": len(self._dead_ranges),
                "constant_outputs": list(self._const_outputs),
                "constant_signals": {k: v for k, v in
                                     self._constant_signals.items()},
                "details": self._optimizations,
            },
        }

    def build_netlist(self) -> InferredNetlist:
        """Build inferred gate-level netlist from scan results."""
        nl = InferredNetlist()

        def sig_line(sig):
            si = self.signals.get(sig)
            return getattr(si, 'line', 0) if si else 0

        for sig, width in self.ff_map.items():
            props = {}
            if sig in getattr(self, '_enabled_regs', set()):
                props['clock_enable'] = True
            if sig in {s for s, _ in getattr(self, 'shift_ops', []) if isinstance(s, str)}:
                props['shift_reg'] = True
            nl.add_cell(NetlistCell(
                cell_type='FF', name=f'ff_{sig}', width=width,
                line=sig_line(sig), inputs=[sig + '_d'], outputs=[sig],
                properties=props,
            ))
        for sig, width in self.latch_map.items():
            nl.add_cell(NetlistCell(
                cell_type='LATCH', name=f'latch_{sig}', width=width,
                line=sig_line(sig), inputs=[sig + '_d'], outputs=[sig],
            ))
        for i, (w, ln, expr) in enumerate(self.adders):
            name = f'adder_{i}'
            # If this is a register self-update (sig <= sig +/- ...), chain the
            # adder into that register's cone so the path adder->mux->reg_d is
            # captured as one timing arc.
            out_net = f'{name}_out'
            m = re.match(r'\s*(\w+)\s*[+\-]', expr)
            tgt = m.group(1) if m else None
            if tgt and tgt in self.ff_map:
                out_net = (tgt + '_in') if tgt in self.comb_depths else (tgt + '_d')
            nl.add_cell(NetlistCell(
                cell_type='ADDER', name=name, width=w,
                line=ln, inputs=[f'{name}_a', f'{name}_b'],
                outputs=[out_net],
                properties={'expr': expr},
            ))
        for i, (wa, wb, ln) in enumerate(self.multipliers):
            name = f'mult_{i}'
            nl.add_cell(NetlistCell(
                cell_type='MULT', name=name, width=wa + wb,
                line=ln, inputs=[f'{name}_a', f'{name}_b'],
                outputs=[f'{name}_out'],
            ))
        for i, (w, ln, expr) in enumerate(self.comparators):
            name = f'comp_{i}'
            nl.add_cell(NetlistCell(
                cell_type='COMP', name=name, width=w,
                line=ln, inputs=[f'{name}_a', f'{name}_b'],
                outputs=[f'{name}_out'],
                properties={'expr': expr},
            ))
        for i, (w, ln) in enumerate(self.shift_ops):
            name = f'shift_{i}'
            nl.add_cell(NetlistCell(
                cell_type='SHIFT', name=name, width=w,
                line=ln, inputs=[f'{name}_in', f'{name}_amt'],
                outputs=[f'{name}_out'],
            ))
        for i, (wbits, depth, ln) in enumerate(self.memories):
            name = f'mem_{i}'
            cell_type = 'BRAM' if wbits * depth >= 1024 else 'LUT'
            nl.add_cell(NetlistCell(
                cell_type=cell_type, name=name, width=wbits,
                line=ln, inputs=[f'{name}_addr', f'{name}_din'],
                outputs=[f'{name}_dout'],
                properties={'depth': depth, 'total_bits': wbits * depth},
            ))
        mux_idx = 0
        for sig, depth in self.comb_depths.items():
            prev_net = sig + '_in'
            # If sig is registered, the comb cone computes the FF's D input.
            final_net = (sig + '_d') if sig in self.ff_map else sig
            for d in range(depth):
                name = f'mux_{mux_idx}'
                out_net = final_net if d == depth - 1 else f'{name}_out'
                nl.add_cell(NetlistCell(
                    cell_type='MUX', name=name, width=self._sig_width(sig),
                    line=sig_line(sig), inputs=[prev_net, f'{name}_sel'],
                    outputs=[out_net],
                    properties={'signal': sig},
                ))
                prev_net = out_net
                mux_idx += 1
        return nl


def _predict_synthesis(result: AnalysisResult,
                       trees=None, sources=None,
                       all_signals=None,
                       precomputed_synth=None) -> list[SynthPrediction]:
    """Generate synthesis predictions from AST data + lint findings.
    Uses precomputed synth data from Phase 4 when available."""
    predictions = []
    m = result.measurements
    synth_data = {}

    # --- Use precomputed or compute fresh ---
    file_synth_iter = {}
    if precomputed_synth:
        file_synth_iter = precomputed_synth
    elif trees and sources and all_signals:
        for fname in trees:
            est = SynthesisEstimator(trees[fname], sources[fname],
                                     all_signals.get(fname, {}))
            file_synth_iter[fname] = est.estimate()

    for fname, file_data in file_synth_iter.items():
            if not synth_data:
                synth_data = dict(file_data)
            else:
                # Merge multi-file estimates
                synth_data["ff_bits"] += file_data["ff_bits"]
                synth_data["latch_bits"] += file_data["latch_bits"]
                synth_data["total_gates"] += file_data["total_gates"]
                for k in synth_data["gate_breakdown"]:
                    synth_data["gate_breakdown"][k] += file_data["gate_breakdown"].get(k, 0)
                old_t = synth_data["timing"]
                new_t = file_data["timing"]
                if new_t["critical_path_ns"] > old_t["critical_path_ns"]:
                    synth_data["timing"] = new_t
                synth_data["area_um2"] += file_data["area_um2"]
                synth_data["power"]["high_toggle_bits"] += file_data["power"]["high_toggle_bits"]
                synth_data["power"]["ungated_ff_bits"] += file_data["power"]["ungated_ff_bits"]
                synth_data["power"]["gated_ff_bits"] += file_data["power"]["gated_ff_bits"]
                for k in ("adders", "multipliers", "comparators",
                         "state_decodes", "shifters"):
                    synth_data["operators"][k] = (
                        synth_data["operators"].get(k, 0)
                        + file_data["operators"].get(k, 0))
                synth_data["operators"].setdefault("adder_details", []).extend(
                    file_data["operators"].get("adder_details", []))
                synth_data["operators"].setdefault("comparator_details", []).extend(
                    file_data["operators"].get("comparator_details", []))
                synth_data["operators"].setdefault("state_decode_details", []).extend(
                    file_data["operators"].get("state_decode_details", []))
                synth_data.setdefault("ce_signals", []).extend(
                    file_data.get("ce_signals", []))
                synth_data.setdefault("non_ce_signals", []).extend(
                    file_data.get("non_ce_signals", []))
                synth_data["memory"]["arrays"] += file_data["memory"]["arrays"]
                synth_data["memory"]["total_bits"] += file_data["memory"]["total_bits"]
                synth_data["memory"]["details"].extend(file_data["memory"]["details"])
                synth_data["fpga_resources"]["dsp_blocks"] += file_data["fpga_resources"]["dsp_blocks"]
                synth_data["fpga_resources"]["bram_blocks"] += file_data["fpga_resources"]["bram_blocks"]
                synth_data["fpga_resources"]["lut_count"] += file_data["fpga_resources"]["lut_count"]
                synth_data["fpga_resources"]["ff_count"] += file_data["fpga_resources"]["ff_count"]
                synth_data["ff_signals"].update(file_data.get("ff_signals", {}))
                synth_data["power"]["enabled_blocks"] += file_data["power"].get("enabled_blocks", 0)
                synth_data["power"]["total_seq_blocks"] += file_data["power"].get("total_seq_blocks", 0)
                f_fsm = file_data.get("fsm", {})
                if f_fsm.get("states", 0) > synth_data.get("fsm", {}).get("states", 0):
                    synth_data["fsm"] = f_fsm
                f_fo = file_data.get("fanout", {})
                old_hf = synth_data.get("fanout", {}).get("high_fanout_signals", [])
                new_hf = f_fo.get("high_fanout_signals", [])
                merged_hf = old_hf + new_hf
                merged_hf.sort(key=lambda x: -x[1])
                synth_data["fanout"] = {
                    "high_fanout_signals": merged_hf[:10],
                    "max_fanout": merged_hf[0] if merged_hf else None,
                }
                f_opt = file_data.get("optimization", {})
                old_opt = synth_data.get("optimization", {})
                old_opt["const_prop_count"] = old_opt.get("const_prop_count", 0) + f_opt.get("const_prop_count", 0)
                old_opt["dead_branches"] = old_opt.get("dead_branches", 0) + f_opt.get("dead_branches", 0)
                old_opt.setdefault("constant_outputs", []).extend(f_opt.get("constant_outputs", []))
                old_opt.setdefault("details", []).extend(f_opt.get("details", []))
                for k, v in f_opt.get("constant_signals", {}).items():
                    old_opt.setdefault("constant_signals", {})[k] = v
                synth_data["optimization"] = old_opt

    # Store raw synthesis data in measurements
    if synth_data:
        result.measurements["cogni.synth"] = synth_data

    # --- Generate predictions from AST data ---
    if synth_data:
        ops = synth_data["operators"]
        mem = synth_data["memory"]
        fsm = synth_data.get("fsm", {})
        pwr = synth_data["power"]
        fo = synth_data.get("fanout", {})

        # Inference predictions — what hardware will synthesis create
        latch_sigs = set()
        for f in result.findings:
            if f.rule == 'W402_latch_inferred':
                sig_m = re.search(r"'(\w+)'", f.message)
                if sig_m:
                    latch_sigs.add(sig_m.group(1))
        synth_data["latch_signal_names"] = sorted(latch_sigs)
        multi_driver_sigs = set()
        for f in result.findings:
            if f.rule == 'W_multi_driver':
                sig_m = re.search(r"'(\w+)'", f.message)
                if sig_m:
                    multi_driver_sigs.add(sig_m.group(1))
        if latch_sigs:
            overlap = latch_sigs & multi_driver_sigs
            sig_list = ", ".join(sorted(latch_sigs))
            detail = "Signals: %s" % sig_list
            if overlap:
                detail += " (%s also multi-driven — latch secondary)" % \
                    ", ".join(sorted(overlap))
            predictions.append(SynthPrediction(
                category="inference",
                prediction="Will infer %d unintended latch(es)"
                           % len(latch_sigs),
                confidence="high",
                detail=detail,
            ))

        if ops["multipliers"] > 0:
            predictions.append(SynthPrediction(
                category="inference",
                prediction="Will infer DSP: %d multiplier(s)" % ops["multipliers"],
                confidence="high",
                detail="FPGA: maps to DSP48 blocks | ASIC: dedicated multiplier",
            ))

        if mem["arrays"] > 0:
            bram = synth_data["fpga_resources"]["bram_blocks"]
            # A multi-write-port array will not infer a standard block RAM in
            # portable synthesis — report inference as template-dependent.
            multi_write_ram = any(
                f.rule == "W_multi_driver"
                and re.search(r'write port|RAM array', f.message, re.I)
                for f in result.findings)
            if multi_write_ram:
                predictions.append(SynthPrediction(
                    category="inference",
                    prediction="Memory: %d-bit RAM detected — portable BRAM "
                               "inference: NO (multiple write ports)"
                               % mem["total_bits"],
                    confidence="high",
                    detail="FPGA: infers BRAM only if coded to the vendor "
                           "dual-port RAM template | "
                           "ASIC: requires dual-port SRAM macro",
                ))
            else:
                mem_type = "BRAM" if bram > 0 else "LUTRAM/FF"
                predictions.append(SynthPrediction(
                    category="inference",
                    prediction="Will infer %s: %d array(s), %d bits"
                               % (mem_type, mem["arrays"], mem["total_bits"]),
                    confidence="high",
                    detail="FPGA: %d BRAM candidate(s) | "
                           "ASIC: synthesizable as a flip-flop array; a "
                           "dedicated SRAM macro is recommended for area/power"
                           % bram,
                ))

        if ops["adders"] > 0:
            adder_exprs = [d["expr"] for d in ops.get("adder_details", [])]
            detail = "FPGA: dedicated carry chain | ASIC: ripple/CLA adder"
            if adder_exprs:
                detail = ", ".join(adder_exprs[:4]) + " | " + detail
            predictions.append(SynthPrediction(
                category="inference",
                prediction="Will infer carry chain: %d adder(s)"
                           % ops["adders"],
                confidence="high",
                detail=detail,
            ))

        if ops["comparators"] > 0:
            cmp_details = ops.get("comparator_details", [])
            cmp_exprs = [d["expr"] for d in cmp_details]
            # Classify each: magnitude (<,>,<=,>=) vs equality (==,!=).
            mag = sum(1 for d in cmp_details
                      if re.search(r'[<>]=?', d["expr"])
                      and not re.search(r'==|!=', d["expr"]))
            eq = ops["comparators"] - mag
            kinds = []
            if mag:
                kinds.append(f"{mag} magnitude")
            if eq:
                kinds.append(f"{eq} equality")
            detail = "FPGA: LUT-based | ASIC: " + (
                "magnitude comparator" if mag else "equality (XNOR-AND tree)")
            if cmp_exprs:
                detail = ", ".join(cmp_exprs[:5]) + " | " + detail
            predictions.append(SynthPrediction(
                category="inference",
                prediction="Will infer %d comparator(s): %s"
                           % (ops["comparators"], ", ".join(kinds)),
                confidence="high",
                detail=detail,
            ))

        sd_count = ops.get("state_decodes", 0)
        if sd_count > 0:
            sd_exprs = [d["expr"] for d in ops.get("state_decode_details", [])]
            detail = ", ".join(sd_exprs[:5])
            predictions.append(SynthPrediction(
                category="inference",
                prediction="State decode logic: %d comparison(s)"
                           % sd_count,
                confidence="high",
                detail=detail,
            ))

        if ops["shifters"] > 0:
            predictions.append(SynthPrediction(
                category="inference",
                prediction="Will infer %d barrel shifter(s) / shift mux"
                           % ops["shifters"],
                confidence="high",
                detail="FPGA: LUT-based shift network | "
                       "ASIC: mux tree for variable shift amount",
            ))

        if fsm.get("states", 0) > 0:
            predictions.append(SynthPrediction(
                category="inference",
                prediction="Will infer FSM: %d states, %d-bit encoding"
                           % (fsm["states"], fsm["encoding_bits"]),
                confidence="high",
                detail="One-hot: %d FFs | Binary: %d FFs — "
                       "tool chooses encoding based on target"
                       % (fsm["states"], fsm["min_bits"]),
            ))

        ce_sigs = synth_data.get("ce_signals", [])
        non_ce = synth_data.get("non_ce_signals", [])
        if ce_sigs:
            detail = "CE: %s" % ", ".join(ce_sigs[:5])
            if non_ce:
                detail += " | No CE: %s" % ", ".join(non_ce[:5])
            predictions.append(SynthPrediction(
                category="inference",
                prediction="Clock-enable candidates: %d of %d register blocks"
                           % (len(ce_sigs), len(ce_sigs) + len(non_ce)),
                confidence="medium",
                detail=detail + " (conditional-update regs; synthesis chooses "
                                "CE vs mux based on target library)",
            ))

        # Optimization predictions — what synthesis will remove
        hf = fo.get("high_fanout_signals", [])
        if hf:
            top3 = ", ".join("%s(%d)" % (s, c) for s, c in hf[:3])
            predictions.append(SynthPrediction(
                category="optimization",
                prediction="%d high-fanout signal(s) — synthesis will buffer"
                           % len(hf),
                confidence="medium",
                detail="Top: %s" % top3,
            ))

    # --- Constant propagation / dead code predictions ---
    if synth_data:
        opt = synth_data.get("optimization", {})
        const_outs = opt.get("constant_outputs", [])
        const_sigs = opt.get("constant_signals", {})
        opt_details = opt.get("details", [])

        if opt_details:
            for d in opt_details:
                predictions.append(SynthPrediction(
                    category="optimization",
                    prediction="Constant propagation: %s" % d["detail"],
                    confidence="high",
                    detail="Line %d — branch removed by synthesis" % d["line"],
                ))

        if const_outs:
            for name in const_outs:
                val = const_sigs.get(name, '?')
                predictions.append(SynthPrediction(
                    category="optimization",
                    prediction="Output '%s' is constant %s — no logic needed"
                               % (name, val),
                    confidence="high",
                    detail="Synthesis ties output to constant; "
                           "driving logic optimized away",
                ))

        if const_sigs and not const_outs:
            for name, val in const_sigs.items():
                predictions.append(SynthPrediction(
                    category="optimization",
                    prediction="Signal '%s' reduced to constant %s" % (name, val),
                    confidence="high",
                    detail="Dead logic eliminated after constant propagation",
                ))

    # --- Lint-derived predictions (synthesis blockers) ---
    blk = m.get("cogni.lint.blocking_in_seq.count", 0)
    mix = m.get("cogni.lint.mixed_assignments.count", 0)
    if blk > 0 or mix > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction="Gate-level sim will differ from RTL sim",
            confidence="high",
            detail="%d blocking-in-seq + %d mixed assignments" % (blk, mix),
        ))

    multi = m.get("cogni.lint.multiple_drivers.count", 0)
    if multi > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction="%d signal(s) with multiple drivers — synthesis will fail"
                       % multi,
            confidence="high",
            detail="DC: Error ELAB-366 | Vivado: [Synth 8-6859] | "
                   "Genus: CDFG-xxx — no netlist generated",
        ))

    loops = m.get("cogni.lint.comb_loop.count", 0)
    if loops > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction="Synthesis will FAIL — %d combinational loop(s)" % loops,
            confidence="high",
            detail="DC: Warning UID-95, optimization disabled | "
                   "Vivado: [Synth 8-295] | timing analysis unreliable",
        ))

    oor = m.get("cogni.lint.comparison_out_of_range.count", 0)
    if oor > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction="%d comparison(s) optimized to constant — dead logic"
                       % oor,
            confidence="high",
            detail="Synthesis removes unreachable branches",
        ))

    if not predictions:
        predictions.append(SynthPrediction(
            category="clean",
            prediction="No synthesis issues predicted",
            confidence="high",
            detail="RTL looks clean for synthesis",
        ))

    return predictions


# ===================================================================
# Console formatting
# ===================================================================

def format_analysis(result: AnalysisResult) -> str:
    lines = [f"=== COGNI RTL ANALYZER ({len(ALL_CHECKS)} rules, AST-based) ==="]
    lines.append(f"  Files analyzed: {len(result.files)}")
    lines.append("")

    # ---- RTL Lint Summary (top-level overview) ----
    errors = [f for f in result.findings if f.severity == 'error']
    warnings = [f for f in result.findings if f.severity == 'warning']
    infos = [f for f in result.findings if f.severity == 'info']

    lines.append(f"{'=' * 50}")
    lines.append("RTL LINT SUMMARY")
    lines.append(f"{'=' * 50}")
    lines.append(f"  Errors   : {len(errors)}")
    lines.append(f"  Warnings : {len(warnings)}")
    lines.append(f"  Info     : {len(infos)}")
    lines.append(f"  Total    : {len(result.findings)}")

    # Critical functional risks
    critical_rules = {
        "FUNC_comb_loop": "Combinational loop",
        "FUNC_counter_overflow": "Counter overflow",
        "FUNC_cmp_out_of_range": "Dead branch (comparison impossible)",
        "W_multi_driver": "Multiple drivers",
        "W263_case_no_default": "Missing case default (latch)",
        "ELAB_cdc_crossing": "Clock domain crossing",
        "ELAB_missing_module": "Missing module definition",
    }
    active_critical = []
    for rule, label in critical_rules.items():
        count = sum(1 for f in result.findings if f.rule == rule)
        if count > 0:
            active_critical.append(f"  !! {label} ({count})")

    if active_critical:
        lines.append("")
        lines.append("  Critical Risks:")
        lines.extend(active_critical)

    # Synthesis verdict
    has_comb_loop = any(f.rule == "FUNC_comb_loop" for f in result.findings)
    has_multi_driver = any(f.rule == "W_multi_driver" for f in result.findings)
    if has_comb_loop or has_multi_driver:
        lines.append("")
        lines.append("  Synthesis: !! FAIL")
    elif errors:
        lines.append("")
        lines.append("  Synthesis: ! WARNINGS — review errors before tapeout")
    else:
        lines.append("")
        lines.append("  Synthesis: PASS")
    lines.append("")

    # ---- Detailed findings by category ----
    if result.findings:
        _CATEGORY_MAP = {
            'FUNC_': 'FUNCTIONAL', 'FSM_': 'FUNCTIONAL',
            'ELAB_': 'ELABORATION',
            'PWR_': 'POWER', 'MEM_': 'POWER',
            'STYLE_': 'STYLE',
            'CLK_': 'CLOCK/RESET',
        }
        _SEVERITY_ORDER = ['error', 'warning', 'info']

        def _categorize(f):
            for prefix, cat in _CATEGORY_MAP.items():
                if f.rule.startswith(prefix):
                    return cat
            if f.severity == 'error':
                return 'ERRORS'
            if f.severity == 'warning':
                return 'WARNINGS'
            return 'INFO'

        SECTION_ORDER = ['ERRORS', 'WARNINGS', 'FUNCTIONAL',
                         'ELABORATION', 'CLOCK/RESET', 'POWER',
                         'STYLE', 'INFO']

        by_category: dict[str, list[Finding]] = {}
        for f in result.findings:
            cat = _categorize(f)
            by_category.setdefault(cat, []).append(f)

        for section in SECTION_ORDER:
            items = by_category.get(section)
            if not items:
                continue
            lines.append(f"{'=' * 50}")
            lines.append(f"{section} ({len(items)})")
            lines.append(f"{'=' * 50}")

            by_rule: dict[str, list[Finding]] = {}
            for f in items:
                by_rule.setdefault(f.rule, []).append(f)
            for rule in sorted(by_rule,
                               key=lambda r: _SEVERITY_ORDER.index(
                                   by_rule[r][0].severity)
                               if by_rule[r][0].severity in _SEVERITY_ORDER
                               else 99):
                rule_items = by_rule[rule]
                sev = rule_items[0].severity.upper()
                lines.append(f"  [{sev:>7}] {rule} ({len(rule_items)})")
                for item in rule_items[:3]:
                    lines.append(
                        f"           {item.file}:{item.line} "
                        f"-- {item.message}")
                if len(rule_items) > 3:
                    lines.append(
                        f"           ... and {len(rule_items)-3} more")
            lines.append("")

    if result.predictions:
        lines.append(f"{'=' * 50}")
        lines.append("SYNTHESIS PREDICTIONS")
        lines.append(f"{'=' * 50}")

        # Group predictions by category
        cat_order = ["optimization", "area", "timing", "power", "functional", "latch", "clean"]
        cat_labels = {
            "optimization": "OPTIMIZATION (CONST PROP / DEAD CODE)",
            "area": "AREA & RESOURCES",
            "timing": "TIMING",
            "power": "POWER",
            "functional": "FUNCTIONAL RISKS",
            "latch": "LATCH INFERENCE",
            "clean": "STATUS",
        }
        by_cat: dict[str, list] = {}
        for p in result.predictions:
            by_cat.setdefault(p.category, []).append(p)

        for cat in cat_order:
            preds = by_cat.get(cat)
            if not preds:
                continue
            lines.append(f"  --- {cat_labels.get(cat, cat.upper())} ---")
            for p in preds:
                icon = {"high": ">>", "medium": " >", "low": " ~"}.get(
                    p.confidence, " ?")
                lines.append(f"  {icon} {p.prediction}")
                lines.append(f"       {p.detail}")
            lines.append("")

        # Show synthesis data summary if available
        sd = result.measurements.get("cogni.synth")
        if sd:
            lines.append(f"  --- SYNTHESIS SCORECARD ---")
            lines.append(f"  Gate Equivalents : {sd['total_gates']:,}")
            lines.append(f"  Flip-Flop Bits   : {sd['ff_bits']}")
            lines.append(f"  Critical Path    : {sd['timing']['critical_path_ns']:.2f} ns")
            lines.append(f"  Max Frequency    : ~{sd['timing']['max_freq_mhz']:.0f} MHz")
            lines.append(f"  Area (est.)      : ~{sd['area_um2']:.0f} um2")
            lines.append(f"  Clock Gating     : {sd['power']['clock_gating_pct']:.0f}%")
            if sd['memory']['total_bits'] > 0:
                lines.append(f"  Memory Bits      : {sd['memory']['total_bits']:,}")
            lines.append("")

    return "\n".join(lines)


# ===================================================================
# Phase 5: COGNI AGENT — LLM-powered review layer
# ===================================================================

def _parse_llm_json(raw: str):
    """Parse JSON from LLM output, tolerating common formatting issues."""
    import json as _j
    # First try direct parse
    try:
        return _j.loads(raw)
    except _j.JSONDecodeError:
        pass
    # Try to extract each top-level array independently
    keys = ["false_positives", "missing_bugs", "fsm_consequences", "suggested_rules"]
    data = {}
    for key in keys:
        pattern = r'"%s"\s*:\s*\[' % key
        m = re.search(pattern, raw)
        if not m:
            data[key] = []
            continue
        start = m.end() - 1
        depth = 0
        end = start
        for i in range(start, len(raw)):
            if raw[i] == '[':
                depth += 1
            elif raw[i] == ']':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        arr_str = raw[start:end]
        arr_str = re.sub(r',\s*\]', ']', arr_str)
        try:
            data[key] = _j.loads(arr_str)
        except _j.JSONDecodeError:
            # Last resort: extract individual objects
            objs = []
            for obj_m in re.finditer(r'\{[^{}]*\}', arr_str):
                try:
                    objs.append(_j.loads(obj_m.group()))
                except _j.JSONDecodeError:
                    pass
            data[key] = objs
    return data if any(data.values()) else None


def agent_review(result: AnalysisResult, rtl_sources: dict[str, str],
                 model: str = "us.anthropic.claude-sonnet-4-6",
                 region: str = "us-east-1") -> AnalysisResult:
    """Layer 2: Cogni Agent reviews static findings against RTL source via AWS Bedrock.

    Reads every finding + the actual RTL code, then:
    1. Flags false positives with reasoning
    2. Discovers missing bugs the static engine missed
    3. Traces FSM consequences (overflow -> stuck state -> unreachable)
    4. Suggests concrete fixes

    Returns an enriched AnalysisResult with agent annotations.
    """
    import boto3, json as _json

    findings_text = "\n".join(
        "  [%7s] %s %s:%d -- %s" % (f.severity, f.rule, f.file, f.line, f.message)
        for f in result.findings)

    sources_text = ""
    for fname, src in rtl_sources.items():
        numbered = "\n".join(
            "%4d| %s" % (i+1, line)
            for i, line in enumerate(src.splitlines()))
        sources_text += "\n--- %s ---\n%s\n" % (fname, numbered)

    kb = _load_knowledge()
    prior_context = ""
    waivers = kb.get("waivers", [])
    learned = kb.get("learned_rules", [])
    if waivers or learned:
        prior_context = "\nALREADY KNOWN — do NOT re-suggest anything that overlaps these:\n"
        if waivers:
            prior_context += "\nWaived findings (already suppressed):\n"
            for w in waivers:
                prior_context += "  - %s (line %d): %s\n" % (
                    w.get("rule", "?"), w.get("line", 0),
                    w.get("reason", "")[:120])
        if learned:
            prior_context += "\nLearned rules (already auto-checking):\n"
            for r in learned:
                prior_context += "  - %s: %s [pattern: %s]\n" % (
                    r.get("name", "?"),
                    r.get("description", "")[:100],
                    r.get("pattern", "")[:60])
        prior_context += ("\nDo NOT suggest rules that duplicate the above "
                          "(even under a different name). "
                          "Only suggest genuinely NEW patterns.\n\n")

    # Static context — cached across loop iterations (RTL source + instructions + known rules)
    system_text = (
        "You are a senior RTL verification engineer reviewing lint findings.\n\n"
        "Below is the RTL source code being analyzed by "
        "Cogni (151-rule AST-based analyzer).\n\n"
        + prior_context +
        "IMPORTANT: Do NOT flag standard RTL idioms as bugs. These are ACCEPTABLE:\n"
        "- `count + 1'b1` or `+ 1` for counter increments\n"
        "- Registered outputs not driven on every branch (FF holds value)\n"
        "- Standard synchronous reset patterns\n"
        "- One-hot or binary FSM encodings\n"
        "Only flag issues that are genuinely bugs or will cause synthesis/simulation problems.\n\n"
        "RTL SOURCE:\n" + sources_text + "\n\n"
        "Your job:\n\n"
        "1. **FALSE POSITIVES**: List any findings that are false positives. "
        "For each, give the rule, line, and why it's wrong.\n\n"
        "2. **MISSING BUGS**: List any real RTL bugs the static analyzer missed. "
        "For each, give the line number, what the bug is, and what category it would be "
        "(e.g., FUNC, FSM, CDC, TIMING).\n\n"
        "3. **FSM CONSEQUENCES**: For any counter overflow, comparison-out-of-range, "
        "or dead branch -- trace the FSM consequence. Example: \"bit_cnt wraps at 7, "
        "so bit_cnt==8 never true, so DATA->STOP_BIT transition never fires, so FSM "
        "loops DATA->DATA forever, making STOP_BIT and FINISH unreachable.\"\n\n"
        "4. **SUGGESTED RULES**: If you see patterns that should become new static rules "
        "(not one-off bugs), describe each rule: name, what it checks, why it matters. "
        "Include a `pattern` field and a `check_type` field.\n"
        "   check_type options:\n"
        "   - \"regex\": Python regex matched against RTL source text\n"
        "   - \"param_expr\": Parameter expression evaluated with resolved param values. "
        "Returns true when the condition is met (i.e. the bug exists). "
        "Example: pattern=\"DEPTH < 2\" fires when DEPTH is 0 or 1. "
        "Available ops: +,-,*,/,%,**,<<,>>,&,|,^,==,!=,>,<,>=,<=,&&,||,$clog2(),$bits()\n\n"
        "Respond in this exact JSON format:\n"
        '{\n'
        '  "false_positives": [\n'
        '    {"rule": "...", "line": 0, "reason": "..."}\n'
        '  ],\n'
        '  "missing_bugs": [\n'
        '    {"line": 0, "severity": "error|warning|info", "category": "...", '
        '"message": "...", "fix": "..."}\n'
        '  ],\n'
        '  "fsm_consequences": [\n'
        '    {"trigger": "...", "chain": "...", "impact": "..."}\n'
        '  ],\n'
        '  "suggested_rules": [\n'
        '    {"name": "...", "description": "...", "rationale": "...", '
        '"pattern": "regex_here", "check_type": "regex", "severity": "warning"}\n'
        '  ]\n'
        '}'
    )

    # Build elaboration context (resolved params, widths, hierarchy)
    elab_context = ""
    elab = result.measurements.get("cogni.elab.modules")
    if elab:
        elab_context += "\nELABORATION CONTEXT:\n"
        elab_context += f"  Modules: {elab}\n"
        tops = result.measurements.get("cogni.elab.top_modules", [])
        if tops:
            elab_context += f"  Top modules: {', '.join(tops)}\n"
        hier = result.measurements.get("cogni.elab.hierarchy", "")
        if hier:
            elab_context += f"  Hierarchy:\n{hier}\n"
    # Include resolved parameters per file
    param_context = ""
    for fname, sigs in (
        (os.path.basename(f), _elaborate_from_text(
            open(f, encoding='utf-8', errors='replace').read()))
        for f in result.files if os.path.isfile(f)
    ):
        params = [(s.name, s.param_value) for s in sigs.values()
                  if s.is_param and s.param_value is not None]
        if params:
            param_context += f"\n  {fname}: " + ", ".join(
                f"{n}={v}" for n, v in params)
    if param_context:
        elab_context += "\nRESOLVED PARAMETERS:" + param_context + "\n"

    # Dynamic part — changes each iteration (current findings)
    user_text = ("STATIC FINDINGS (" + str(len(result.findings)) + "):\n"
                 + findings_text
                 + elab_context
                 + "\n\nReview these findings against the RTL source above.")

    try:
        from dotenv import load_dotenv
        from botocore.config import Config
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
        load_dotenv(env_path)

        client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_DEFAULT_REGION", region),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            config=Config(read_timeout=120, connect_timeout=10),
        )
        body = _json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_text}],
        })
        resp = client.invoke_model(modelId=model, body=body)
        resp_body = _json.loads(resp["body"].read())
        text = resp_body["content"][0]["text"]
        # Try ```json ... ``` block first, then outermost { ... }
        code_match = re.search(r'```json\s*([\s\S]*?)\s*```', text)
        raw = code_match.group(1) if code_match else None
        if not raw:
            json_match = re.search(r'\{[\s\S]*\}', text)
            if not json_match:
                return result
            raw = json_match.group()
        # Strip trailing commas before ] or } (common LLM mistake)
        raw = re.sub(r',\s*([}\]])', r'\1', raw)
        data = _parse_llm_json(raw)
        if data is None:
            return result
    except Exception as e:
        print("[Cogni Agent] Bedrock call failed: %s" % e)
        return result

    # Enrich the result with agent findings
    enriched = AnalysisResult(
        files=result.files,
        findings=list(result.findings),
        predictions=list(result.predictions),
        measurements=dict(result.measurements),
    )

    # Mark false positives
    fp_keys = set()
    for fp in data.get("false_positives", []):
        fp_keys.add((fp.get("rule", ""), fp.get("line", 0)))
    enriched.findings = [
        f for f in enriched.findings
        if (f.rule, f.line) not in fp_keys
    ]

    # Add missing bugs as new findings
    for bug in data.get("missing_bugs", []):
        file = result.files[0] if result.files else "unknown"
        enriched.findings.append(Finding(
            rule=f"AGENT_{bug.get('category', 'FUNC')}",
            severity=bug.get("severity", "warning"),
            file=os.path.basename(file),
            line=bug.get("line", 0),
            message=bug.get("message", ""),
            synth_impact=bug.get("fix", ""),
        ))

    # Add FSM consequence findings
    for fsm in data.get("fsm_consequences", []):
        enriched.findings.append(Finding(
            rule="AGENT_FSM_consequence",
            severity="error",
            file=os.path.basename(result.files[0]) if result.files else "",
            line=0,
            message=f"{fsm.get('trigger', '')}: {fsm.get('chain', '')}",
            synth_impact=fsm.get("impact", ""),
        ))

    # Store agent metadata
    enriched.measurements["cogni.agent.false_positives"] = len(
        data.get("false_positives", []))
    enriched.measurements["cogni.agent.missing_bugs"] = len(
        data.get("missing_bugs", []))
    enriched.measurements["cogni.agent.fsm_consequences"] = len(
        data.get("fsm_consequences", []))
    enriched.measurements["cogni.agent.suggested_rules"] = [
        r.get("name", "") for r in data.get("suggested_rules", [])]

    # --- Persist learnings to disk ---
    kb = _load_knowledge()

    # Save false-positive waivers so they auto-suppress on future runs
    for fp in data.get("false_positives", []):
        waiver = {
            "rule": fp.get("rule", ""),
            "line": fp.get("line", 0),
            "reason": fp.get("reason", ""),
            "file_pattern": "",
            "message_pattern": "",
        }
        if not any(w["rule"] == waiver["rule"] and w["line"] == waiver["line"]
                   for w in kb["waivers"]):
            kb["waivers"].append(waiver)

    # Save learned rules so they run as checkers on future analyses
    for sr in data.get("suggested_rules", []):
        rule_name = sr.get("name", "").strip()
        if not rule_name:
            continue
        rule_name = re.sub(r'\W+', '_', rule_name)
        if not rule_name.startswith("LEARNED_"):
            rule_name = "LEARNED_" + rule_name
        learned = {
            "name": rule_name,
            "description": sr.get("description", ""),
            "rationale": sr.get("rationale", ""),
            "check_type": sr.get("check_type", "regex"),
            "pattern": sr.get("pattern", ""),
            "severity": sr.get("severity", "warning"),
            "message": sr.get("description", ""),
        }
        existing_names = {r["name"] for r in kb["learned_rules"]}
        existing_patterns = {r.get("pattern", "") for r in kb["learned_rules"]
                             if r.get("pattern")}
        if learned["name"] in existing_names:
            continue
        if learned.get("pattern", "") in existing_patterns:
            continue
        dup_of = _is_semantic_duplicate(
            learned["name"], learned.get("description", ""),
            kb["learned_rules"])
        if dup_of:
            continue
        kb["learned_rules"].append(learned)

    # Append review summary to history
    kb["review_history"].append({
        "files": [os.path.basename(f) for f in result.files],
        "fp_removed": len(data.get("false_positives", [])),
        "missing_found": len(data.get("missing_bugs", [])),
        "fsm_consequences": len(data.get("fsm_consequences", [])),
        "rules_learned": len(data.get("suggested_rules", [])),
    })
    if len(kb["review_history"]) > 100:
        kb["review_history"] = kb["review_history"][-100:]

    _save_knowledge(kb)
    enriched.measurements["cogni.agent.waivers_total"] = len(kb["waivers"])
    enriched.measurements["cogni.agent.learned_rules_total"] = len(kb["learned_rules"])

    return enriched


def analyze_with_agent(rtl_files: list[str],
                       model: str = "us.anthropic.claude-sonnet-4-6",
                       region: str = "us-east-1") -> AnalysisResult:
    """Full analysis pipeline: static engine + Cogni agent review."""
    result = analyze_design(rtl_files)

    sources = {}
    for f in rtl_files:
        if os.path.isfile(f):
            with open(f, encoding='utf-8', errors='replace') as fh:
                sources[os.path.basename(f)] = fh.read()

    if sources:
        result = agent_review(result, sources, model=model, region=region)

    return result


def agent_review_loop(rtl_files: list[str],
                      rtl_sources: dict[str, str] | None = None,
                      max_iterations: int = 3,
                      model: str = "us.anthropic.claude-sonnet-4-6",
                      region: str = "us-east-1",
                      on_iteration=None) -> dict:
    """Looping agent review: analyze → review → learn → re-analyze until convergence.

    Returns a dict with the final AnalysisResult plus iteration log.
    on_iteration(iteration_num, summary_dict) is called after each round if provided.
    """
    kb_before = _load_knowledge()
    prev_waivers = len(kb_before.get("waivers", []))
    prev_rules = len(kb_before.get("learned_rules", []))

    sources = dict(rtl_sources) if rtl_sources else {}
    if not sources:
        for f in rtl_files:
            if os.path.isfile(f):
                with open(f, encoding='utf-8', errors='replace') as fh:
                    sources[os.path.basename(f)] = fh.read()

    iterations = []
    result = None
    prev_finding_keys = set()

    for i in range(1, max_iterations + 1):
        result = analyze_design(rtl_files)
        finding_count = len(result.findings)

        result = agent_review(result, sources, model=model, region=region)

        kb_after = _load_knowledge()
        new_waivers = len(kb_after.get("waivers", [])) - prev_waivers
        new_rules = len(kb_after.get("learned_rules", [])) - prev_rules

        fp_removed = result.measurements.get("cogni.agent.false_positives", 0)
        missing_found = result.measurements.get("cogni.agent.missing_bugs", 0)
        fsm_count = result.measurements.get("cogni.agent.fsm_consequences", 0)

        curr_finding_keys = {(f.rule, f.line, f.file) for f in result.findings}

        summary = {
            "iteration": i,
            "findings_in": finding_count,
            "findings_out": len(result.findings),
            "fp_removed": fp_removed,
            "missing_found": missing_found,
            "fsm_consequences": fsm_count,
            "new_waivers": new_waivers,
            "new_rules": new_rules,
            "total_waivers": len(kb_after.get("waivers", [])),
            "total_rules": len(kb_after.get("learned_rules", [])),
        }
        iterations.append(summary)

        if on_iteration:
            on_iteration(i, summary)

        prev_waivers = len(kb_after.get("waivers", []))
        prev_rules = len(kb_after.get("learned_rules", []))

        findings_stable = (curr_finding_keys == prev_finding_keys) if i > 1 else False
        no_new_knowledge = (new_waivers == 0 and new_rules == 0)
        count_stable = (i > 1 and
                        abs(len(curr_finding_keys) - len(prev_finding_keys)) <= 1)

        if findings_stable or no_new_knowledge or count_stable:
            summary["converged"] = True
            break

        prev_finding_keys = curr_finding_keys

    return {
        "result": result,
        "iterations": iterations,
        "converged": len(iterations) > 0 and iterations[-1].get("converged", False),
        "total_iterations": len(iterations),
    }
