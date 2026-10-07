"""Describe browser-event distribution gaps against Balabit, CMU and IKDD.

Reports ECDF distance, not a bot score, authentication decision or fitted model.
"""
import argparse
from bisect import bisect_right
from collections import defaultdict
import csv
import io
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.analyze_behavior_datasets import distribution, download, mouse_metrics
from scripts.behavior_differential import samples

IKDD_REVISION = '5f271e3256554bcd819b54e96a287b6512fb02aa'
IKDD_REPO = 'MachineLearningVisionRG/IKDD'


def ks_distance(left, right):
    """Maximum empirical-CDF gap, including ties; no independence/p-value claim."""
    if not left or not right:
        raise ValueError('Both samples must be nonempty')
    if not all(math.isfinite(value) for value in [*left, *right]):
        raise ValueError('Samples must be finite')
    a, b = sorted(left), sorted(right)
    return max(abs(bisect_right(a, value)/len(a) - bisect_right(b, value)/len(b))
               for value in sorted(set(a+b)))


def parse_ikdd(data):
    values = defaultdict(list)
    for line in data.decode('utf-8-sig').splitlines():
        # Discard demographic/header lines; do not report participant attributes.
        match = re.fullmatch(r'(\d+)[-\u2013](\d+),(.*)', line)
        if not match:
            continue
        hold = int(match[2]) == 0
        for raw in match[3].split(','):
            if raw.strip():
                value = float(raw)
                if math.isfinite(value) and 0 <= value <= (500 if hold else 3000):
                    values['hold_ms' if hold else 'keydown_interval_ms'].append(value)
    if not values.get('hold_ms') or not values.get('keydown_interval_ms'):
        raise ValueError('IKDD file has no usable hold/latency samples')
    return dict(values)


def load_ikdd(output):
    tree_data, manifest = download(f'https://api.github.com/repos/{IKDD_REPO}/git/trees/{IKDD_REVISION}?recursive=1', output/'ikdd-tree.json')
    first_sessions = {}
    for entry in sorted(json.loads(tree_data)['tree'], key=lambda e:e['path']):
        match = re.search(r'user(\d+)_', entry['path'])
        if entry['type'] == 'blob' and match and entry['path'].endswith('.txt'):
            first_sessions.setdefault(int(match[1]), entry['path'])
    subjects = sorted(first_sessions)
    if len(subjects) < 10:
        raise ValueError('IKDD inventory is incomplete')
    selected = [subjects[round(index*(len(subjects)-1)/9)] for index in range(10)]
    pool, per_subject, manifests = defaultdict(list), {}, [manifest]
    for user in selected:
        path = first_sessions[user]
        data, manifest = download(f'https://raw.githubusercontent.com/{IKDD_REPO}/{IKDD_REVISION}/{quote(path)}', output/'raw'/Path(path).name)
        manifests.append(manifest)
        values = parse_ikdd(data)
        per_subject[str(user)] = {key:distribution(sample) for key,sample in values.items()}
        for key, sample in values.items():
            pool[key].extend(sample)
    return dict(pool), {'revision':IKDD_REVISION, 'available_users':len(subjects), 'sampled_users':len(selected),
                       'selection':'ten evenly spaced sorted user IDs; first lexicographic session each',
                       'per_subject':per_subject, 'downloads':manifests}


