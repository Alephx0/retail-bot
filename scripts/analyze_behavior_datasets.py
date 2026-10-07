"""Download public research samples into ignored artifacts; emit aggregate timing statistics.

No dataset or fitted biometric model is bundled into the application.
Balabit: first lexicographic training session per user, client clock, seconds.
CMU: complete fixed-password timing table, seconds (including negative UD times).
"""
import argparse
import csv
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from urllib.request import Request, urlopen

BALABIT_REVISION = 'd00d6f779254a2a917deeab4a5b7a9e8643bd91e'
CMU_URL = 'https://www.cs.cmu.edu/~keystroke/DSL-StrongPasswordData.csv'


def distribution(values):
    values = sorted(value for value in values if math.isfinite(value))
    if not values:
        return {'n': 0}
    def percentile(q):
        index = (len(values) - 1) * q
        lower = int(index)
        return round(values[lower] + (values[min(lower+1, len(values)-1)] - values[lower]) * (index-lower), 4)
    return {'n': len(values), 'p05': percentile(.05), 'p50': percentile(.5), 'p95': percentile(.95)}


def download(url, destination):
    if not destination.exists():
        request = Request(url, headers={'User-Agent': 'retail-behavior-research/1'})
        with urlopen(request, timeout=45) as response:
            data = response.read(16_000_001)
        if len(data) > 16_000_000:
            raise ValueError('Research download exceeded its size limit')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    data = destination.read_bytes()
    return data, {'url': url, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def mouse_metrics(data):
    metrics, counts = defaultdict(list), Counter()
    previous = None
    previous_scroll = None
    pressed = {}
    for row in csv.DictReader(io.StringIO(data.decode('utf-8-sig'))):
        counts['rows'] += 1
        counts['state:' + row['state']] += 1
        t, x, y = (float(row[key]) for key in ('client timestamp', 'x', 'y'))
        if not all(math.isfinite(v) for v in (t, x, y)):
            counts['invalid'] += 1
            continue
        if row['state'] == 'Move' and previous:
            pt, px, py, state = previous
            dt, distance = t-pt, math.hypot(x-px, y-py)
            if dt <= 0:
                counts['nonpositive_dt'] += 1
            elif dt <= 1 and state == 'Move' and distance > 0:
                metrics['move_interval_ms'].append(dt*1000)
                metrics['speed_recorded_px_s'].append(distance/dt)
        if row['state'] == 'Pressed':
            pressed[row['button']] = (t, x, y)
        elif row['state'] == 'Released' and row['button'] in pressed:
            pt, px, py = pressed.pop(row['button'])
            # Separate clicks from drags; truncate long holds explicitly.
            if 0 < t-pt <= 2 and math.hypot(x-px, y-py) <= 5:
                metrics['click_hold_ms'].append((t-pt)*1000)
        if row['button'] == 'Scroll' and row['state'] in ('Up', 'Down'):
            if previous_scroll is not None and 0 < t-previous_scroll <= 1:
                metrics['scroll_interval_ms'].append((t-previous_scroll)*1000)
            previous_scroll = t
        previous = (t, x, y, row['state'])
    return metrics, counts


def analyze(output):
    output.mkdir(parents=True, exist_ok=True)
    tree_data, _ = download(f'https://api.github.com/repos/balabit/Mouse-Dynamics-Challenge/git/trees/{BALABIT_REVISION}?recursive=1', output/'balabit-tree.json')
    tree = json.loads(tree_data)
    sessions = {}
    for item in sorted(tree['tree'], key=lambda item: item['path']):
        if item['type'] == 'blob' and item['path'].startswith('training_files/'):
            sessions.setdefault(item['path'].split('/')[1], item)
    manifests, per_user = [], {}
    combined, totals = defaultdict(list), Counter()
    for user, item in sessions.items():
        data, manifest = download(f'https://raw.githubusercontent.com/balabit/Mouse-Dynamics-Challenge/{BALABIT_REVISION}/{item["path"]}', output/'raw'/item['path'])
        manifests.append(manifest)
        metrics, counts = mouse_metrics(data)
        totals.update(counts)
        per_user[user] = {key: distribution(value) for key, value in metrics.items()}
        for key, values in metrics.items():
            combined[key].extend(values)
    cmu, manifest = download(CMU_URL, output/'raw/cmu.csv')
    manifests.append(manifest)
    keys = defaultdict(list)
    subjects, rows = set(), 0
    for row in csv.DictReader(io.StringIO(cmu.decode('utf-8-sig'))):
        subjects.add(row['subject']); rows += 1
        for key, value in row.items():
            for prefix, metric in [('H.', 'hold_ms'), ('DD.', 'keydown_interval_ms'), ('UD.', 'keyup_keydown_ms')]:
                if key.startswith(prefix):
                    keys[metric].append(float(value)*1000)
    result = {'balabit': {'revision': BALABIT_REVISION, 'users': len(sessions), 'sessions': len(sessions),
                          'selection': 'first lexicographic training session per user',
                          'clock': 'client timestamp, seconds; nonpositive deltas excluded from speed',
                          'counts': dict(totals), 'metrics': {key: distribution(value) for key, value in combined.items()},
                          'per_user': per_user},
              'cmu': {'subjects': len(subjects), 'rows': rows,
                      'metrics': {key: distribution(value) for key, value in keys.items()},
                      'overlapping_key_transitions': sum(value < 0 for value in keys['keyup_keydown_ms'])},
              'downloads': manifests,
              'limits': ['Remote desktop pixels and timing are not calibrated browser CSS-pixel measurements.',
                         'CMU measures one repeated password, not general web form entry.',
                         'No scroll delta calibration or bot-detection evaluation is provided by these samples.',
                         'Aggregate comparisons only; no individual traces or learned model enter runtime.']}
    (output/'dataset-summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k: {a:b for a,b in v.items() if a!='per_user'} for k,v in result.items() if k in ('balabit','cmu')}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/behavior-research'))
    analyze(parser.parse_args().output)
