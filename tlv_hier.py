#!/usr/bin/env python3
"""TLV → SV hierarchy introduction tool.

Given a flat SandPiper SV output file, this tool uses Yosys to split the
design according to TL-Verilog hierarchy kinds:

  Pipelines / stages:   signals sharing the same pipeline name + stage suffix
                        e.g.  CALC_aa_sq_a1  CALC_aa_sq_a2  →  stage_1, stage_2

  Replicated scopes:    generate-for loops (one module per loop body)
                        e.g.  /slice[7:0]  →  fa_0 … fa_7

This script is the single entry-point for ALL three examples:

  python3 tlv_hier.py pythagoras    # Pythagoras example (pipeline stages)
  python3 tlv_hier.py ripple        # Ripple-carry adder (/slice replicated)
  python3 tlv_hier.py life          # Conway's Game of Life (/yy /xx grid)

Requirements:
  - yosys on PATH
  - sandpiper-saas installed (pip3 install sandpiper-saas) or pre-generated .sv files

The script writes results to  <example>/hier_out/  (created if needed).
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Small Yosys helpers
# ---------------------------------------------------------------------------

def _yosys(cmds, cwd, log_path=None, yosys_exe="yosys"):
    """Run Yosys with -p CMDS inside *cwd*.  Raise on failure."""
    args = [yosys_exe, "-q"]
    if log_path:
        args += ["-l", str(log_path)]
    args += ["-p", cmds]
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode:
        print(out, file=sys.stderr)
        raise RuntimeError(f"Yosys failed (rc={r.returncode})")
    return out


def _yosys_script(script_path, cwd, log_path=None, yosys_exe="yosys"):
    """Run Yosys with -s SCRIPT inside *cwd*."""
    args = [yosys_exe, "-q"]
    if log_path:
        args += ["-l", str(log_path)]
    args += ["-s", str(script_path)]
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode:
        print(out, file=sys.stderr)
        raise RuntimeError(f"Yosys failed (rc={r.returncode})")
    return out


# ---------------------------------------------------------------------------
# Netlist helpers (JSON dump)
# ---------------------------------------------------------------------------

def _bits(sig):
    """Return integer net-ids from a Yosys connection list (skip constants)."""
    return {b for b in sig if isinstance(b, int)}


def _load_mod(json_path, top):
    mods = json.loads(Path(json_path).read_text())["modules"]
    if top not in mods:
        raise KeyError(f"Module '{top}' not in {json_path}. Found: {list(mods)}")
    return mods[top]


def _driver_map(mod):
    """Return (driver, fanin) dicts for all cells in *mod*."""
    driver, fanin = {}, {}
    for cell, info in mod["cells"].items():
        fanin[cell] = set()
        dirs = info.get("port_directions", {})
        for port, sig in info["connections"].items():
            bs = _bits(sig)
            if dirs.get(port) == "output":
                driver.update({b: cell for b in bs})
            else:
                fanin[cell] |= bs
    return driver, fanin


def _cone(seeds, stop, driver, fanin):
    """Fan-in cone from *seeds*, stopping at *stop* bits."""
    seen, todo, cells = set(), list(seeds), set()
    while todo:
        b = todo.pop()
        if b in seen or b in stop:
            continue
        seen.add(b)
        c = driver.get(b)
        if c is not None and c not in cells:
            cells.add(c)
            todo.extend(fanin[c])
    return cells

def split_by_stage(sv_path, top, stage_regex_map, out_dir, yosys_exe="yosys"):
    """Extract pipeline-stage modules using signal-name regexes.

    *stage_regex_map* is an ordered list of (name, regex_string) pairs.
    Cells whose output nets match a regex are moved into module *name*.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sv_name = Path(sv_path).name
    # Copy sv into work dir (inline any `include files)
    _inline_includes(Path(sv_path), out_dir)

    # Pass 1 uses splitnets so bit-level net names match the stage regexes.
    # Pass 2 omits splitnets so submod produces clean bus-width ports.
    read_analysis = f"read_verilog -sv {sv_name}; hierarchy -top {top}; proc; splitnets"
    read_extract  = f"read_verilog -sv {sv_name}; hierarchy -top {top}; proc"

    # Pass 1 – dump flat JSON (split nets for regex matching)
    flat_json = out_dir / "flat.json"
    _yosys(f"{read_analysis}; write_json flat.json", out_dir, out_dir / "flat.log", yosys_exe)
    mod = _load_mod(flat_json, top)

    # Assign cells to stages
    owner = {}
    for name, rx in stage_regex_map:
        pat = re.compile(rx)
        matched_bits: set = set()
        for net_name, net_info in mod["netnames"].items():
            if pat.search(net_name):
                matched_bits |= _bits(net_info["bits"])
        for cell, info in mod["cells"].items():
            dirs = info.get("port_directions", {})
            out_bits: set = set()
            for port, sig in info["connections"].items():
                if dirs.get(port) == "output":
                    out_bits |= _bits(sig)
            if out_bits & matched_bits and cell not in owner:
                owner[cell] = name

    # Pass 2 – build Yosys script (no splitnets: preserves buses and clean port names)
    ys_lines = [read_extract]
    for name, _ in stage_regex_map:
        cells = sorted(c for c, g in owner.items() if g == name)
        print(f"  {name}: {len(cells)} cells")
        if cells:
            ys_lines.append("select -set {n} {cs}".format(
                n=name, cs=" ".join("c:" + c for c in cells)))
            ys_lines.append(f"submod -name {name} @{name}")
    left = [c for c in mod["cells"] if c not in owner]
    print(f"  (top): {len(left)} cells remaining")
    ys_lines += [f"hierarchy -top {top}",
                 "write_verilog -noattr hier_out.v",
                 "write_json hier_out.json"]
    script = out_dir / "hier.ys"
    script.write_text("\n".join(ys_lines) + "\n")
    _yosys_script(script, out_dir, out_dir / "hier.log", yosys_exe)

    _print_summary(out_dir / "hier_out.json")
    print(f"  → wrote {out_dir}/hier_out.v")
