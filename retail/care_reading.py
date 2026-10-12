"""Purposeful, read-only pointer activity during account browsing pauses."""
import asyncio

from . import behavior


# Return geometry only. Re-sample the live document on every movement, excluding
# account/navigation/purchase controls and content covered by another element.
READING_POINT = r'''({u,v,pick,previous}) => {
    const blocked='nav,header,footer,form,button,input,select,textarea,[role=button],[role=dialog],[role=menu],[contenteditable=true]';
    const allowed=e=>{
        if(e.closest(blocked))return false;
        const link=e.closest('a');
        if(!link)return true;
        try{const u=new URL(link.href);return u.origin===location.origin && /^\/(dp|gp\/product)\/[A-Z0-9]{10}(?:[/?]|$)/i.test(u.pathname);}
        catch{return false;}
    };
    const points=[];
    const nodes=document.querySelectorAll('h1,h2,h3,p,main img,[role=main] img,#landingImage,#productTitle,#feature-bullets li,[data-hook=review-body],.a-expander-content td,#productDetails_techSpec_section_1 td');
    for(const e of Array.from(nodes).slice(0,400)){
        if(!allowed(e))continue;
        const b=e.getBoundingClientRect(),s=getComputedStyle(e);
        if(s.visibility!=='visible'||s.display==='none'||Number(s.opacity)===0)continue;
        const left=Math.max(16,b.left),right=Math.min(innerWidth-16,b.right);
        const top=Math.max(80,b.top),bottom=Math.min(innerHeight-24,b.bottom);
        if(right-left<24||bottom-top<12)continue;
        const x=left+(right-left)*u,y=top+(bottom-top)*v;
        const hit=document.elementFromPoint(x,y);
        if(!hit||!(hit===e||e.contains(hit))||!allowed(hit))continue;
        if(Math.hypot(x-previous[0],y-previous[1])<24)continue;
        points.push({x,y});
    }
    return points.length?points[Math.min(points.length-1,Math.floor(pick*points.length))]:null;
}'''


async def read_with_pointer(page, seconds, guard):
    """Read within the existing budget, without scrolling, clicks or background jobs."""
    if seconds <= 0:
        return
    paced = behavior.controller(page)
    budget = asyncio.timeout(seconds)
    try:
        async with budget:
            if paced is None or not paced.browsing:
                while True:
                    await asyncio.sleep(min(4, seconds))
                    await guard()
            # One owner for reading, typing, clicks and scrolls. Cancellation
            # releases this lock and leaves no detached pointer task behind.
            async with paced.lock:
                while True:
                    await asyncio.sleep(paced.rng.uniform(.45, 1.6))
                    await guard()
                    async with asyncio.timeout(3):
                        point = await page.evaluate(READING_POINT, {
                            'u': paced.rng.triangular(.2, .8, .5),
                            'v': paced.rng.triangular(.2, .8, .5),
                            'pick': paced.rng.random(), 'previous': paced.position,
                        })
                        if point:
                            await paced.move(point['x'], point['y'])
                    await guard()
    except TimeoutError:
        if not budget.expired():
            raise
