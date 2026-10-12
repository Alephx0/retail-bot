import asyncio
import copy
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from retail import behavior
from retail.models import PointerCalibration
from scripts.analyze_mouse_study import smooth_progress, summarize
from test_browsing_calibration import CALIBRATION


POINTER={'samples':18,'base_ms':425.756,'ms_per_pixel':.71426,'start_power':.7,'end_power':1.8,'bend_fraction':.0733}


def test_fitter_rejects_pauses_and_warmup_and_improves_held_out_motion():
    trials=[]
    for i in range(20):
        distance=150+i*55
        duration=300+.8*distance
        points=[]
        for j in range(101):
            t=j/100
            progress=smooth_progress(t,.7,1.8)
            points.append({'t':200+t*duration,'x':distance*progress,'y':math.sin(math.pi*progress)*distance*.05})
        trials.append({'index':i,'start':{'x':0,'y':0},'click':{'x':distance,'y':0,'t':200+duration+100,'hold':100},
                       'points':[*points,{'t':200+duration+100,'x':distance,'y':0}],'misses':0})
    trials[2]['misses']=1
    # Mid-travel pause must not teach the engine to stop its pointer in flight.
    for p in trials[3]['points'][50:]: p['t']+=250
    result=summarize(trials)
    assert result['usable_trials']==17
    assert result['excluded_indices']==[0,2,3]
    assert result['pointer_profile']['base_ms']==pytest.approx(300,abs=.01)
    assert result['pointer_profile']['ms_per_pixel']==pytest.approx(.8)
    assert result['leave_one_out']['blended_progress_error']<result['leave_one_out']['standard_progress_error']
    assert result['leave_one_out']['blended_duration_error_ms']<result['leave_one_out']['standard_duration_error_ms']


def test_pointer_curve_is_monotone_smooth_at_endpoints_and_bounded():
    PointerCalibration.model_validate(POINTER)
    for start,end in ((.7,.7),(.7,2),(2,.7),(2,2),(.7,1.8)):
        profile={**POINTER,'start_power':start,'end_power':end}
        positions=[behavior._pointer_progress(i/1000,profile) for i in range(1001)]
        assert positions[0]==0 and positions[-1]==1
        assert all(a<=b for a,b in zip(positions,positions[1:]))
        assert behavior._pointer_progress(.000001,profile)/.000001<.001
        assert (1-behavior._pointer_progress(.999999,profile))/.000001<.001
    for i in range(101):
        t=i/100
        assert behavior._pointer_progress(t)==pytest.approx(smooth_progress(t))
    assert behavior._pointer_progress(.4,POINTER)>behavior._pointer_progress(.4)
    for patch in ({'samples':9},{'start_power':.2},{'bend_fraction':1},{'base_ms':float('nan')}):
        with pytest.raises(ValueError): PointerCalibration.model_validate({**POINTER,**patch})


@pytest.mark.parametrize('stall',[0,.08])
def test_fitted_movement_profiles_transport_stalls_and_scope(monkeypatch,stall):
    async def scenario():
        rows={}
        for browsing,preset in [(False,'balanced'),*[(True,p) for p in behavior.TIMING_PRESETS]]:
            clock=[0.]
            points=[]
            async def sleep(seconds): clock[0]+=seconds
            async def move(x,y):
                points.append((clock[0],x,y))
                clock[0]+=.006+(stall if len(points)==5 else 0)
            monkeypatch.setattr(behavior,'time',SimpleNamespace(monotonic=lambda:clock[0]))
            monkeypatch.setattr(behavior.asyncio,'sleep',sleep)
            calibration={**copy.deepcopy(CALIBRATION),'pointer':copy.deepcopy(POINTER)}
            page=SimpleNamespace(context=SimpleNamespace(_retail_input_timing_preset=preset,_retail_browsing_input=browsing,_retail_input_calibration=calibration),
                viewport_size={'width':1200,'height':900},mouse=SimpleNamespace(move=move))
            controller=behavior.PacedInput(page,seed=20)
            calibration['pointer']['base_ms']=50
            assert controller.calibration['pointer']['base_ms']==POINTER['base_ms']
            await controller.move(800,500)
            assert points[-1][1:]==(800,500)
            assert all(0<=x<1200 and 0<=y<900 for _,x,y in points)
            assert all(b[0]-a[0]>=behavior.POINTER_FRAME_SECONDS-1e-8 for a,b in zip(points,points[1:-1]))
            assert not controller.lock.locked()
            rows[browsing,preset]=clock[0]
        assert rows[True,'balanced']>rows[False,'balanced']
        assert rows[True,'slow']>rows[True,'balanced']>rows[True,'fast']>rows[True,'aggressive']
    asyncio.run(scenario())