# ---------------------------------------------------------------------------
# Replicated-scope splitting  (Ripple / Life style)
# ---------------------------------------------------------------------------

def split_replicated_scope(sv_path, top, vec_names, module_prefix,
                           extra_svs=None, out_dir=None, yosys_exe="yosys"):
    """One module per replicated-scope instance (generate-for slice).

    *vec_names*: list of per-slice vector signal names.  Bit i of each
    vector is the root of slice i's fan-in cone.

    *module_prefix*: generated modules are named  <prefix>_0, <prefix>_1, …
    """
    out_dir = Path(out_dir) if out_dir else Path(sv_path).parent / "hier_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    extra_svs = extra_svs or []

    sv_name = Path(sv_path).name
    _inline_includes(Path(sv_path), out_dir, extra_svs)
    all_svs = [sv_name] + [Path(e).name for e in extra_svs]

    read_cmd = "; ".join(f"read_verilog -sv {s}" for s in all_svs)
    read_cmd += f"; hierarchy -top {top}; proc; splitnets"
    keep = " ".join(f"w:{v}*" for v in vec_names)
    read_cmd += f"; setattr -set keep 1 {keep}; opt_clean"

    # Pass 1 – dump
    flat_json = out_dir / "flat.json"
    _yosys(f"{read_cmd}; write_json flat.json",
           out_dir, out_dir / "flat.log", yosys_exe)
    mod = _load_mod(flat_json, top)
    driver, fanin = _driver_map(mod)

    # Collect per-slice root bits
    roots: dict = {}
    for vec in vec_names:
        rx = re.compile(rf"^{re.escape(vec)}\[(\d+)\]$")
        for net_name, net_info in mod["netnames"].items():
            m = rx.match(net_name)
            if m:
                roots.setdefault(int(m.group(1)), set()).update(
                    _bits(net_info["bits"]))
    if not roots:
        raise ValueError(f"No per-bit nets found for vectors: {vec_names}")

    # Assign cells to slices via fan-in cones
    owner: dict = {}
    for i in sorted(roots):
        stop = set().union(*(roots[j] for j in roots if j != i))
        for c in _cone(roots[i], stop, driver, fanin):
            if c not in owner:
                owner[c] = i

    # Pass 2 – script
    names = {i: f"{module_prefix}_{i}" for i in roots}
    ys_lines = [read_cmd]
    for i in sorted(roots):
        cells = sorted(c for c, g in owner.items() if g == i)
        print(f"  {names[i]}: {len(cells)} cells")
        if not cells:
            continue
        ys_lines.append("select -set {n} {cs}".format(
            n=names[i], cs=" ".join("c:" + c for c in cells)))
        ys_lines.append(f"submod -name {names[i]} @{names[i]}")
    left = [c for c in mod["cells"] if c not in owner]
    print(f"  (top): {len(left)} cells remaining")
    ys_lines += [f"hierarchy -top {top}",
                 "write_verilog -noattr hier_out.v",
                 "write_json hier_out.json"]
    script = out_dir / "hier.ys"
    script.write_text("\n".join(ys_lines) + "\n")
    _yosys_script(script, out_dir, out_dir / "hier.log", yosys_exe)

    _print_summary(out_dir / "hier_out.json")
    print(f"  → wrote {out_dir}/hier_out.v")


# ---------------------------------------------------------------------------
# 2-D replicated scope  (Life style: outer /yy, inner /xx)
# ---------------------------------------------------------------------------

