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

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

import tree_sitter_verilog as tsv
import tree_sitter as ts

_SV_LANG = ts.Language(tsv.language())
_PARSER = ts.Parser(_SV_LANG)


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


@dataclass
class ElaborationResult:
    module_db: dict[str, ModuleInfo] = field(default_factory=dict)
    top_modules: list[str] = field(default_factory=list)
    hierarchy: dict[str, list[InstanceInfo]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)


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


def _is_inside(inner, outer) -> bool:
    return (inner.start_byte >= outer.start_byte and
            inner.end_byte <= outer.end_byte)


# ---------------------------------------------------------------------------
# Phase 2: ELABORATE
# ---------------------------------------------------------------------------

def _eval_param_expr(expr: str, params: dict[str, int]) -> int | None:
    expr = expr.strip()
    if not expr:
        return None
    if re.fullmatch(r'\d+', expr):
        return int(expr)
    if expr in params:
        return params[expr]

    m = re.match(r'\$clog2\s*\((.+)\)', expr)
    if m:
        inner = _eval_param_expr(m.group(1), params)
        if inner is not None and inner > 0:
            return max(1, math.ceil(math.log2(inner)))
        return None

    for op_char, op_fn in [
        ('-', lambda a, b: a - b),
        ('+', lambda a, b: a + b),
        ('*', lambda a, b: a * b),
    ]:
        depth = 0
        for i in range(len(expr) - 1, 0, -1):
            if expr[i] == ')':
                depth += 1
            elif expr[i] == '(':
                depth -= 1
            elif expr[i] == op_char and depth == 0:
                left = _eval_param_expr(expr[:i], params)
                right = _eval_param_expr(expr[i+1:], params)
                if left is not None and right is not None:
                    return op_fn(left, right)
                break

    if expr.startswith('(') and expr.endswith(')'):
        return _eval_param_expr(expr[1:-1], params)

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


