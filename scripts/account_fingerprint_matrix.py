"""Isolated public-site audit. Stores measured results, never modifies detector output."""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retail.amazon import Amazon
from retail.models import Account
from retail.store import Store
from fingerprint_differential import capture, SITES

SITES.update(scan='https://fingerprint-scan.com/', bot='https://overpoweredjs.bot/')
FLAGS = ['canvas', 'webgl', 'webgpu', 'audio', 'workers', 'fonts', 'navigator', 'screen']


async def main(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    results_path = output / 'results.json'
    results = json.loads(results_path.read_text(encoding='utf-8')) if results_path.exists() else []
    cases = [('javascript', name) for name in ['disabled', 'canvas', 'webgl', 'webgpu', 'everything']]
    cases += [('native', name) for name in ['disabled', 'everything']]
    if args.cases:
        cases = [case for case in cases if '-'.join(case) in args.cases.split(',')]
    for backend, name in cases:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            extension_ids = [store.put('browser_extensions', {'name': Path(path).name, 'path': path})['id'] for path in args.extensions]
            settings = {'browser_channel': 'chrome', 'show_browser_window': True,
                        'browser_incognito': not args.normal,
                        'browser_extension_ids': extension_ids,
                        'fingerprint_backend': backend, 'fingerprint_proxy_location': False,
                        **{'fingerprint_'+flag: name == 'everything' or name == flag for flag in FLAGS}}
            store.put('settings', settings, 'settings')
            account = store.put('accounts', Account(name='Public audit').model_dump(), 'public-audit-fixed-v1')
            adapter = Amazon(store)
            folder = output / f'{backend}-{name}'
            folder.mkdir(exist_ok=True)
            row = {'backend': backend, 'configuration': name, 'settings': settings, 'sites': {}}
            print(f'START {backend}-{name}', flush=True)
            try:
                context = await adapter.context(account)
                for site in args.sites:
                    row['sites'][site] = await capture(context, site, folder)
                    print(f'{backend}-{name} {site}: {row["sites"][site]}', flush=True)
                await context.close()
            except Exception as exc:
                row['error'] = str(exc)
                print(str(exc), flush=True)
            finally:
                await adapter.close()
                store.db.close()
            results = [old for old in results if (old['backend'], old['configuration']) != (backend, name)]
            results.append(row)
            results_path.write_text(json.dumps(results, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--cases', default='')
    parser.add_argument('--normal', action='store_true')
    parser.add_argument('--extensions', nargs='*', default=[])
    parser.add_argument('--sites', nargs='+', default=['scan', 'bot', 'creepjs'], choices=['scan', 'bot', 'creepjs'])
    asyncio.run(main(parser.parse_args()))