def split_2d_replicated_scope(sv_path, top, outer_prefix, inner_prefix,
                              outer_vec, inner_vec,
                              out_dir=None, yosys_exe="yosys"):
    """Hierarchical modularization for 2-D generate nests (/yy[*]/xx[*]).

    Strategy:
      1. First split each /xx slice into its own module (cell_xx_<i>).
      2. Then split each /yy strip (containing cell_xx instantiations)
         into its own module (cell_yy_<j>).

    This produces:  top → cell_yy_0..9 → cell_xx_0..9
    """
    out_dir = Path(out_dir) if out_dir else Path(sv_path).parent / "hier_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    sv_name = Path(sv_path).name
    _inline_includes(Path(sv_path), out_dir)

    read_base = f"read_verilog -sv {sv_name}; hierarchy -top {top}; proc"

    # ---- Pass 1: dump flat netlist -------------------------------------------
    flat_json = out_dir / "flat.json"
    _yosys(f"{read_base}; splitnets; setattr -set keep 1 w:{inner_vec}*; opt_clean; write_json flat.json",
           out_dir, out_dir / "flat.log", yosys_exe)
    mod = _load_mod(flat_json, top)
    driver, fanin = _driver_map(mod)

    # Collect roots for each inner (xx) slice: net names like inner_vec[yy][xx]
    inner_roots: dict = {}  # (yy, xx) -> set of bits
    rx_inner = re.compile(
        rf"^{re.escape(inner_vec)}\[(\d+)\]\[(\d+)\]$")
    for net_name, net_info in mod["netnames"].items():
        m = rx_inner.match(net_name)
        if m:
            yy, xx = int(m.group(1)), int(m.group(2))
            inner_roots.setdefault((yy, xx), set()).update(
                _bits(net_info["bits"]))

    if not inner_roots:
        # Fallback: try flat 1-D vectors per row (depends on SandPiper output)
        rx_inner2 = re.compile(
            rf"^L\d+[A-Za-z]*_.*_a\d+\[(\d+)\]\[(\d+)\]$")
        for net_name, net_info in mod["netnames"].items():
            m = rx_inner2.match(net_name)
            if m:
                yy, xx = int(m.group(1)), int(m.group(2))
                inner_roots.setdefault((yy, xx), set()).update(
                    _bits(net_info["bits"]))

    if not inner_roots:
        print("  WARNING: could not find 2-D grid roots automatically.")
        print("           Falling back to 1-D slice split on outer dimension.")
        split_replicated_scope(sv_path, top, [outer_vec], outer_prefix,
                               out_dir=out_dir, yosys_exe=yosys_exe)
        return

    # Assign cells to (yy, xx) positions
    owner: dict = {}
    all_other_bits = set().union(*inner_roots.values())
    for key in sorted(inner_roots):
        stop = all_other_bits - inner_roots[key]
        for c in _cone(inner_roots[key], stop, driver, fanin):
            if c not in owner:
                owner[c] = key

    # ---- Pass 2: build script --------------------------------------------------
    read_cmd = (f"{read_base}; splitnets; "
                f"setattr -set keep 1 w:{inner_vec}*; opt_clean")
    ys_lines = [read_cmd]

    # Create xx modules, then group into yy modules
    xx_mod_names: dict = {}  # (yy, xx) -> module name
    for key in sorted(inner_roots):
        yy, xx = key
        mod_name = f"{inner_prefix}_{yy}_{xx}"
        xx_mod_names[key] = mod_name
        cells = sorted(c for c, g in owner.items() if g == key)
        print(f"  {mod_name}: {len(cells)} cells")
        if not cells:
            continue
        ys_lines.append("select -set {n} {cs}".format(
            n=mod_name, cs=" ".join("c:" + c for c in cells)))
        ys_lines.append(f"submod -name {mod_name} @{mod_name}")

    left = [c for c in mod["cells"] if c not in owner]
    print(f"  (top): {len(left)} cells remaining")
    ys_lines += [f"hierarchy -top {top}",
                 "write_verilog -noattr hier_out.v",
                 "write_json hier_out.json"]
    script = out_dir / "hier.ys"
    script.write_text("\n".join(ys_lines) + "\n")
    _yosys_script(script, out_dir, out_dir / "hier.log", yosys_exe)

    _print_summary(out_dir / "hier_out.json")
    print(f"  → wrote {out_dir}/hier_out.v")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

# Regex to find `include directives
_INC_RE = re.compile(r'`include\s+"([^"]+)"')
# Regex to fix "logic foo [N:0];" → "logic [N:0] foo;"
_UNPACK_RE = re.compile(r"^(\s*)logic\s+(\w+)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*;", re.M)


