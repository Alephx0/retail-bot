"""Summarize exports from fingerprint_differential.py without scoring bot acceptance."""
import json
import hashlib
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True)


def summarize(root, baseline_root=None):
    rows = json.loads((root / 'results.json').read_text(encoding='utf-8'))
    groups = defaultdict(list)
    for row in rows:
        folder = root / row['case'] / str(row['repeat'])
        creep = json.loads((folder / 'creepjs.json').read_text(encoding='utf-8')) if (folder / 'creepjs.json').exists() else {}
        fp = creep.get('fingerprint', {})
        row['creep_summary'] = {
            'total_lies': fp.get('lies', {}).get('totalLies'),
            'lies': fp.get('lies', {}).get('data', {}),
            'trash': fp.get('trash', {}).get('trashBin'),
            'errors': fp.get('capturedErrors', {}).get('data'),
            'resistance': fp.get('resistance'),
            'headless': fp.get('headless'),
            'lied_surfaces': {k: v['lied'] for k, v in fp.items() if isinstance(v, dict) and v.get('lied')},
            'surface_hashes': {k: fp.get(k, {}).get('$hash') for k in ('canvas2d','canvasWebgl','offlineAudioContext','workerScope','screen','navigator')},
            'render_hashes': {surface: hashlib.sha256(canonical({k:fp.get(surface,{}).get(k) for k in keys}).encode()).hexdigest() for surface, keys in {
                'canvas': ('dataURI','paintURI','paintCpuURI','textURI','emojiURI'),
                'webgl': ('pixels','pixels2','dataURI','dataURI2','parameters','extensions'),
                'audio': ('sampleSum','binsSample','copySample','values','compressorGainReduction'),
            }.items()},
            'worker_renderer': fp.get('workerScope', {}).get('webglRenderer'),
        }
        leaks_file = folder / 'browserleaks.json'
        leaks = json.loads(leaks_file.read_text(encoding='utf-8')) if leaks_file.exists() else {}
        row['browserleaks_hashes'] = {key: (match[1] if (match := re.search(label+r'\s+([A-Fa-f0-9]{32})', leaks.get('text',''))) else None) for key, label in [('report','WebGL Report Hash'),('image','WebGL Image Hash')]}
        ami_file = folder / 'amiunique.json'
        ami = json.loads(ami_file.read_text(encoding='utf-8')) if ami_file.exists() else {}
        parts = ami.get('text','').split('JAVASCRIPT ATTRIBUTES', 1)
        def attribute(text, label):
            match = re.search(r'\d+ - '+label+r'\s*\t\s*\t([^\n]+)', text)
            return match[1].strip() if match else None
        http = parts[0].split('HTTP HEADERS ATTRIBUTES')[-1]
        js = parts[1] if len(parts) > 1 else ''
        row['amiunique_values'] = {'http_ua':attribute(http,'User agent'), 'js_ua':attribute(js,'User agent'), 'http_language':attribute(http,'Content language'), 'js_language':attribute(js,'Content language'), 'js_platform':attribute(js,'Platform')}
        groups[row['case']].append(row)
    baseline = groups.get('bot-off', [{}])[0]
    base_probe = baseline.get('probe', {})
    base_lies = baseline.get('creep_summary', {}).get('lies', {})
    summaries = {}
    for name, runs in groups.items():
        first = runs[0]
        probe = first.get('probe', {})
        summaries[name] = {
            'repeats':len(runs),
            'lie_counts':[r['creep_summary']['total_lies'] for r in runs],
            'warning_counts':[None if r['creep_summary']['trash'] is None else len(r['creep_summary']['trash']) for r in runs],
            'worker_profile_errors':[r.get('worker_profile_errors', []) for r in runs],
            'new_lie_apis':sorted(set().union(*(set(r['creep_summary']['lies']) for r in runs))-set(base_lies)),
            'changed_probe_surfaces':[k for k in probe if probe[k] != base_probe.get(k)],
            'unstable_probe_surfaces':[k for k in probe if len({canonical(r.get('probe',{}).get(k)) for r in runs}) > 1],
            'browserleaks_hashes':[r['browserleaks_hashes'] for r in runs],
            'creep_surface_hash_variants':{k:len({r['creep_summary']['surface_hashes'][k] for r in runs}) for k in first['creep_summary']['surface_hashes']},
            'creep_render_hash_variants':{k:len({r['creep_summary']['render_hashes'][k] for r in runs}) for k in first['creep_summary']['render_hashes']},
            'worker_main_gpu_match':[r.get('probe',{}).get('gpu',{}).get('renderer') == r.get('probe',{}).get('worker',{}).get('renderer') for r in runs],
            'creep_worker_main_gpu_match':[None if not r['creep_summary']['worker_renderer'] else r.get('probe',{}).get('gpu',{}).get('renderer') == r['creep_summary']['worker_renderer'] for r in runs],
            'amiunique_ua_match':[None if not r['amiunique_values']['http_ua'] or not r['amiunique_values']['js_ua'] else r['amiunique_values']['http_ua']==r['amiunique_values']['js_ua'] for r in runs],
            'amiunique_language_match':[None if not r['amiunique_values']['http_language'] or not r['amiunique_values']['js_language'] else r['amiunique_values']['http_language'].split(',')[0].split(';')[0]==r['amiunique_values']['js_language'].split(',')[0] for r in runs],
            'errors':[{'repeat':r['repeat'],'error':r.get('error'),'sites':r.get('sites'),'worker_profile_errors':r.get('worker_profile_errors', [])} for r in runs if r.get('error') or r.get('worker_profile_errors') or any(v.get('error') or v.get('page_errors') or v.get('screenshot_error') or v.get('status') != 200 for v in r.get('sites',{}).values())],
        }
    (root / 'summary.json').write_text(json.dumps({'cases':summaries,'runs':rows},indent=2), encoding='utf-8')
    backend = rows[0].get('backend', 'javascript')
    repetitions = sorted({len(runs) for runs in groups.values()})
    lines = ['# Differential fingerprint audit', '', 'Chromium/Chrome '+rows[0].get('browser_version','unknown')+f'. Backend: {backend}. Fresh sessions per configuration: {repetitions}; fixed synthetic account/seed. All transformed cases are headless. Baselines launch the selected backend executable directly, with transformations disabled, and attach over CDP; they are not an uninstrumented manual browser. Default profiles differ from the bot in screen/locale settings. Production settings and accounts were not used.', '', '| Configuration | CreepJS lies | Changed probe surfaces vs bot off | Unstable probe surfaces |', '|---|---|---|---|']
    for name, s in summaries.items():
        lines.append(f"| {name} | {', '.join(map(str,s['lie_counts']))} | {', '.join(s['changed_probe_surfaces']) or 'none'} | {', '.join(s['unstable_probe_surfaces']) or 'none'} |")
    lines += ['', '## Findings', '']
    coverage = {site:sum(site in r.get('sites',{}) for r in rows)
                for site in ('creepjs','browserleaks','amiunique')}
    lines += ['- Captures per site: '+canonical(coverage)+'. Zero means that site was not included in this audit.']
    if baseline_root is not None:
        prior = json.loads((baseline_root / 'results.json').read_text(encoding='utf-8'))
        comparisons = {}
        for name, runs in groups.items():
            old_runs = [r for r in prior if r['case'] == name]
            if not old_runs:
                continue
            prior_counts = [json.loads((baseline_root / name / str(r['repeat']) / 'creepjs.json').read_text(encoding='utf-8'))['fingerprint']['lies']['totalLies'] for r in old_runs]
            comparisons[name] = {
                'before_lies': prior_counts,
                'after_lies': summaries[name]['lie_counts'],
                'all_probe_outputs_match_prior': all(r.get('probe') == old_runs[0].get('probe') for r in runs),
            }
        (root / 'comparison.json').write_text(json.dumps(comparisons,indent=2), encoding='utf-8')
        lines += ['', '### Comparison with prior audit', '', '| Configuration | Before lies | After lies | Probe outputs unchanged |', '|---|---|---|---|']
        for name, c in comparisons.items():
            lines += [f"| {name} | {c['before_lies']} | {c['after_lies']} | {c['all_probe_outputs_match_prior']} |"]
        lines += ['']
    if (root / 'pytest.xml').exists():
        suites = list(ET.parse(root / 'pytest.xml').getroot().iter('testsuite'))
        totals = {k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
        lines += ['- Repository test suite: '+canonical(totals)+'.']
    failing = [name for name,s in summaries.items() if s['new_lie_apis']]
    lines += ['- Configurations with new lie APIs relative to bot-off: '+(', '.join(failing) or 'none observed')+'.']
    creep_rows = [r for r in rows if 'creepjs' in r.get('sites', {})]
    if creep_rows:
        lines += ['- CreepJS internally reported warning-bin entries: '+str(sum(len(r['creep_summary']['trash'] or []) for r in creep_rows))+'. Internally captured errors: '+str(sum(len(r['creep_summary']['errors'] or []) for r in creep_rows))+'. Missing fields must be checked in the original exports.']
    if 'restored-off' in summaries:
        restored = summaries['restored-off']
        lines += ['- Restoration: '+('measured probe values match the disabled baseline; CreepJS lie counts '+str(restored['lie_counts']) if not restored['changed_probe_surfaces'] else 'differences remain: '+str(restored['changed_probe_surfaces']))+'.']
    for name in ('webgl-only','webgpu-only','everything'):
        if name not in groups:
            continue
        r = groups[name][0]
        probe = r.get('probe',{})
        lines += [f"- {name}: main WebGL renderer `{probe.get('gpu',{}).get('renderer')}`; WebGPU info `{canonical((probe.get('webgpu') or {}).get('info'))}`; CreepJS worker renderer `{r['creep_summary']['worker_renderer']}`."]
    if all(all(s['amiunique_ua_match']) and all(s['amiunique_language_match']) for s in summaries.values()):
        lines += ['- AmIUnique HTTP/JavaScript UA and primary language agree in all completed runs.']
    lines += ['- Intended rendering stability and warning-free behavior are separate: stable output does not excuse new prototype warnings. Each configuration uses the same implementation throughout its repetitions.', '- Results apply to the recorded backend and executable version. They do not establish the same behavior for another browser build.', '']
    if backend == 'native':
        lines += ['- Native graphics identity is shared by WebGL, WebGPU, and all worker realms when either GPU identity flag is enabled. Canvas and shader rendering changes remain separately selectable. The worker flag needs no constructor interception in this backend. Audio retains valid native values.', '']
    lines += ['', '## Detailed differences', '']
    for name, s in summaries.items():
        lines += [f'### {name}', '', '- New lie APIs: '+(', '.join(s['new_lie_apis']) or 'none'), '- Dedicated worker/main GPU match: '+str(s['worker_main_gpu_match']), '- CreepJS worker/main GPU match: '+str(s['creep_worker_main_gpu_match']), '- AmIUnique HTTP/JS UA match: '+str(s['amiunique_ua_match']), '- AmIUnique primary language match: '+str(s['amiunique_language_match']), '- BrowserLeaks report/image hashes: '+canonical(s['browserleaks_hashes']), '- CreepJS surface hash variant counts: '+canonical(s['creep_surface_hash_variants']), '- Errors: '+canonical(s['errors']), '']
        lines += ['- Warning-bin counts: '+canonical(s['warning_counts']), '- Worker initialization errors: '+canonical(s['worker_profile_errors']), '']
        lines += ['- Render-only hash variant counts (excludes CreepJS warning metadata): '+canonical(s['creep_render_hash_variants']), '']
    lines += ['## Interpretation limits', '', 'Counts describe this run, not retailer acceptance. Audio metadata can legitimately remain native. Workers-only changes interception, not GPU identity; the combined run is needed to assess propagation. Native/headless screen and UA differences are expected. A stable hash does not establish coherent values. CreepJS surface hashes include warning metadata; use render-only counts to distinguish that variation from actual rendering instability. See summary.json for warning details, and each case/repeat folder for original page exports and screenshots.', '', 'Sources: [CreepJS](https://abrahamjuliot.github.io/creepjs/), [BrowserLeaks](https://browserleaks.com/webgl), [AmIUnique](https://www.amiunique.org/fingerprint).']
    (root / 'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps(summaries,indent=2))


if __name__ == '__main__':
    summarize(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