def compare(args):
    args.output.mkdir(parents=True, exist_ok=True)
    ikdd, metadata = load_ikdd(args.output/'ikdd')
    balabit = defaultdict(list)
    for file in sorted((args.research/'raw/training_files').glob('*/*')):
        values, _ = mouse_metrics(file.read_bytes())
        for key, values in values.items():
            balabit[key].extend(values)
    cmu = defaultdict(list)
    for row in csv.DictReader(io.StringIO((args.research/'raw/cmu.csv').read_text(encoding='utf-8-sig'))):
        for key, value in row.items():
            for prefix, name in [('H.','hold_ms'),('DD.','keydown_interval_ms'),('UD.','keyup_keydown_ms')]:
                if key.startswith(prefix):
                    cmu[name].append(float(value)*1000)
    browser = defaultdict(lambda:defaultdict(list))
    runs = json.loads((args.browser/'results.json').read_text(encoding='utf-8'))
    for row in runs:
        if row.get('error') or not row.get('completed_once') or not row.get('values_correct'):
            raise ValueError('Browser audit contains an incomplete run')
        events = json.loads((args.browser/f"{row['case']}-{row['repeat']}-events.json").read_text(encoding='utf-8'))
        if any(e['type']=='keydown' and 'target' not in e for e in events):
            raise ValueError('Rerun the browser audit with field identifiers to exclude cross-field pauses')
        for key, values in samples(events).items():
            browser[row['case']][key].extend(values)
    comparisons = []
    for dataset, source, mapping in [
        ('Balabit',balabit,{'move_interval_ms':'move_interval_ms','click_hold_ms':'click_hold_ms','scroll_interval_ms':'wheel_interval_ms'}),
        ('CMU',cmu,{'hold_ms':'hold_ms','keydown_interval_ms':'keydown_interval_ms','keyup_keydown_ms':'keyup_keydown_ms'}),
        ('IKDD',ikdd,{'hold_ms':'hold_ms','keydown_interval_ms':'keydown_interval_ms'}),
    ]:
        for human_key, browser_key in mapping.items():
            reference = source[human_key]
            for case, values in browser.items():
                observed = values.get(browser_key, [])
                if dataset=='IKDD':
                    observed = [v for v in observed if 0 <= v <= (500 if human_key=='hold_ms' else 3000)]
                comparisons.append({'dataset':dataset,'metric':human_key,'case':case,
                                    'human':distribution(reference),'browser':distribution(observed),
                                    'ecdf_distance':round(ks_distance(reference,observed),4) if observed else None})
    result = {'functional_audit':json.loads((args.browser/'acceptance.json').read_text(encoding='utf-8')),
              'browser_sessions':len(runs), 'ikdd':metadata, 'comparisons':comparisons,
              'overlap_fraction':{'CMU':sum(v<0 for v in cmu['keyup_keydown_ms'])/len(cmu['keyup_keydown_ms']),
                                  'paced':sum(v<0 for v in browser['paced']['keyup_keydown_ms'])/len(browser['paced']['keyup_keydown_ms'])},
              'interpretation':'ECDF gaps describe these pooled samples. No bot score, pass threshold or model tuning.',
              'limitations':['Balabit is RDP, not browser input; movement speed omitted because pixel scales are uncalibrated.',
                             'Keyboard tasks and key mixes differ; compare descriptive statistics, not biometric identity.',
                             'Consecutive events are correlated; no iid p-values are reported.',
                             'Event-weighted pooling is not uniform user weighting; IKDD per-subject summaries are retained.',
                             'Five clicks per mode are insufficient to validate a click-duration distribution.']}
    (args.output/'comparison.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    lines=['# Dataset comparison', '', f"Browser sessions: {len(runs)}. Functional audit passed: {result['functional_audit']['passed']}.", '',
           '| Dataset / metric | Human n | Human p05 / median / p95 (ms) | Paced n | Paced p05 / median / p95 (ms) | ECDF gap |',
           '|---|---:|---|---:|---|---:|']
    for row in comparisons:
        if row['case']!='paced':
            continue
        human, paced = row['human'], row['browser']
        quantiles=lambda v:' / '.join(f'{v[k]:.1f}' for k in ('p05','p50','p95'))
        lines.append(f"| {row['dataset']} {row['metric']} | {human['n']} | {quantiles(human)} | {paced['n']} | {quantiles(paced)} | {row['ecdf_distance']:.3f} |")
    lines += ['', 'ECDF gap ranges from 0 (identical empirical distributions) to 1 (fully separated). It is not a bot probability. No threshold is used to declare human behavior.', '',
              f"Negative keyup-to-next-keydown intervals: CMU {result['overlap_fraction']['CMU']:.2%}; paced {result['overlap_fraction']['paced']:.2%}.", '',
              *result['limitations'], '',
              'Sources: [Balabit](https://github.com/balabit/Mouse-Dynamics-Challenge), [CMU](https://www.cs.cmu.edu/~keystroke/), [IKDD](https://github.com/MachineLearningVisionRG/IKDD).', '',
              'No controller parameters were changed to produce this comparison. Raw reference downloads remain in ignored artifacts. Detailed standard/paced/restored measurements and hashes are in comparison.json.', '']
    (args.output/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    print('\n'.join(lines[:17]))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',type=Path,required=True)
    parser.add_argument('--research',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    compare(parser.parse_args())
