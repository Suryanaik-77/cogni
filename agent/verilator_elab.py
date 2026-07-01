"""Proof-of-concept: populate elaboration facts from Verilator's XML.

This is the *optional* elaboration backend discussed in the architecture
review. It does NOT copy any Verilator code — it invokes the `verilator`
binary as a separate subprocess (`--xml-only`) and parses the emitted XML,
which is a fully-elaborated netlist: generate loops unrolled, parameters
resolved, widths computed.

The point is to show the same facts the pure-Python ElaborationModel derives
by pattern-matching come out *exactly* from Verilator when the tool is present
— accurate generate replication and widths for free — while the rest of Cogni
(its rules, its reporting) is untouched.

Fallback: if verilator is absent or the design does not elaborate, callers
keep using the pure-Python path. Nothing here is required.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class VElab:
    """Elaboration facts recovered from Verilator's XML netlist."""
    top: str = ""
    params: dict[str, int] = field(default_factory=dict)     # name -> value
    widths: dict[str, int] = field(default_factory=dict)     # signal -> bit width
    ff_bits: int = 0                                          # total registered bits
    ff_signals: dict[str, int] = field(default_factory=dict)  # reg -> width
    comparators: int = 0     # magnitude/equality compare cells (post-unroll)
    adders: int = 0          # add/sub cells (post-unroll)
    multipliers: int = 0
    always_blocks: int = 0   # sequential+comb always, after generate unroll
    ok: bool = False         # True when verilator elaborated successfully


def verilator_available() -> bool:
    return shutil.which("verilator") is not None


def _parse_vconst(name: str) -> int | None:
    """Parse a Verilator const literal like 32'sh8, 8'h1, 2'h0 -> int."""
    m = re.fullmatch(r"(\d+)'(s)?([bodh])([0-9a-fA-F]+)", name.replace("&apos;", "'"))
    if not m:
        # plain decimal fallback
        return int(name) if name.isdigit() else None
    base = {'b': 2, 'o': 8, 'd': 10, 'h': 16}[m.group(3)]
    try:
        return int(m.group(4), base)
    except ValueError:
        return None


def run_verilator_xml(files: list[str], top: str | None = None) -> str | None:
    """Run `verilator --xml-only` and return the XML text, or None on failure."""
    if not verilator_available():
        return None
    out_dir = tempfile.mkdtemp(prefix="cogni_velab_")
    cmd = ["verilator", "--xml-only", "-Wno-fatal", "-Wno-DECLFILENAME",
           "--Mdir", out_dir]
    if top:
        cmd += ["--top-module", top]
    cmd += [os.path.abspath(f) for f in files]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        xmls = [f for f in os.listdir(out_dir) if f.endswith(".xml")]
        if not xmls:
            return None
        with open(os.path.join(out_dir, xmls[0]), encoding="utf-8") as fh:
            return fh.read()
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# Verilator node tags that map to hardware operators.
_CMP_TAGS = {"lt", "gt", "lte", "gte", "eq", "neq", "gts", "lts", "gtes", "ltes"}
_ADD_TAGS = {"add", "sub"}
_MUL_TAGS = {"mul", "muls"}


def parse_verilator_xml(xml_text: str) -> VElab:
    """Parse Verilator XML netlist into elaboration facts."""
    v = VElab()
    root = ET.fromstring(xml_text)

    # dtype_id -> bit width (from the basic dtype table)
    dtype_w: dict[str, int] = {}
    for bd in root.iter("basicdtype"):
        did = bd.get("id")
        left, right = bd.get("left"), bd.get("right")
        if did is None:
            continue
        if left is not None and right is not None:
            dtype_w[did] = abs(int(left) - int(right)) + 1
        else:
            dtype_w[did] = 1

    net = root.find("netlist")
    if net is None:
        return v
    module = net.find("module")
    if module is None:
        return v
    v.top = module.get("name", "")

    # Params + signal widths (direct children of the module)
    for var in module.findall("var"):
        name = var.get("name")
        did = var.get("dtype_id")
        w = dtype_w.get(did, 1)
        if var.get("param") == "true":
            c = var.find("const")
            if c is not None:
                val = _parse_vconst(c.get("name", ""))
                if val is not None:
                    v.params[name] = val
        else:
            v.widths[name] = w

    # Registers: LHS var of any nonblocking assign (assigndly), post-unroll.
    # pwm_out written in gen_ch[0..3] is still one 4-bit var -> counted once.
    for adly in module.iter("assigndly"):
        kids = list(adly)
        if not kids:
            continue
        lhs = kids[-1]  # Verilator puts the LHS reference last
        ref = lhs if lhs.tag == "varref" else next(
            (n for n in lhs.iter("varref")), None)
        if ref is not None:
            nm = ref.get("name")
            if nm and nm in v.widths:
                v.ff_signals[nm] = v.widths[nm]
    v.ff_bits = sum(v.ff_signals.values())

    # Operators after generate unroll (count every cell node).
    for node in module.iter():
        if node.tag in _CMP_TAGS:
            v.comparators += 1
        elif node.tag in _ADD_TAGS:
            v.adders += 1
        elif node.tag in _MUL_TAGS:
            v.multipliers += 1
    v.always_blocks = sum(1 for _ in module.iter("always"))

    v.ok = True
    return v


def elaborate_with_verilator(files: list[str], top: str | None = None) -> VElab | None:
    """Full path: run verilator, parse XML. None if unavailable/failed."""
    xml = run_verilator_xml(files, top)
    if xml is None:
        return None
    try:
        return parse_verilator_xml(xml)
    except ET.ParseError:
        return None
