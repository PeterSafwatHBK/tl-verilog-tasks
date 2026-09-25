#!/usr/bin/env python3
r"""Split a flat SandPiper-generated SV design into modules, using REGEXES on signal names.

Usage:
    python3 regex_split.py DESIGN.sv TOP_MODULE NAME=REGEX [NAME=REGEX ...]

Example:
    python3 regex_split.py pythagoras.sv pythagoras \
        'stage1=^(\w+_)?CALC_(\w)*_a1$' 'stage2=^(\w+_)?CALC_(\w)*_a2$'

For each NAME=REGEX: find every wire whose name matches REGEX, collect the cells
that drive those wires, and move those cells into a new module called NAME.
Aliases are handled: a wire named CALC_x_a2 is found even if Yosys renamed it
to a port name, because aliased wires share the same net bits in the JSON dump.
"""
import json, re, subprocess, sys

if len(sys.argv) < 4:
    sys.exit(__doc__)
sv, top = sys.argv[1], sys.argv[2]
groups = [g.split("=", 1) for g in sys.argv[3:]]
READ = f"read_verilog -sv {sv}; hierarchy -top {top}; proc"

# Pass 1: dump the design WITH all wire names (aliases included).
subprocess.run(["yosys", "-q", "-p", f"{READ}; write_json flat.json"], check=True)
mod = json.load(open("flat.json"))["modules"][top]

def net_bits(bits):            # keep real net ids, skip constants ("0", "1", "x")
    return {b for b in bits if isinstance(b, int)}

# Which cells drive the wires each regex matches?
owner = {}
for name, rx in groups:
    pat = re.compile(rx)
    matched = [n for n in mod["netnames"] if pat.search(n)]
    bits = set()
    for n in matched:
        bits |= net_bits(mod["netnames"][n]["bits"])
    print(f"{name}: {len(matched)} wires match")
    for cell, info in mod["cells"].items():
        outs = set()
        for port, direction in info["port_directions"].items():
            if direction == "output":
                outs |= net_bits(info["connections"][port])
        if outs & bits:
            if cell in owner:
                print(f"  warning: {cell} already claimed by {owner[cell]}")
            else:
                owner[cell] = name

# Pass 2: build and run the Yosys script.
ys = [READ]
for name, _ in groups:
    cells = [c for c, g in owner.items() if g == name]
    print(f"{name}: {len(cells)} cells")
    if cells:
        print(f"select -set {name} " + " ".join("c:" + c for c in cells))
        ys.append(f"select -set {name} " + " ".join("c:" + c for c in cells))
        ys.append(f"submod -name {name} @{name}")
left = [c for c in mod["cells"] if c not in owner]
print(f"{len(left)} cells left in {top}")
ys += [f"hierarchy -top {top}", "ls", "write_verilog -noattr split_out.v"]
open("regex_split.ys", "w").write("\n".join(ys) + "\n")
subprocess.run(["yosys", "-q", "-s", "regex_split.ys"], check=True)
print("wrote split_out.v")