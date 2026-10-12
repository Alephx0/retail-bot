"""Read-only local dashboard timing sample. Does not start accounts or tasks."""
import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8789)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--output', default='artifacts/account-care-review/dashboard-measurement.json')
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    results = {}
    for path in ('/api/state', '/api/account-care', '/static/account-care.js'):
        durations = []
        for _ in range(args.samples):
            start = time.perf_counter()
            with opener.open(f'http://127.0.0.1:{args.port}{path}', timeout=45) as response:
                response.read()
            durations.append(round((time.perf_counter()-start)*1000, 2))
        results[path] = {'samples_ms': durations, 'median_ms': statistics.median(durations), 'max_ms': max(durations)}
    report = {'routes': results, 'limitations': 'Local HTTP samples against the current workspace. Includes dashboard serialization; excludes retailer, model and browser-launch latency. Does not modify or start tasks.'}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
