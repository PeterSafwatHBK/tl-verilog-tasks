#!/usr/bin/env python3
"""Split a flat SandPiper design into one Yosys module per generate-loop slice.

    python3 slice_split.py top.sv top
    python3 slice_split.py top.sv top --vec Slice_out_a0 Slice_carry_out_a0 --prefix fa

Slice i is bit i of each --vec net (TL-Verilog emits /slice$sig as Slice_sig_a0[slice]).
Every cell in the fan-in cone of those bits, stopping at the neighbouring slices'
bits, is moved into module <prefix>_i. Everything happens in --work (default yosys_split/).
"""
import argparse, json, re, subprocess, sys
from collections import Counter
from pathlib import Path

INC = re.compile(r'`include\s+"([^"]+)"')
UNPACKED = re.compile(r"^(\s*)logic\s+(\w+)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*;", re.M)


def die(msg):
    sys.exit(f"error: {msg}")


def bits(sig):
    """Real net ids only; constants show up as the strings "0", "1", "x", "z"."""
    return {b for b in sig if isinstance(b, int)}


def stage(main, extras, work, fix):
    """Copy the design and its `include files into work/ (flat layout assumed)."""
    work.mkdir(parents=True, exist_ok=True)
    todo, seen = [Path(main)] + [Path(e) for e in extras], set()
    while todo:
        p = todo.pop(0)
        if not p.exists():
            die(f"{p} not found")
        if p.resolve() in seen:
            continue
        seen.add(p.resolve())
        text = p.read_text()
        for inc in INC.findall(text):
            q = p.parent / inc
            if q.exists():
                todo.append(q)
            else:
                print(f"note: include {inc} not found next to {p.name}")
        if fix:  # "logic foo [7:0];"  ->  "logic [7:0] foo;"
            text, n = UNPACKED.subn(r"\1logic [\3:\4] \2;", text)
            if n:
                print(f"patched {n} unpacked 1-bit array declaration(s) in {p.name}")
        (work / p.name).write_text(text)


def yosys(exe, work, argv, log):
    try:
        r = subprocess.run([exe, "-q", "-l", log, *argv], cwd=work,
                           capture_output=True, text=True)
    except FileNotFoundError:
        die(f"'{exe}' not found; use --yosys /path/to/yosys")
    out = (r.stdout + r.stderr).strip()
    if r.returncode:
        die(f"yosys failed (full log: {work / log})\n{out}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sv", help="main SV file (its `include files are found beside it)")
    ap.add_argument("top", help="top module name")
    ap.add_argument("--vec", nargs="+", default=["Slice_out_a0", "Slice_carry_out_a0"],
                    help="nets whose bit i belongs to slice i")
    ap.add_argument("--prefix", default="fa", help="new modules are <prefix>_<i>")
    ap.add_argument("--extra", nargs="+", default=[],
                    help="more files to read, e.g. a pseudo_rand stub")
    ap.add_argument("--read-cmd", help='replace the read step, e.g. "plugin -i slang; read_slang top.sv"')
    ap.add_argument("--work", default="yosys_split")
    ap.add_argument("--yosys", default="yosys")
    ap.add_argument("--keep-unpacked", action="store_true",
                    help="do not pack 1-bit unpacked array declarations")
    ap.add_argument("--no-run", action="store_true", help="write split.ys but do not run it")
    a = ap.parse_args()

    work = Path(a.work)
    stage(a.sv, a.extra, work, not a.keep_unpacked)
    read = a.read_cmd or "; ".join(f"read_verilog -sv {Path(f).name}" for f in [a.sv, *a.extra])
    keep = " ".join(f"w:{v}*" for v in a.vec)
    # Same commands in both passes, so auto-generated cell names line up.
    READ = (f"{read}; hierarchy -top {a.top}; proc; splitnets; "
            f"setattr -set keep 1 {keep}; opt_clean")

    # ---- Pass 1: dump the netlist and work out which cell belongs to which slice ----
    yosys(a.yosys, work, ["-p", f"{READ}; write_json flat.json"], "dump.log")
    mods = json.loads((work / "flat.json").read_text())["modules"]
    if a.top not in mods:
        die(f"module {a.top} not in dump; found {list(mods)}")
    mod = mods[a.top]

    roots = {}                                    # slice index -> its output bits
    for vec in a.vec:
        rx = re.compile(rf"^{re.escape(vec)}\[(\d+)\]$")
        hits = [(rx.match(n), w) for n, w in mod["netnames"].items()]
        hits = [(m, w) for m, w in hits if m]
        if not hits:
            cand = [n for n in mod["netnames"] if vec.lower() in n.lower()][:10]
            die(f"no per-bit nets named {vec}[N]; nets containing '{vec}': {cand}")
        for m, w in hits:
            roots.setdefault(int(m.group(1)), set()).update(bits(w["bits"]))

    driver, fanin = {}, {}                        # bit -> driving cell; cell -> bits it reads
    for cell, info in mod["cells"].items():
        fanin[cell] = set()
        dirs = info.get("port_directions", {})    # missing for unknown modules (pseudo_rand)
        for port, sig in info["connections"].items():
            if dirs.get(port) == "output":
                driver.update({b: cell for b in bits(sig)})
            else:
                fanin[cell] |= bits(sig)

    def cone(seeds, stop):
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

    owner = {}                                    # cell -> slice index
    for i in sorted(roots):
        stop = set().union(*(roots[j] for j in roots if j != i))
        for c in cone(roots[i], stop):
            if c in owner:
                print(f"  warning: {c} is in the cones of slice {owner[c]} and slice {i}; "
                      f"keeping it in {owner[c]}")
            else:
                owner[c] = i

    # ---- Pass 2: build and run the Yosys script ----
    names = {i: f"{a.prefix}_{i}" for i in roots}
    print(f"{len(roots)} slices found")
    ys = [READ]
    for i in sorted(roots):
        cells = sorted(c for c, g in owner.items() if g == i)
        print(f"{names[i]}: {len(cells)} cells, {len(roots[i])} root bits")
        if not cells:
            continue
        bad = [c for c in cells if set(c) & set("*?[]")]
        if bad:
            print(f"  warning: glob characters in cell names, select may miss them: {bad}")
        ys += [f"select -set {names[i]} " + " ".join("c:" + c for c in cells),
               f"submod -name {names[i]} @{names[i]}"]
    left = [c for c in mod["cells"] if c not in owner]
    print(f"{len(left)} cells left in {a.top}: {' '.join(left)}")
    ys += [f"hierarchy -top {a.top}", "write_verilog -noattr split_out.v", "write_json split.json"]
    (work / "split.ys").write_text("\n".join(ys) + "\n")
    if a.no_run:
        print(f"wrote {work / 'split.ys'} (not run)")
        return

    out = yosys(a.yosys, work, ["-s", "split.ys"], "split.log")
    if out:
        print(out)

    # ---- Check the result ----
    res = json.loads((work / "split.json").read_text())["modules"]
    print("\nResult:")
    for name, m in res.items():
        ports = m.get("ports", {})
        n_in = sum(len(p["bits"]) for p in ports.values() if p["direction"] == "input")
        n_out = sum(len(p["bits"]) for p in ports.values() if p["direction"] == "output")
        kinds = dict(Counter(c["type"] for c in m["cells"].values()))
        print(f"  {name}: {n_in} in, {n_out} out, cells {kinds}")
        if name in names.values() and n_out == 0:
            print("    warning: no output ports; submod found no consumer for this slice's outputs")
    print(f"\nwrote {work / 'split_out.v'}, {work / 'split.json'}, logs in {work}/*.log")


if __name__ == "__main__":
    main()