def _elaborate_from_text(text: str) -> dict[str, SignalInfo]:
    text_clean = re.sub(r'//.*?$', '', text, flags=re.MULTILINE)
    text_clean = re.sub(r'/\*.*?\*/', '', text_clean, flags=re.DOTALL)

    signals: dict[str, SignalInfo] = {}
    params: dict[str, int] = {}

    def _line_at(pos: int) -> int:
        return text_clean[:pos].count('\n') + 1

    for m in re.finditer(
            r'parameter\s+(?:int\s+(?:unsigned\s+)?)?(\w+)\s*=\s*', text_clean):
        name = m.group(1)
        expr = _capture_balanced(text_clean, m.end())
        val = _eval_param_expr(expr, params)
        if val is not None:
            params[name] = val
            signals[name] = SignalInfo(
                name=name, width=32, line=_line_at(m.start()),
                is_param=True, param_value=val)

    for m in re.finditer(
            r'localparam\s+(?:\w+\s+)?(\w+)\s*=\s*', text_clean):
        name = m.group(1)
        expr = _capture_balanced(text_clean, m.end())
        val = _eval_param_expr(expr, params)
        if val is not None:
            params[name] = val
            signals[name] = SignalInfo(
                name=name, width=32, line=_line_at(m.start()),
                is_param=True, param_value=val)

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
        mi.signals = _elaborate_from_text(text)

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
        """Width mismatch between parent signal and child port."""
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
                    if not child_port or child_port.width <= 0:
                        continue
                    # Check if signal_expr has bit/part select
                    if '[' in pc.signal_expr:
                        continue
                    parent_sig = mi.signals.get(pc.signal_expr)
                    if not parent_sig or parent_sig.width <= 0:
                        continue
                    if (parent_sig.width != child_port.width and
                            abs(parent_sig.width - child_port.width) > 0):
                        findings.append(Finding(
                            rule="ELAB_port_width_mismatch", severity="warning",
                            file=inst.file, line=pc.line,
                            message=(f"Width mismatch: signal "
                                     f"'{pc.signal_expr}'({parent_sig.width}b) "
                                     f"connected to port "
                                     f"'.{pc.port_name}'({child_port.width}b) "
                                     f"of '{inst.instance_name}' "
                                     f"({inst.module_name})"),
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


def W263_case_no_default(tree, file, signals):
    """W263: Case without default in combinational block."""
    findings = []
    for always in _find_nodes(tree.root_node, 'always_construct'):
        if _always_type(always) != 'always_comb':
            continue
        for cs in _find_nodes(always, 'case_statement'):
            items = _find_nodes(cs, 'case_item')
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
                for i in _find_nodes(c, 'case_item'))
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
                                        default_assigned.add(s)

        for sig in assigned_sigs - default_assigned:
            for ba in all_ba:
                if _get_lhs_signal(ba) == sig:
                    findings.append(Finding(
                        rule="W402_latch_inferred", severity="warning",
                        file=file, line=_node_line(ba),
                        message=f"'{sig}' not assigned on all paths — latch inferred",
                        synth_impact="Latch: area overhead, timing hazard",
                    ))
                    break
    return findings


def W164_width_mismatch(tree, file, signals):
    """W164: Width mismatch in comparison or assignment."""
    findings = []
    seen = set()

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
    all_text = _node_text(tree.root_node)

    written = set()
    for ntype in ('blocking_assignment', 'nonblocking_assignment',
                  'net_assignment', 'net_decl_assignment'):
        for n in _find_nodes(tree.root_node, ntype):
            sig = _get_lhs_signal(n)
            if sig:
                written.add(sig)

    for sig_name, sig_info in signals.items():
        if sig_info.is_param or sig_info.direction in ('input', 'inout', 'output'):
            continue
        if sig_name not in written:
            continue
        total = len(re.findall(rf'\b{re.escape(sig_name)}\b', all_text))
        assign_count = 0
        for ntype in ('blocking_assignment', 'nonblocking_assignment',
                      'net_assignment', 'net_decl_assignment'):
            assign_count += sum(1 for n in _find_nodes(tree.root_node, ntype)
                                if _get_lhs_signal(n) == sig_name)
        if total <= assign_count + 1:
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
    all_text = _node_text(tree.root_node)
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
    all_text = _node_text(tree.root_node)
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
    all_text = _node_text(tree.root_node)
    driven = set()
    for ntype in ('blocking_assignment', 'nonblocking_assignment',
                  'net_assignment', 'net_decl_assignment'):
        for n in _find_nodes(tree.root_node, ntype):
            sig = _get_lhs_signal(n)
            if sig:
                driven.add(sig)
    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        for na in _find_nodes(ca, 'net_assignment'):
            sig = _get_lhs_signal(na)
            if sig:
                driven.add(sig)

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
    all_text = _node_text(tree.root_node)
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
        items = _find_nodes(cs, 'case_item')
        has_default = any('default' in _node_text(i)[:20] for i in items)
        if has_default:
            continue
        # Count case items (exclude default)
        n_items = len([i for i in items
                       if 'default' not in _node_text(i)[:20]])
        # Get case expression to find width
        ce = _find_nodes(cs, 'case_expression')
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
        for item in _find_nodes(cs, 'case_item'):
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

    for sig, blocks in sig_drivers.items():
        unique = sorted(set(blocks))
        if len(unique) >= 2:
            locs = ', '.join(f'L{b}' for b in unique)
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
    # This is the SYNTH version of W402 — synthesizer's perspective
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

        conds = _find_nodes(always, 'conditional_statement')
        for cs in conds:
            text = _node_text(cs)
            if '\nelse' not in text and ' else' not in text:
                for s in assigned:
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

def FUNC_comparison_oor(tree, file, signals):
    """Comparison out of range: signal width cannot hold the value."""
    findings = []
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}

    for binop in _find_nodes_multi(tree.root_node,
                                    {'binary_expression', 'expression'}):
        text = _node_text(binop).strip()
        if '==' not in text and '!=' not in text and \
           '>=' not in text and '<=' not in text and \
           '>' not in text and '<' not in text:
            continue

        for m in re.finditer(r'(\w+)\s*([!=<>]=|[<>])\s*(\w+)', text):
            lhs_name, op, rhs_name = m.group(1), m.group(2), m.group(3)

            for sig_name, val_name in [(lhs_name, rhs_name), (rhs_name, lhs_name)]:
                si = signals.get(sig_name)
                val = params.get(val_name)
                if val is None and re.fullmatch(r'\d+', val_name):
                    val = int(val_name)

                if not si or si.is_param or si.width <= 0 or val is None:
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
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}

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
                # Search for comparisons against this counter in the full tree
                full_text = _node_text(tree.root_node)
                for cm in re.finditer(
                        rf'\b{re.escape(lhs)}\s*==\s*(\w+)', full_text):
                    val_name = cm.group(1)
                    val = params.get(val_name)
                    if val is None and re.fullmatch(r'\d+', val_name):
                        val = int(val_name)
                    if val is not None and val > max_val:
                        findings.append(Finding(
                            rule="FUNC_counter_overflow", severity="error",
                            file=file, line=_node_line(asgn),
                            message=(
                                f"Counter overflow: '{lhs}' is {si.width}-bit "
                                f"(max {max_val}), wraps before reaching "
                                f"{val_name}={val}. "
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


def FUNC_case_width_mismatch(tree, file, signals):
    """Case expression width doesn't match case item widths."""
    findings = []
    params = {s.name: s.param_value for s in signals.values()
              if s.is_param and s.param_value is not None}
    for cs in _find_nodes(tree.root_node, 'case_statement'):
        ce = _find_nodes(cs, 'case_expression')
        if not ce:
            continue
        ce_ids = _get_identifiers(ce[0])
        ce_width = None
        for cid in ce_ids:
            si = signals.get(cid)
            if si and si.width > 0:
                ce_width = si.width
                break
        if ce_width is None:
            continue

        for item in _find_nodes(cs, 'case_item'):
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
            for nba in _find_nodes(always, 'nonblocking_assignment'):
                s = _get_lhs_signal(nba)
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
        for nba in _find_nodes(always, 'nonblocking_assignment'):
            s = _get_lhs_signal(nba)
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
        items = _find_nodes(cs, 'case_item')
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
        width = int(m.group(1)) - int(m.group(2)) + 1
        states = [s.strip().split('=')[0].strip()
                  for s in m.group(3).split(',') if s.strip()]
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
        ids = _find_nodes(mod, 'simple_identifier')
        if ids:
            mod_name = _node_text(ids[0])
            file_stem = os.path.splitext(file)[0]
            if mod_name != file_stem:
                findings.append(Finding(
                    rule="STYLE_mod_name", severity="info",
                    file=file, line=_node_line(ids[0]),
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
    text = _node_text(tree.root_node)
    for kw in ['force', 'release']:
        for m in re.finditer(rf'\b{kw}\s+\w+', text):
            line = text[:m.start()].count('\n') + 1
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
    for atype in ('blocking_assignment', 'nonblocking_assignment'):
        for asgn in _find_nodes(tree.root_node, atype):
            lhs = _get_lhs_signal(asgn)
            if not lhs:
                continue
            children = asgn.named_children
            if len(children) >= 2:
                rhs_text = _node_text(children[-1]).strip()
                if rhs_text == lhs:
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
    all_text = _node_text(tree.root_node)
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
    signed_sigs = set()
    for m in re.finditer(r'\b(?:signed|int)\s+(?:\[[^\]]+\]\s*)?(\w+)', text):
        signed_sigs.add(m.group(1))
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
                if si and si.width > 1 and not si.is_param:
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
            if '+' in rhs_text or '*' in rhs_text:
                rhs_ids = _get_identifiers(children[-1])
                max_rhs_w = 0
                for rid in rhs_ids:
                    ri = signals.get(rid)
                    if ri and ri.width > 0:
                        max_rhs_w = max(max_rhs_w, ri.width)
                if '*' in rhs_text and max_rhs_w > 0:
                    needed = max_rhs_w * 2
                    if li.width < needed and li.width < max_rhs_w + 1:
                        findings.append(Finding(
                            rule="W468_arith_overflow", severity="warning",
                            file=file, line=_node_line(asgn),
                            message=f"'{lhs}'({li.width}b) may overflow: multiply needs {needed}b",
                            synth_impact="Truncated result: wrong arithmetic output",
                        ))
                elif '+' in rhs_text and max_rhs_w > 0:
                    if li.width <= max_rhs_w and li.width < max_rhs_w + 1:
                        n_adds = rhs_text.count('+')
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
    for ca in _find_nodes(tree.root_node, 'continuous_assign'):
        for na in _find_nodes(ca, 'net_assignment'):
            sig = _get_lhs_signal(na)
            if sig:
                driven.add(sig)

    for sig_name, si in signals.items():
        if si.direction in ('input', 'inout') or si.is_param:
            continue
        if si.direction == 'output':
            continue
        if sig_name not in driven:
            all_text = _node_text(tree.root_node)
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
    """W362: Output not driven on all conditional paths."""
    findings = []
    output_sigs = {n for n, s in signals.items() if s.direction == 'output'}
    for always in _find_nodes(tree.root_node, 'always_construct'):
        driven_in_block = set()
        for ntype in ('blocking_assignment', 'nonblocking_assignment'):
            for n in _find_nodes(always, ntype):
                sig = _get_lhs_signal(n)
                if sig and sig in output_sigs:
                    driven_in_block.add(sig)
        if not driven_in_block:
            continue

        conds = _find_nodes(always, 'conditional_statement')
        for cs in conds:
            text = _node_text(cs)
            if '\nelse' not in text and ' else' not in text:
                for out in driven_in_block:
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
        for member in em.group(1).split(','):
            name = member.strip().split('=')[0].strip()
            if name:
                enum_members.add(name)
    # Collect localparam names
    for m in re.finditer(r'localparam\s+(?:\w+\s+)?(\w+)\s*=', text):
        params[m.group(1)] = 0
    constants = set(params) | enum_members

    for cs in _find_nodes(tree.root_node, 'case_statement'):
        for item in _find_nodes(cs, 'case_item'):
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
        ids = _find_nodes(n, 'simple_identifier')
        name = _node_text(ids[0]) if ids else 'unknown'
        findings.append(Finding(
            rule="SYNTH_5011_class", severity="error",
            file=file, line=_node_line(n),
            message=f"Class '{name}' is not synthesizable",
            synth_impact="OOP constructs have no hardware mapping",
        ))
    return findings


def SYNTH_unique_priority(tree, file, signals):
    """SYNTH_5012: unique/priority — all modern tools support these, no warning."""
    return []


def SYNTH_disable_iff(tree, file, signals):
    """SYNTH_5013: disable iff in assertions needs synth translate_off."""
    findings = []
    text = _node_text(tree.root_node)
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
    for m in re.finditer(r'(\w+)\s*([/%])\s*(\d+)', text):
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
            rhs_ids = _get_identifiers(children[-1])
            for rid in rhs_ids:
                ri = signals.get(rid)
                if ri and ri.width > 0 and ri.width > li.width + 1:
                    rhs_text = _node_text(children[-1]).strip()
                    if '[' in rhs_text:
                        continue
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
        items = _find_nodes(cs, 'case_item')
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
        for nba in _find_nodes(always, 'nonblocking_assignment'):
            s = _get_lhs_signal(nba)
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
            ids = _find_nodes(mod, 'simple_identifier')
            if ids:
                names.append(_node_text(ids[0]))
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
        text = _node_text(gen)[:80]
        if ':' not in text.split('\n')[0]:
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

def FSM_unreachable_state(tree, file, signals):
    """FSM state not reachable from reset state (graph-based)."""
    findings = []
    text = _node_text(tree.root_node)
    for m in re.finditer(
            r'typedef\s+enum\s+logic\s*\[\d+:\d+\]\s*\{([^}]+)\}\s*(\w+)',
            text):
        states_text = m.group(1)
        type_name = m.group(2)
        states = [s.strip().split('=')[0].strip()
                  for s in states_text.split(',') if s.strip()]
        if len(states) < 3:
            continue

        # Build transition graph: state -> set of next states
        graph: dict[str, set[str]] = {s: set() for s in states}
        for cs in _find_nodes(tree.root_node, 'case_statement'):
            ce = _find_nodes(cs, 'case_expression')
            if not ce:
                continue
            ce_ids = _get_identifiers(ce[0])
            if not any('state' in i.lower() or i in ('cs', 'ns') for i in ce_ids):
                continue
            for item in _find_nodes(cs, 'case_item'):
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
                            rhs = _node_text(children[-1]).strip()
                            if rhs in graph:
                                graph[label].add(rhs)

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
                        rhs = _node_text(children[-1]).strip()
                        if rhs in graph:
                            reset_state = rhs
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
        ce = _find_nodes(cs, 'case_expression')
        if not ce:
            continue
        ce_text = _node_text(ce[0]).strip()
        if 'state' in ce_text.lower() or 'fsm' in ce_text.lower() or \
           ce_text in ('cs', 'ns', 'current_state', 'next_state', 'state_q'):
            items = _find_nodes(cs, 'case_item')
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
    for func in _find_nodes(tree.root_node, 'function_declaration'):
        ids = _find_nodes(func, 'simple_identifier')
        if ids:
            fname = _node_text(ids[0])
            calls = len(re.findall(rf'\b{re.escape(fname)}\s*\(', text))
            if calls <= 1:
                findings.append(Finding(
                    rule="W192_unused_func", severity="info",
                    file=file, line=_node_line(func),
                    message=f"Function '{fname}' declared but never called",
                    synth_impact="Dead code: synthesis ignores uncalled functions",
                ))
    for task in _find_nodes(tree.root_node, 'task_declaration'):
        ids = _find_nodes(task, 'simple_identifier')
        if ids:
            tname = _node_text(ids[0])
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
    for m in re.finditer(r'([&|^~])\s*(\w+)', text):
        op, sig = m.group(1), m.group(2)
        si = signals.get(sig)
        if si and si.width == 1 and not si.is_param:
            before = text[max(0, m.start()-2):m.start()]
            if not re.search(r'\w', before):
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
            if '-' in rhs_text and '+' not in rhs_text:
                li = signals.get(lhs)
                if li and li.width > 0 and li.width <= 8:
                    text = _node_text(tree.root_node)
                    is_signed = bool(re.search(
                        rf'\bsigned\b[^;]*\b{re.escape(lhs)}\b', text))
                    if not is_signed:
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
                        rule="CLK_divider", severity="info",
                        file=file, line=_node_line(nba),
                        message=f"'{lhs} <= ~{lhs}': clock divider pattern",
                        synth_impact="Derived clock: use clock divider cell or PLL",
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
        ids = _find_nodes(mod, 'simple_identifier')
        if not ids:
            continue
        mod_name = _node_text(ids[0])
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
        ids = _find_nodes(mod, 'simple_identifier')
        if not ids:
            continue
        mod_name = _node_text(ids[0])
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
        states_text = m.group(1)
        states = [s.strip().split('=')[0].strip()
                  for s in states_text.split(',') if s.strip()]
        if len(states) < 3:
            continue

        # Build full transition graph (including self-loops)
        graph: dict[str, set[str]] = {s: set() for s in states}
        for cs in _find_nodes(tree.root_node, 'case_statement'):
            ce = _find_nodes(cs, 'case_expression')
            if not ce:
                continue
            ce_ids = _get_identifiers(ce[0])
            if not any('state' in i.lower() or i in ('cs', 'ns') for i in ce_ids):
                continue
            for item in _find_nodes(cs, 'case_item'):
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
                            rhs = _node_text(children[-1]).strip()
                            if rhs in graph:
                                graph[label].add(rhs)

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
    text = _node_text(tree.root_node)
    for m in re.finditer(r'(\w+)\s*\*\s*(\d+)', text):
        val = int(m.group(2))
        if val > 1 and (val & (val - 1)) == 0:
            shift = int(math.log2(val))
            line = text[:m.start()].count('\n') + 1
            findings.append(Finding(
                rule="PWR_shift_vs_mult", severity="info",
                file=file, line=line,
                message=f"'{m.group(1)} * {val}': use '<< {shift}' instead",
                synth_impact="Multiplier vs shifter: shift is smaller and faster",
            ))
    for m in re.finditer(r'(\d+)\s*\*\s*(\w+)', text):
        val = int(m.group(1))
        if val > 1 and (val & (val - 1)) == 0:
            shift = int(math.log2(val))
            line = text[:m.start()].count('\n') + 1
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
                    if '+' in rhs_text or '-' in rhs_text or '*' in rhs_text:
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
]


# ===================================================================
# Main analyzer
# ===================================================================

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
    return findings


def analyze_design(rtl_files: list[str]) -> AnalysisResult:
    result = AnalysisResult(files=list(rtl_files))

    for f in rtl_files:
        if os.path.isfile(f):
            result.findings.extend(analyze_file(f))

    # Phase 2b: Cross-module elaboration (when multiple files)
    elab_measurements = {}
    if len(rtl_files) > 1:
        try:
            elaborator = DesignElaborator()
            elaborator.parse_files(rtl_files)
            elab = elaborator.elaborate()
            result.findings.extend(elab.findings)
            elab_measurements = {
                "cogni.elab.modules": len(elab.module_db),
                "cogni.elab.top_modules": elab.top_modules,
                "cogni.elab.hierarchy": elaborator.format_hierarchy(elab),
            }
        except Exception:
            pass

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
        "cogni.lint.total_issues": len(result.findings),
    }
    result.measurements.update(elab_measurements)

    result.predictions = _predict_synthesis(result)
    return result


# ===================================================================
# Phase 4: PREDICT
# ===================================================================

def _predict_synthesis(result: AnalysisResult) -> list[SynthPrediction]:
    predictions = []
    m = result.measurements

    latch = m.get("cogni.lint.latch_inference.count", 0)
    if latch > 0:
        predictions.append(SynthPrediction(
            category="latch",
            prediction=f"Synthesis will infer {latch} unintended latch(es)",
            confidence="high",
            detail="Each latch: ~4 gates overhead, timing hazard",
        ))

    blk = m.get("cogni.lint.blocking_in_seq.count", 0)
    mix = m.get("cogni.lint.mixed_assignments.count", 0)
    if blk > 0 or mix > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction="Gate-level sim will differ from RTL sim",
            confidence="high",
            detail=f"{blk} blocking-in-seq + {mix} mixed assignments",
        ))

    multi = m.get("cogni.lint.multiple_drivers.count", 0)
    if multi > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction=f"{multi} signal(s) with multiple drivers",
            confidence="high",
            detail="Bus contention or X propagation",
        ))

    oor = m.get("cogni.lint.comparison_out_of_range.count", 0)
    if oor > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction=f"{oor} comparison(s) always true/false — dead branches",
            confidence="high",
            detail="Signal width can't hold value: unreachable FSM states",
        ))

    dup = m.get("cogni.lint.duplicate_case.count", 0)
    if dup > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction=f"{dup} duplicate case value(s)",
            confidence="high",
            detail="Unreachable arm: sim/synth mismatch",
        ))

    width = m.get("cogni.lint.width_mismatch.count", 0)
    if width > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction=f"{width} width mismatch(es)",
            confidence="medium",
            detail="Implicit extend/truncate may change behavior",
        ))

    depth = m.get("cogni.lint.comb_depth.max", 0)
    if depth >= 4:
        predictions.append(SynthPrediction(
            category="timing",
            prediction=f"Comb depth {depth} — critical path risk",
            confidence="medium",
            detail=f"~{depth * 0.2:.1f}ns added per decision level",
        ))

    mem = m.get("cogni.lint.memory_array.count", 0)
    if mem > 0:
        predictions.append(SynthPrediction(
            category="area",
            prediction=f"{mem} array(s) should use SRAM macros",
            confidence="high",
            detail="FF arrays: ~10x area vs SRAM",
        ))

    loops = m.get("cogni.lint.comb_loop.count", 0)
    if loops > 0:
        predictions.append(SynthPrediction(
            category="functional",
            prediction="Synthesis will FAIL — combinational loop",
            confidence="high",
            detail=f"{loops} loop(s): most tools abort",
        ))

    unused = m.get("cogni.lint.unused_signal.count", 0)
    if unused > 0:
        predictions.append(SynthPrediction(
            category="area",
            prediction=f"{unused} dead signal(s) optimized away",
            confidence="high",
            detail="May indicate missing connections",
        ))

    rst = m.get("cogni.lint.async_reset_misuse.count", 0)
    if rst > 0:
        predictions.append(SynthPrediction(
            category="power",
            prediction=f"Async reset in {rst} data path(s)",
            confidence="medium",
            detail="STARC violation: glitch risk during reset",
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
    lines.append(f"  Total findings: {len(result.findings)}")
    lines.append("")

    if result.findings:
        # Categorize findings by semantic group
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
        for p in result.predictions:
            icon = {"high": "!!", "medium": "!", "low": "~"}.get(
                p.confidence, "?")
            lines.append(f"  [{icon}] [{p.category}] {p.prediction}")
            lines.append(f"       {p.detail}")
            lines.append("")

    return "\n".join(lines)
