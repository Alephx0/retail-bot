"""Compare complete differential captures without treating detector findings as success."""
import argparse
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def measure(root, case, repeats):
    rows = [row for row in read(root / 'results.json') if row['case'] == case]
    if len(rows) != repeats or {row['repeat'] for row in rows} != set(range(1, repeats + 1)):
        raise ValueError(f'{root}/{case}: incomplete repetitions')
    findings = []
    for row in rows:
        result = read(root / case / str(row['repeat']) / 'creepjs.json')
        if row.get('error') or result.get('error') or result.get('page_errors') or result.get('screenshot_error') or result.get('status') != 200:
            raise ValueError(f'{root}/{case}: incomplete capture')
        fp = result['fingerprint']
        if type(fp['lies']['totalLies']) is not int or not isinstance(fp['trash']['trashBin'], list):
            raise ValueError('Invalid CreepJS export')
        renderer = row['probe']['gpu']['renderer']
        findings.append({
            'lies': fp['lies']['totalLies'], 'lie_details': fp['lies']['data'],
            'warnings': fp['trash']['trashBin'], 'captured_errors': fp['capturedErrors']['data'],
            'main_renderer': renderer,
            'creep_worker_matches': fp['workerScope']['webglRenderer'] == renderer,
            'dedicated_worker_matches': row['probe']['worker']['renderer'] == renderer,
            'worker_initialization_errors': row.get('worker_profile_errors', []),
            'ratings': {k: fp['headless'][k] for k in ('likeHeadlessRating', 'headlessRating', 'stealthRating')},
        })
    probes = [row['probe'] for row in rows]
    return {
        'case': case, 'sessions': len(rows),
        'runtime_versions': sorted({row['browser_version'] for row in rows}),
        'generated_browser_majors': sorted({row['suite_browser_major'] for row in rows if 'suite_browser_major' in row}),
        'profile_digests': sorted({row['suite_profile_digest'] for row in rows if 'suite_profile_digest' in row}),
        'unstable_probe_surfaces': [key for key in probes[0] if any(p.get(key) != probes[0][key] for p in probes)],
        'findings': findings,
    }, probes[0]


def compare(args):
    measurements, probes = {}, {}
    sources = [('custom-disabled', args.custom, 'bot-off'),
               ('custom-enabled', args.custom, 'everything'),
               ('custom-restored', args.custom, 'restored-off'),
               ('suite-disabled', args.suite, 'bot-off'),
               ('suite-enabled', args.suite, 'suite-full'),
               ('suite-restored', args.suite, 'restored-off')]
    sources += [(f'suite-{p.name}', p, 'suite-full') for p in args.suite_extra]
    if args.previous:
        sources += [('previous-custom', args.previous, 'everything')]
    for label, root, case in sources:
        measurements[label], probes[label] = measure(root, case, args.repeats)
    restoration = {
        'custom': probes['custom-disabled'] == probes['custom-restored'],
        'suite': probes['suite-disabled'] == probes['suite-restored'],
        'disabled_backends_match': probes['custom-disabled'] == probes['suite-disabled'],
        'custom_matches_previous': probes['custom-enabled'] == probes.get('previous-custom') if args.previous else None,
    }
    result = {'measurements_complete': True, 'measurements': measurements, 'restoration': restoration}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    lines = ['# Fingerprint backend comparison', '',
             'Same stock Chrome host and Python/Patchright driver. Suite mode uses the upstream complete profile; custom mode uses all five transformations. Each row is repeated in fresh contexts. Missing captures fail this comparison; detector findings remain visible.', '',
             '| Case | Lies per run | Warnings per run | CreepJS worker mismatches | Like-headless / headless / stealth |',
             '|---|---|---|---:|---|']
    for label, data in measurements.items():
        findings = data['findings']
        ratings = sorted({tuple(f['ratings'].values()) for f in findings})
        lines.append(f"| {label} | {[f['lies'] for f in findings]} | {[len(f['warnings']) for f in findings]} | {sum(not f['creep_worker_matches'] for f in findings)}/{len(findings)} | {ratings} |")
    lines += ['', '## Repeatability and restoration', '', '```json', json.dumps(restoration, indent=2), '```', '']
    for label, data in measurements.items():
        lines += [f"- {label}: browser {data['runtime_versions']}; generated Chrome major {data['generated_browser_majors'] or 'not generated'}; unstable probe surfaces {data['unstable_probe_surfaces'] or 'none'}."]
    lines += ['', '## Detector details', '']
    for label, data in measurements.items():
        first = data['findings'][0]
        if not first['lies'] and not first['warnings']:
            continue
        lines += [f'### {label}', '', '```json', json.dumps({'lies': first['lie_details'], 'warnings': first['warnings']}, indent=2), '```', '']
    lines += ['## Scope', '', 'The upstream suite payload was not patched or combined with custom hooks. Its document initialization does not supply the custom service-worker startup/restart integration. Generated browser versions and physical GPU behavior can differ. Results describe these packages, seeds, driver and host, not every possible fingerprint or retailer acceptance. Lower headless heuristics do not cancel out API lies or worker mismatches.', '']
    (args.output / 'COMPARISON.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'cases': len(measurements), 'restoration': restoration}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--custom', type=Path, required=True)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--suite-extra', type=Path, nargs='*', default=[])
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    compare(parser.parse_args())
