"""
cogni.adapters.rtl.verilator.xml_facts
=======================================
Turn a Verilator ``--xml-only`` AST into rtl.* / core.* WorldModel facts.

This is a PURE function: it takes the XML text and returns (facts, tags).
It does NOT invoke Verilator and does NOT touch the filesystem — that lives
in ``perceiver.py``. Keeping the parse pure means it can be unit-tested
against a captured XML fixture with no Verilator install (see
tests/fixtures/cmd_alu.xml).

Design note — perceiver vs oracle separation: this reads only design
*structure* (modules, always blocks, FSM states, register widths, clock/
reset edges). It deliberately does NOT surface lint warnings (latches,
width mismatches) — those are the oracle's job (the "answer key"). The
perceiver describes the question; the oracle holds the answer.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# Signal-name patterns used to tell an async reset apart from a clock in a
# sensitivity list. Conservative on purpose — anything not matching is a clock.
_RESET_RE = re.compile(r"(rst|reset|rstn|rst_n|aresetn|nreset)", re.IGNORECASE)


def _dtype_width(dtype_id: str | None, dtypes: dict) -> int:
    """Resolve a dtype id to a bit width, following enum/ref dtypes down to
    their base. Scalar logic/bit (no left/right range) is width 1."""
    seen: set[str] = set()
    while dtype_id is not None and dtype_id not in seen:
        seen.add(dtype_id)
        node = dtypes.get(dtype_id)
        if node is None:
            return 1
        if node.tag == "basicdtype":
            left, right = node.get("left"), node.get("right")
            if left is not None and right is not None:
                return abs(int(left) - int(right)) + 1
            return 1
        # enumdtype / refdtype -> follow the underlying type.
        dtype_id = node.get("sub_dtype_id")
    return 1


def _first_varref(elem) -> ET.Element | None:
    """Depth-first search for the first <varref> under an element."""
    if elem is None:
        return None
    if elem.tag == "varref":
        return elem
    for child in elem:
        found = _first_varref(child)
        if found is not None:
            return found
    return None


def _is_sequential(always) -> list:
    """Return the list of edge-triggered senitems if `always` is clocked,
    else []. A combinational always_comb has no edge sensitivity."""
    sentree = always.find("sentree")
    if sentree is None:
        return []
    return [si for si in sentree.iter("senitem") if si.get("edgeType")]


def facts_from_xml(
    xml_text: str,
    *,
    source: str,
    lines_of_code: int | None = None,
    code_origin: str = "unknown",
    author_intent: str | None = None,
) -> tuple[dict, list[str]]:
    """Parse a Verilator XML AST into (facts, tags).

    `code_origin` / `author_intent` are human-supplied context that cannot
    be derived from the AST; they are passed through verbatim (default
    "unknown" — we do not guess).
    """
    root = ET.fromstring(xml_text)

    # --- dtype table: id -> element (for width resolution) ---
    dtypes: dict = {}
    for tt in root.iter("typetable"):
        for child in tt:
            did = child.get("id")
            if did is not None:
                dtypes[did] = child

    # --- memory arrays: dtype ids and the var names that use them. These
    # are storage/RAM, reported separately and excluded from flop counts. ---
    array_dtype_ids = {
        n.get("id") for n in dtypes.values()
        if n.tag in ("unpackarraydtype", "packarraydtype")
    }
    mem_var_names = {
        v.get("name") for v in root.iter("var")
        if v.get("dtype_id") in array_dtype_ids
    }

    # --- top module name ---
    modules = list(root.iter("module"))
    top_name = modules[0].get("name") if modules else None

    # --- parameters (var with param="true") ---
    params = [v.get("name") for v in root.iter("var") if v.get("param") == "true"]

    # --- always blocks: classify comb vs seq; harvest clock/reset edges ---
    n_comb = n_seq = 0
    clocks: set[str] = set()
    resets: dict[str, str] = {}        # reset signal -> edge polarity
    register_widths: dict[str, int] = {}

    for always in root.iter("always"):
        edges = _is_sequential(always)
        if not edges:
            n_comb += 1
            continue
        n_seq += 1
        # Sensitivity list -> clocks vs async resets.
        for si in edges:
            vr = si.find("varref")
            name = vr.get("name") if vr is not None else None
            if name is None:
                continue
            if _RESET_RE.search(name):
                resets[name] = si.get("edgeType")    # POS / NEG
            else:
                clocks.add(name)
        # Registers = LHS of every (blocking or non-blocking) assign in the
        # clocked block. LHS is the last child of the assign node.
        for node in list(always.iter("assign")) + list(always.iter("assigndly")):
            kids = list(node)
            if not kids:
                continue
            vr = _first_varref(kids[-1])
            if vr is None:
                continue
            name = vr.get("name")
            if name in mem_var_names:
                continue   # storage/RAM — counted in rtl.memory_arrays, not flops
            register_widths[name] = _dtype_width(vr.get("dtype_id"), dtypes)

    register_bits = sum(register_widths.values())

    # --- reset strategy from async reset edges (else sync/none) ---
    if resets:
        pol = next(iter(resets.values()))
        reset_strategy = "async_low" if pol == "NEG" else "async_high"
    else:
        reset_strategy = "none"

    # --- case statements ---
    n_case = len(list(root.iter("case")))

    # --- FSMs: enum dtypes and their state counts ---
    enumdtypes = [n for n in dtypes.values() if n.tag == "enumdtype"]
    n_fsms = len(enumdtypes)
    state_count = max(
        (len(list(e.iter("enumitem"))) for e in enumdtypes), default=0
    )

    n_memarrays = len(mem_var_names)

    # --- max operator bit-width (datapath width signal for width rules) ---
    _OPS = {"add", "sub", "mul", "div", "moddiv", "and", "or", "xor",
            "shiftl", "shiftr", "shiftrs", "muls", "divs", "moddivs"}
    op_widths = [
        _dtype_width(n.get("dtype_id"), dtypes)
        for tag in _OPS for n in root.iter(tag)
    ]
    operator_max_bitwidth = max(op_widths, default=0)

    # --- CDC paths: single (or zero) clock domain => no crossings ---
    cdc_paths = 0 if len(clocks) <= 1 else None

    facts: dict = {
        "rtl.module.top": top_name,
        "rtl.module.parameters": params,
        "rtl.always_blocks": n_comb + n_seq,
        "rtl.always_comb_blocks": n_comb,
        "rtl.always_ff_blocks": n_seq,
        "rtl.case_blocks": n_case,
        "rtl.fsms": n_fsms,
        "rtl.fsm.state_count": state_count,
        "rtl.fsm.encoding_declared": n_fsms > 0,
        "rtl.clock_domains": len(clocks),
        "rtl.reset_domains": len(resets),
        "rtl.reset_strategy": reset_strategy,
        "rtl.async_controls": len(resets),
        "rtl.register_bits": register_bits,
        "rtl.memory_arrays": n_memarrays,
        "rtl.operator_max_bitwidth": operator_max_bitwidth,
        "core.code_origin": code_origin,
        "target.stage": "rtl",
        "target.tool": "verilator",
    }
    if cdc_paths is not None:
        facts["rtl.cdc_paths"] = cdc_paths
    if lines_of_code is not None:
        facts["core.lines_of_code"] = lines_of_code
    if author_intent is not None:
        facts["core.author_intent"] = author_intent

    # --- tags (recall keys for the rule engine) ---
    tags = ["rtl_stage"]
    if n_fsms > 0:
        tags.append("fsm_present")
    if len(clocks) == 1:
        tags.append("single_clock")
    if len(resets) == 1:
        tags.append("single_reset_domain")
    if lines_of_code is not None and lines_of_code < 300:
        tags.append("small_module")
    if code_origin == "human":
        tags.append("human_authored")

    return facts, tags
