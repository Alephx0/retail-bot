import pytest

from scripts.fingerprint_differential import zero_lie_failures, zero_warning_failures


def test_zero_lie_check_accepts_a_complete_zero_result():
    assert zero_lie_failures([
        {'case': 'bot-off', 'repeat': 1,
         'sites': {'creepjs': {'status': 200, 'total_lies': 0}}}
    ], 1) == []


@pytest.mark.parametrize('creep', [
    {}, {'status': 200}, {'status': 200, 'total_lies': None},
    {'status': 200, 'total_lies': False},
    {'status': 200, 'total_lies': 1},
    {'status': 503, 'total_lies': 0},
    {'status': 200, 'total_lies': 0, 'error': 'incomplete export'},
    {'status': 200, 'total_lies': 0, 'page_errors': ['script failed']},
    {'status': 200, 'total_lies': 0, 'screenshot_error': 'capture failed'},
])
def test_zero_lie_check_rejects_warnings_and_incomplete_results(creep):
    assert zero_lie_failures([
        {'case': 'everything', 'repeat': 1, 'sites': {'creepjs': creep}}
    ], 1)


def test_zero_lie_check_rejects_missing_runs():
    assert zero_lie_failures([], 3)


def clean_warning_record():
    return {'case': 'everything', 'repeat': 1,
            'probe': {'gpu': {'renderer': 'GPU'}, 'worker': {'renderer': 'GPU'}},
            'sites': {'creepjs': {'status': 200, 'total_lies': 0,
                                 'warning_count': 0, 'captured_error_count': 0,
                                 'worker_renderer': 'GPU'}},
            'worker_profile_errors': []}


def test_warning_check_accepts_complete_results():
    assert zero_warning_failures([clean_warning_record()], 1) == []
    assert zero_warning_failures([], 1)


@pytest.mark.parametrize('field,value', [
    ('warning_count', None), ('warning_count', False), ('warning_count', 1),
    ('captured_error_count', None), ('captured_error_count', False),
    ('captured_error_count', 1), ('worker_renderer', None), ('worker_renderer', 'other'),
])
def test_warning_check_rejects_missing_or_nonzero_findings(field, value):
    row = clean_warning_record()
    if value is None:
        del row['sites']['creepjs'][field]
    else:
        row['sites']['creepjs'][field] = value
    assert zero_warning_failures([row], 1)


@pytest.mark.parametrize('fault', ['missing-main', 'missing-worker', 'different-worker', 'bootstrap'])
def test_warning_check_rejects_worker_failures(fault):
    row = clean_warning_record()
    if fault == 'missing-main':
        row['probe'].pop('gpu')
    elif fault == 'missing-worker':
        row['probe'].pop('worker')
    elif fault == 'different-worker':
        row['probe']['worker']['renderer'] = 'other'
    else:
        row['worker_profile_errors'] = ['Worker restart failed']
    assert zero_warning_failures([row], 1)
