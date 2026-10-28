"""Known-answer tests for independently recovered vendor header components."""
import json

import pytest

from shopee_affiliate.signature_components import cached_degraded_token, dat_from_samples

KEY = 'shopee_webUnique_ccd'


@pytest.mark.parametrize(('session', 'local', 'expected'), [
    ({}, {}, ''),
    ({KEY: json.dumps('SESSION')}, {}, 'SESSION'),
    ({}, {KEY: json.dumps('LOCAL')}, 'LOCAL'),
    ({KEY: json.dumps('SESSION')}, {KEY: json.dumps('LOCAL')}, 'SESSION'),
    ({KEY: 'invalid JSON'}, {KEY: json.dumps('LOCAL')}, 'LOCAL'),
    ({KEY: json.dumps('')}, {KEY: json.dumps('LOCAL')}, 'LOCAL'),
])
def test_cached_branch_matches_original_sdk(session, local, expected):
    # These cases were run through the original token SDK with synthetic values.
    assert cached_degraded_token(session, local) == expected


def test_dat_known_answer():
    assert dat_from_samples([0, 1/256, 0.5, 255/256, 0.999999, 0.25, 0.75, 1/16]) == '000180ffff40c010'


@pytest.mark.parametrize('samples', [[], [0] * 7, [0] * 9,
    [0] * 7 + [1], [0] * 7 + [-0.01], [0] * 7 + [float('nan')],
    [0] * 7 + [float('inf')], [0] * 7 + [True], [0] * 7 + ['0']])
def test_dat_invalid_input(samples):
    with pytest.raises(ValueError):
        dat_from_samples(samples)


def test_dat_does_not_mutate_input():
    samples = [0.5] * 8
    assert dat_from_samples(samples) == '80' * 8
    assert samples == [0.5] * 8


@pytest.mark.parametrize(('value', 'expected'), [
    (False, 'LOCAL'), (0, 'LOCAL'), (None, 'LOCAL'),
    (1, ''), ({'synthetic': 1}, ''), ([], ''), (['synthetic'], ''),
    ('SYNTHETIC_é', ''), ('SYNTHETIC_\n', 'SYNTHETIC_\n'),
])
def test_selection_precedes_vendor_type_and_ascii_gate(value, expected):
    # Empty arrays/objects are truthy in JS, so do not fall back to local storage.
    assert cached_degraded_token({KEY: json.dumps(value)}, {KEY: '"LOCAL"'}) == expected


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', '{broken'])
def test_javascript_invalid_json_falls_back(value):
    assert cached_degraded_token({KEY: value}, {KEY: '"LOCAL"'}) == 'LOCAL'


@pytest.mark.parametrize('storage', [[], None, {KEY: 1}, {KEY: 'x' * 8193}])
def test_invalid_storage(storage):
    with pytest.raises(ValueError, match='storage'):
        cached_degraded_token(storage, {})


def test_large_json_number_is_js_truthy_and_blocks_fallback():
    assert cached_degraded_token({KEY: '9' * 5000}, {KEY: '"LOCAL"'}) == ''


def test_huge_integer_sample_is_validation_error():
    with pytest.raises(ValueError):
        dat_from_samples([0] * 7 + [10**400])
