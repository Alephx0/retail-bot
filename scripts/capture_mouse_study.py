"""Isolated visible mouse study. Own CDP session; no application/account state."""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import time

from patchright.async_api import async_playwright


def sanitize_trial(raw):
    """Keep only complete bounded mouse measurements from the local fixture."""
    if not isinstance(raw,dict):
        return None
    def numbers(value,fields):
        if not isinstance(value,dict):
            raise ValueError('Expected measurement')
        result={}
        for field in fields:
            n=value[field]
            if isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or abs(n)>1e7:
                raise ValueError('Invalid measurement')
            result[field]=round(n,4)
        return result
    try:
        index=raw['index']
        if type(index) is not int or not 0<=index<24:
            return None
        points=raw['points']
        if not isinstance(points,list) or not 2<=len(points)<=4001:
            return None
        clean={'index':index,'start':numbers(raw['start'],('x','y')),
               'target':numbers(raw['target'],('x','y','width','height')),
               'viewport':numbers(raw['viewport'],('width','height')),
               'click':numbers(raw['click'],('x','y','t','hold')),
               'points':[numbers(p,('t','x','y')) for p in points]}
        clean['misses']=max(0,min(1000,int(raw.get('misses',0))))
        if clean['click']['hold']<0 or any(p['t']<0 for p in clean['points']):
            return None
        return clean
    except (KeyError,ValueError,TypeError):
        return None


async def main(folder):
    folder.mkdir(parents=True,exist_ok=True)
    if (folder/'trials.jsonl').exists():
        raise ValueError('Use a fresh output directory for each study')
    started=time.time()
    state={'pid':os.getpid(),'recording':True,'connected':False,'saved_trials':0,'rejected_trials':0,'move_samples':0,'started_at':started}
    def status():
        state['updated_at']=time.time()
        temp=folder/'status.tmp'
        temp.write_text(json.dumps(state,indent=2),encoding='utf-8')
        temp.replace(folder/'status.json')
    content=Path(__file__).with_name('mouse_study.html').read_text(encoding='utf-8')
    with (folder/'trials.jsonl').open('w',encoding='utf-8',buffering=1) as stream:
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=False,args=['--start-maximized'])
            context=await browser.new_context(no_viewport=True)
            async def route(request):
                if request.request.url=='https://mouse-study.test/':
                    await request.fulfill(body=content,content_type='text/html')
                else:
                    await request.abort()
            await context.route('**/*',route)
            page=await context.new_page()
            await page.goto('https://mouse-study.test/',wait_until='domcontentloaded')
            cdp=await context.new_cdp_session(page)
            await page.bring_to_front()
            seen=set()
            try:
                while browser.is_connected() and not page.is_closed():
                    try:
                        async with asyncio.timeout(2):
                            reply=await cdp.send('Runtime.evaluate',{'expression':'window.__mouseStudyDrain?.()','returnByValue':True})
                            captured=reply.get('result',{}).get('value') or {}
                            state['connected']=bool(captured)
                            state['move_samples']=captured.get('move_samples',state['move_samples'])
                            state['active']=bool(captured.get('active'))
                            state['paused']=bool(captured.get('paused'))
                            if state['recording']:
                                for raw in captured.get('trials',[])[:24]:
                                    trial=sanitize_trial(raw)
                                    if trial and trial['index'] not in seen:
                                        seen.add(trial['index'])
                                        stream.write(json.dumps(trial,separators=(',',':'))+'\n')
                                        state['saved_trials']+=1
                                    elif not trial:
                                        state['rejected_trials']+=1
                            if captured.get('done') or time.time()-started>=900:
                                state['recording']=False
                            await cdp.send('Runtime.evaluate',{'expression':'window.__mouseStudyConnect?.()' if state['recording'] else 'window.__mouseStudyStop?.()'})
                    except Exception:
                        state['connected']=False
                    status()
                    await asyncio.sleep(.1)
            finally:
                state['recording']=False
                status()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(main(parser.parse_args().output))
