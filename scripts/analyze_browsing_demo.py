"""Summarize local demonstration geometry/timing, without replaying input."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics


def distribution(values):
    values = sorted(values)
    if not values:
        return {'count': 0}
    return {'count': len(values), 'p10': round(values[int((len(values)-1)*.1)], 2),
            'median': round(statistics.median(values), 2),
            'p90': round(values[int((len(values)-1)*.9)], 2)}


def summarize(events):
    keys = [e for e in events if e['type']=='key' and e.get('category')=='character']
    clicks = [e for e in events if e['type']=='click' and e.get('hold', 0)>0]
    moves, paths, burst = [], [], []
    scrolls, wheel_burst = [], []
    last_move = None
    stationary_wheels = wheel_count = 0

    def end_move():
        if len(burst)>2:
            distance = sum(math.hypot(b['x']-a['x'], b['y']-a['y']) for a,b in zip(burst, burst[1:]))
            straight = math.hypot(burst[-1]['x']-burst[0]['x'], burst[-1]['y']-burst[0]['y'])
            duration = burst[-1]['t']-burst[0]['t']
            if distance>=20 and duration>0:
                moves.append({'duration_ms': duration, 'distance_px': distance, 'speed_px_s': distance/duration*1000})
                if straight>=20:
                    paths.append(distance/straight)
        burst.clear()

    def end_wheel():
        if wheel_burst:
            scrolls.append({'duration_ms':wheel_burst[-1]['t']-wheel_burst[0]['t'],
                            'distance_px':abs(sum(e['dy'] for e in wheel_burst))})
        wheel_burst.clear()

    for event in events:
        if event['type']=='page':
            end_move()
            end_wheel()
            last_move = None
        if event['type']=='move':
            if burst and event['t']-burst[-1]['t']>200:
                end_move()
            burst.append(event)
            last_move = event['t']
        if event['type']=='wheel' and event.get('mode')==0:
            if wheel_burst and event['t']-wheel_burst[-1]['t']>250:
                end_wheel()
            wheel_burst.append(event)
            wheel_count += 1
            stationary_wheels += last_move is not None and event['t']-last_move>250
    end_move()
    end_wheel()
    return {
        'event_counts':dict(Counter(e['type'] for e in events)),
        'page_categories':dict(Counter(e['page'] for e in events if e['type']=='page')),
        'click_categories':dict(Counter(e.get('target','page') for e in clicks)),
        'character_hold_ms':distribution([e['hold'] for e in keys]),
        'character_flight_under_1s_ms':distribution([e['flight'] for e in keys if 0<e.get('flight',0)<1000]),
        'pointer_click_hold_ms':distribution([e['hold'] for e in clicks]),
        'click_horizontal_fraction':distribution([e['rx'] for e in clicks if 0<=e.get('rx',-1)<=1]),
        'click_vertical_fraction':distribution([e['ry'] for e in clicks if 0<=e.get('ry',-1)<=1]),
        'pointer_burst_duration_ms':distribution([e['duration_ms'] for e in moves]),
        'pointer_burst_distance_px':distribution([e['distance_px'] for e in moves]),
        'pointer_burst_average_speed_px_s':distribution([e['speed_px_s'] for e in moves]),
        'pointer_path_to_displacement_ratio':distribution(paths),
        'scroll_burst_duration_ms':distribution([e['duration_ms'] for e in scrolls]),
        'scroll_burst_distance_px':distribution([e['distance_px'] for e in scrolls]),
        'wheel_events_after_stationary_pointer_fraction':round(stationary_wheels/wheel_count,3) if wheel_count else None,
        'limitations':[
            'One short demonstration; distributions are descriptive, not a trained personal model.',
            'Pointer bursts split at 200ms gaps; scroll bursts at 250ms. They are not necessarily goal-directed actions.',
            'Navigation can lose the last 250ms of buffered input; page events are not unique page visits.',
            'Search typing only; no key values, page text, identities, URLs, screenshots or account data.',
            'Long typing pauses and clicks without a pointer hold are excluded from timing distributions.',
        ],
    }


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    status = json.loads((args.folder/'status.json').read_text(encoding='utf-8'))
    if status['recording']:
        parser.error('Finish capture before creating its summary.')
    events = [json.loads(line) for line in (args.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    result = summarize(events)
    (args.folder/'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
