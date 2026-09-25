import re

from add_submod_tags import derive_module_name, extract_stage_signal, find_generate_for_blocks


def test_generate_block_discovery():
    text = open('ripple_adder.sv').read()
    blocks = find_generate_for_blocks(text)
    labels = [b['label'] for b in blocks]
    assert 'L1_Slice' in labels
    assert 'L1b_Slice' in labels


def test_module_name_derivation():
    assert derive_module_name('L1_Slice') == 'Slice'
    assert derive_module_name('L1b_Slice') == 'Slice'


def test_flat_signal_extraction():
    assert extract_stage_signal('Slice_carry_in_a0') == ('Slice', 'carry_in')
    assert extract_stage_signal('L0_result_a0') == ('L0', 'result')
    assert extract_stage_signal('pipe_a') == ('pipe', 'a')