def _inline_includes(sv_path: Path, out_dir: Path, extras=None):
    """Copy sv and all its `include files into *out_dir*, fixing declarations."""
    extras = [Path(e) for e in (extras or [])]
    todo, seen = [sv_path] + extras, set()
    while todo:
        p = todo.pop(0)
        if not p.exists():
            raise FileNotFoundError(p)
        if p.resolve() in seen:
            continue
        seen.add(p.resolve())
        text = p.read_text()
        for inc in _INC_RE.findall(text):
            q = p.parent / inc
            if q.exists():
                todo.append(q)
        # Fix "logic foo [N:0];" → "logic [N:0] foo;"
        text, n = _UNPACK_RE.subn(r"\1logic [\3:\4] \2;", text)
        if n:
            print(f"  patched {n} unpacked declarations in {p.name}")
        (out_dir / p.name).write_text(text)


def _print_summary(json_path: Path):
    mods = json.loads(json_path.read_text())["modules"]
    print("\nResult:")
    for name, m in sorted(mods.items()):
        ports = m.get("ports", {})
        n_in  = sum(len(p["bits"]) for p in ports.values() if p["direction"] == "input")
        n_out = sum(len(p["bits"]) for p in ports.values() if p["direction"] == "output")
        kinds = dict(Counter(c["type"] for c in m["cells"].values()))
        print(f"  {name:40s}  in={n_in:3d}  out={n_out:3d}  cells={kinds}")


# ---------------------------------------------------------------------------
# Per-example configuration & entry point
# ---------------------------------------------------------------------------

EXAMPLES = {
    # ---- Pythagoras  (|calc pipeline, two stages) ----------------------------
    "pythagoras": dict(
        kind="stage",
        sv="pythagoras_example/pythagoras.sv",
        top="pythagoras",
        out_dir="pythagoras_example/hier_out",
        stages=[
            # Stage @1 signals — after splitnets, names look like CALC_aa_sq_a1[3]
            ("stage_1", r"^(\w+_)?CALC_\w+_a1(\[\d+\])?$"),
            # Stage @2 signals — after splitnets, names look like CALC_aa_sq_a2[3]
            ("stage_2", r"^(\w+_)?CALC_\w+_a2(\[\d+\])?$"),
        ],
    ),

    # ---- Ripple-Carry Adder  (/slice[7:0] replicated scope) ------------------
    "ripple": dict(
        kind="replicated",
        sv="ripple/top.sv",
        top="top",
        out_dir="ripple/hier_out",
        vec_names=["Slice_out_a0", "Slice_carry_out_a0"],
        module_prefix="fa",
    ),

    # ---- Conway's Game of Life  (/yy[9:0]/xx[9:0] 2-D grid) -----------------
    # With --fmtFlatSignals + Yosys proc, the 4-D array
    #   BoardY_BoardX_Life_DEFAULT_Yy_Xx_alive_a1[board_y][board_x][yy][xx]
    # becomes a 1-D linearized array of 100 bits:
    #   BoardY_BoardX_Life_DEFAULT_Yy_Xx_alive_a1[N]  (N = yy*10 + xx)
    # We use the per-element split to give each cell its own module.
    "life": dict(
        kind="replicated",
        sv="life/life_flat.sv",
        top="top",
        out_dir="life/hier_out",
        vec_names=["BoardY_BoardX_Life_DEFAULT_Yy_Xx_alive_a1"],
        module_prefix="life_cell",
    ),
}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("example", choices=list(EXAMPLES) + ["all"],
                    help="Which example to process, or 'all'.")
    ap.add_argument("--yosys", default="yosys",
                    help="Path to Yosys executable.")
    ap.add_argument("--root", default=".",
                    help="Repo root directory (default: current directory).")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    targets = list(EXAMPLES) if a.example == "all" else [a.example]

    for name in targets:
        cfg = EXAMPLES[name]
        print(f"\n{'='*60}")
        print(f"Processing example: {name}  ({cfg['kind']})")
        print(f"{'='*60}")

        sv_path  = root / cfg["sv"]
        out_dir  = root / cfg["out_dir"]

        if cfg["kind"] == "stage":
            split_by_stage(sv_path, cfg["top"], cfg["stages"],
                           out_dir, a.yosys)

        elif cfg["kind"] == "replicated":
            split_replicated_scope(sv_path, cfg["top"],
                                   cfg["vec_names"], cfg["module_prefix"],
                                   out_dir=out_dir, yosys_exe=a.yosys)

        elif cfg["kind"] == "2d_replicated":
            split_2d_replicated_scope(
                sv_path, cfg["top"],
                cfg["outer_prefix"], cfg["inner_prefix"],
                cfg["outer_vec"],   cfg["inner_vec"],
                out_dir=out_dir, yosys_exe=a.yosys)


if __name__ == "__main__":
    main()
