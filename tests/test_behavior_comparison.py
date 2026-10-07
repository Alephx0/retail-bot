import pytest

from scripts.behavior_differential import samples
from scripts.compare_behavior_datasets import ks_distance, parse_ikdd


def test_ecdf_distance_handles_ties_and_rejects_missing_samples():
    assert ks_distance([1,1,2,2],[1,2]) == 0
    assert ks_distance([1,2],[3,4]) == 1
    assert ks_distance([0,1],[1,2]) == .5
    with pytest.raises(ValueError):
        ks_distance([], [1])
    with pytest.raises(ValueError):
        ks_distance([float('nan')], [1])


def test_ikdd_units_and_feature_filters_exclude_metadata():
    value = parse_ikdd(b'user demographic metadata\n65-0,80,90,501\n65-66,120,170,3001\n')
    assert value == {'hold_ms':[80,90], 'keydown_interval_ms':[120,170]}


def test_keyboard_metrics_preserve_overlap_and_exclude_cross_field_pauses():
    events = [
        {'type':'keydown','code':'KeyA','target':'first','t':0},
        {'type':'keydown','code':'KeyB','target':'first','t':50},
        {'type':'keyup','code':'KeyA','target':'first','t':80},
        {'type':'keyup','code':'KeyB','target':'first','t':110},
        {'type':'keydown','code':'KeyC','target':'second','t':5000},
        {'type':'keyup','code':'KeyC','target':'second','t':5090},
    ]
    result = samples(events)
    assert result['hold_ms'] == [80,60,90]
    assert result['keydown_interval_ms'] == [50]
    assert result['keyup_keydown_ms'] == [-30]


def test_modifier_chords_do_not_inflate_character_overlap():
    events = [
        {'type':'keydown','code':'ShiftLeft','target':'first','t':0},
        {'type':'keydown','code':'KeyA','target':'first','t':5},
        {'type':'keyup','code':'KeyA','target':'first','t':85},
        {'type':'keyup','code':'ShiftLeft','target':'first','t':90},
        {'type':'keydown','code':'KeyB','target':'first','t':160},
        {'type':'keyup','code':'KeyB','target':'first','t':235},
    ]
    result = samples(events)
    assert result['hold_ms'] == [80,75]
    assert result['modifier_hold_ms'] == [90]
    assert result['keydown_interval_ms'] == [155]
    assert result['keyup_keydown_ms'] == [75]
