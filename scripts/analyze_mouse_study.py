"""Fit bounded aggregate movement parameters; never replay recorded coordinates."""
import argparse
import bisect
import json
import math
from pathlib import Path
import statistics


def smooth_progress(t, start=1., end=1.):
    if t<=0: return 0.
    if t>=1: return 1.
    u=t**start/(t**start+(1-t)**end)
    return u**3*(10-15*u+6*u*u)


def distribution(values):
    values=sorted(values)
    return {'count':len(values),'p10':round(values[int((len(values)-1)*.1)],4),
            'median':round(statistics.median(values),4),'p90':round(values[int((len(values)-1)*.9)],4)} if values else {'count':0}


def trial_metrics(row):
    # The final recorded point is pointer-down; it includes a click pause.
    points=row['points'][:-1]
    clean=[]
    for point in points:
        if not clean or point['t']>clean[-1]['t']:
            clean.append(point)
    if len(clean)<10: return None
    sx,sy=row['start']['x'],row['start']['y']
    dx,dy=row['click']['x']-sx,row['click']['y']-sy
    distance=math.hypot(dx,dy)
    elapsed=clean[-1]['t']-clean[0]['t']
    if distance<80 or not 100<=elapsed<=2500: return None
    path=sum(math.hypot(b['x']-a['x'],b['y']-a['y']) for a,b in zip(clean,clean[1:]))
    if path/distance>1.6 or row.get('misses'): return None
    # Avoid fitting intentional mid-movement rests as slow mouse motion.
    if max(b['t']-a['t'] for a,b in zip(clean,clean[1:]))>150: return None
    times=[(p['t']-clean[0]['t'])/elapsed for p in clean]
    progress=[]
    for p in clean:
        projected=((p['x']-sx)*dx+(p['y']-sy)*dy)/(distance*distance)
        progress.append(max(progress[-1] if progress else 0,min(1,max(0,projected))))
    sampled=[]
    for t in [i/10 for i in range(1,10)]:
        i=min(len(times)-1,max(1,bisect.bisect_left(times,t)))
        weight=(t-times[i-1])/(times[i]-times[i-1])
        sampled.append(progress[i-1]+weight*(progress[i]-progress[i-1]))
    bend=max(abs((p['x']-sx)*dy-(p['y']-sy)*dx)/(distance*distance) for p in clean)
    return {'index':row['index'],'distance_px':distance,'movement_ms':elapsed,'progress':sampled,
            'bend_fraction':bend,'hold_ms':row['click']['hold'],'settle_ms':row['click']['t']-clean[-1]['t']}


def fit(rows):
    x=[r['distance_px'] for r in rows];y=[r['movement_ms'] for r in rows]
    xm,ym=statistics.mean(x),statistics.mean(y)
    denominator=sum((v-xm)**2 for v in x)
    slope=sum((a-xm)*(b-ym) for a,b in zip(x,y))/denominator if denominator>0 else .75
    slope=max(.1,min(2.,slope))
    base=max(50,min(500,ym-slope*xm))
    grid=[.7,.85,1.,1.2,1.4,1.6,1.8,2.]
    def loss(a,b):
        return statistics.mean((smooth_progress((i+1)/10,a,b)-p)**2 for r in rows for i,p in enumerate(r['progress']))
    a,b=min(((a,b) for a in grid for b in grid),key=lambda pair:loss(*pair))
    return {'samples':len(rows),'base_ms':round(base,3),'ms_per_pixel':round(slope,5),
            'start_power':a,'end_power':b,
            'bend_fraction':round(min(.15,statistics.median(r['bend_fraction'] for r in rows)),5)}


def summarize(trials):
    # First movement begins from the Start button and is a warm-up, not a task target.
    rows=[m for r in trials if r['index']!=0 and (m:=trial_metrics(r))]
    if len(rows)<10:
        raise ValueError('Need at least ten usable movements; repeat the study')
    profile=fit(rows)
    held=[]
    for i,row in enumerate(rows):
        trained=fit(rows[:i]+rows[i+1:])
        baseline=min(1100,180+row['distance_px']/1350*1000)
        learned=min(1800,trained['base_ms']+trained['ms_per_pixel']*row['distance_px'])
        for j,observed in enumerate(row['progress']):
            t=(j+1)/10
            standard=smooth_progress(t)
            blended=(standard+smooth_progress(t,trained['start_power'],trained['end_power']))/2
            held.append({'standard_progress_error':abs(standard-observed),'blended_progress_error':abs(blended-observed)})
        row['standard_duration_error_ms']=abs(baseline-row['movement_ms'])
        row['blended_duration_error_ms']=abs((baseline+learned)/2-row['movement_ms'])
    return {'completed_trials':len(trials),'usable_trials':len(rows),'excluded_indices':[r['index'] for r in trials if r['index'] not in {m['index'] for m in rows}],
            'movement_ms':distribution([r['movement_ms'] for r in rows]),
            'distance_px':distribution([r['distance_px'] for r in rows]),
            'click_hold_ms':distribution([r['hold_ms'] for r in rows]),
            'settle_ms':distribution([r['settle_ms'] for r in rows]),
            'median_progress_deciles':[round(statistics.median(r['progress'][i] for r in rows),4) for i in range(9)],
            'pointer_profile':profile,
            'leave_one_out':{**{key:round(statistics.mean(r[key] for r in rows),3) for key in ('standard_duration_error_ms','blended_duration_error_ms')},
                             **{key:round(statistics.mean(r[key] for r in held),5) for key in ('standard_progress_error','blended_progress_error')}},
            'limitations':['One 24-target session; warm-up, misses, long pauses and large detours excluded.',
                           'Cross-validation holds out movements, not another day or browsing context.',
                           'Progress is projected monotonically toward the target; micro-corrections are not replayed.',
                           'Baseline comparison uses the midpoint travel speed and excludes browser transport overhead.']}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('folder',type=Path)
    args=parser.parse_args()
    if json.loads((args.folder/'status.json').read_text())['recording']:
        parser.error('Finish the mouse study first')
    trials=[json.loads(line) for line in (args.folder/'trials.jsonl').read_text().splitlines()]
    result=summarize(trials)
    (args.folder/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
