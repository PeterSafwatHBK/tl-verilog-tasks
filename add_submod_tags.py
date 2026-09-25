#!/usr/bin/env python3
"""Generic SandPiper submodule discovery.

This script discovers generate-for blocks structurally (for (...) begin : LABEL)
and groups flattened stage/pipeline signals using SandPiper's naming convention,
without hardcoding a particular design name or a one-off regex.
"""

import argparse
import re
import sys
from collections import defaultdict

GEN_FOR_RE = re.compile(
    r"for\s*\([^;]*;[^;]*;[^)]*\)\s*begin\s*:\s*(?P<label>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE | re.DOTALL,
)


def find_generate_for_blocks(text):
    """Return every generate-for block as a dict with label/body."""
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.search(r'\bfor\s*\([^)]*\)\s*begin\s*:\s*(?P<label>[A-Za-z_][A-Za-z0-9_]*)', line, re.IGNORECASE)
        if not match:
            i += 1
            continue

        label = match.group('label')
        depth = 1
        j = i + 1
        body_lines = [line]
        while j < len(lines):
            cleaned = re.sub(r'//.*$', '', lines[j])
            cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)
            depth += len(re.findall(r'\bbegin\b', cleaned, flags=re.IGNORECASE))
            depth -= len(re.findall(r'\bend\b', cleaned, flags=re.IGNORECASE))
            body_lines.append(lines[j])
            if depth == 0:
                blocks.append({"label": label, "body": "\n".join(body_lines)})
                i = j + 1
                break
            j += 1
        else:
            blocks.append({"label": label, "body": "\n".join(body_lines)})
            i += 1
    return blocks


def derive_module_name(label):
    """Infer a plausible module name from a generate block label.

    Examples:
        L1_Slice  -> Slice
        L1b_Slice -> Slice
    """
    s = label.strip()
    s = re.sub(r'^(?:genblk\d+|blk\d+|L\d+[A-Za-z]?|G\d+)_?', '', s)
    s = s.strip('_')
    if not s:
        return label
    if '_' in s:
        candidate = s.rsplit('_', 1)[-1]
        return candidate if candidate else s
    return s


def extract_stage_signal(signal_name):
    """Split a flattened signal into (stage_id, signal_base).

    SandPiper's flattened signal convention is effectively:
        <stage>_<signal-base>[_suffix]

    where the stage is the leading token, the signal base is the remainder of the
    name, and a trailing identifier such as _a0, _a1, ... is stripped as the
    generated index/array qualifier.
    """
    token = signal_name.strip()
    if not token or '_' not in token:
        return None

    parts = token.split('_')
    if len(parts) < 2:
        return None

    stage = parts[0]
    base_parts = parts[1:]
    while base_parts and re.fullmatch(r'[A-Za-z]+\d+|\d+', base_parts[-1]):
        base_parts.pop()
    if not base_parts:
        return None

    base = '_'.join(base_parts)
    if not stage or not base:
        return None
    return stage, base


def group_flat_signals_by_stage(text):
    """Group all flattened stage/pipeline signals by stage identifier."""
    cleaned = re.sub(r'//.*', '', text)
    cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'`[A-Za-z_][A-Za-z0-9_]*', '', cleaned)

    signal_names = set()
    signal_name_re = r'(?P<name>[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+(?:_[A-Za-z0-9]+)*)'
    for pattern in [
        rf'\b(?:logic|wire|reg)\s+(?:\[[^\]]+\]\s*)?{signal_name_re}\b',
        rf'\bassign\s+{signal_name_re}\b',
        rf'\b[a-zA-Z_][A-Za-z0-9_]*\s*\[[^\]]+\]\s*=?\s*{signal_name_re}\b',
    ]:
        signal_names.update(re.findall(pattern, cleaned, flags=re.IGNORECASE))

    groups = defaultdict(list)
    for token in signal_names:
        parsed = extract_stage_signal(token)
        if not parsed:
            continue
        stage, base = parsed
        if base not in groups[stage]:
            groups[stage].append(base)
    return dict(groups)


def generate_yosys_submod_script(path, module_name):
    """Return a Yosys script tailored to the discovered module name."""
    return "\n".join([
        f"read_verilog -sv {path}",
        "hierarchy -top top",
        "proc",
        "submod",
        f"select {module_name}",
        "dump",
    ])


def merge_into_module(main_path, tagged_gen_lines, output_path):
    """Backward-compatible merge helper retained for older workflows."""
    with open(main_path, 'r') as f:
        main_lines = f.readlines()

    output_lines = []
    injected = False
    for line in main_lines:
        if re.match(r'\s*`include\s+.*_gen\.sv', line):
            if not injected:
                output_lines.append('// [Merged from _gen.sv with submod tags]\n')
                output_lines.extend(tagged_gen_lines)
                output_lines.append('// [End of merged _gen.sv]\n')
                injected = True
        else:
            output_lines.append(line)

    if not injected:
        output_lines2 = []
        for line in output_lines:
            output_lines2.append(line)
            if re.match(r'\s*module\s+\w+', line) and not injected:
                output_lines2.append('// [Merged from _gen.sv with submod tags]\n')
                output_lines2.extend(tagged_gen_lines)
                output_lines2.append('// [End of merged _gen.sv]\n')
                injected = True
        output_lines = output_lines2

    with open(output_path, 'w') as f:
        f.writelines(output_lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*', help='Input files: design.sv or design_gen.sv design.sv output.sv')
    parser.add_argument('--discover', action='store_true', help='Discover generate blocks and flat signals in a single SV/TLV file.')
    args = parser.parse_args(argv)

    if args.discover:
        if len(args.paths) != 1:
            parser.error('Expected exactly one file when using --discover.')
        with open(args.paths[0], 'r') as f:
            text = f.read()
        blocks = find_generate_for_blocks(text)
        print('Generate-for blocks:')
        for block in blocks:
            label = block['label']
            print(f'  - {label} -> {derive_module_name(label)}')
        groups = group_flat_signals_by_stage(text)
        print('\nFlat stage groups:')
        if groups:
            for stage, bases in sorted(groups.items()):
                print(f'  - {stage}: {bases}')
        else:
            print('  (none found)')
        return 0

    if len(args.paths) != 3:
        print('Usage: python3 add_submod_tags.py design_gen.sv design.sv output.sv')
        print('   or: python3 add_submod_tags.py --discover design.sv')
        return 1

    gen_path, main_path, output_path = args.paths
    try:
        with open(gen_path, 'r') as f:
            gen_text = f.read()
        blocks = find_generate_for_blocks(gen_text)
        print(f'Found generate blocks: {[b["label"] for b in blocks]}')
        groups = group_flat_signals_by_stage(gen_text)
        print(f'Found stage groups: {sorted(groups.keys())}')
        tagged_lines = []
        seen = set()
        for block in blocks:
            label = block['label']
            module_name = derive_module_name(label)
            if module_name not in seen:
                seen.add(module_name)
                tagged_lines.append(f'// Auto-discovered submod: {module_name}\n')
        merge_into_module(main_path, tagged_lines, output_path)
        print(f'Wrote merged file: {output_path}')
        return 0
    except FileNotFoundError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())