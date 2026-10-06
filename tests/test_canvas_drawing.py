import asyncio

import pytest
from patchright.async_api import async_playwright

from retail.fingerprint import build_scripts


@pytest.mark.parametrize('seed', [1, 123, 285165479])
def test_drawing_profiles_preserve_explicit_pixels_and_all_bitmap_views(seed):
    async def scenario():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.add_init_script(build_scripts(seed, perturb_canvas=True)[0])
                page = await context.new_page()
                result = await page.evaluate('''async () => {
                    const canvas=document.createElement('canvas');canvas.width=64;canvas.height=32;
                    const ctx=canvas.getContext('2d',{willReadFrequently:true});
                    let state=12345;
                    const byte=()=>{state=(Math.imul(state,1664525)+1013904223)>>>0;return state>>>24;};
                    let changed=0;
                    for(let trial=0;trial<12;trial++) {
                        const expected=[];
                        for(let y=0;y<32;y++)for(let x=0;x<64;x++) {
                            const rgb=[byte(),byte(),byte()];expected.push(...rgb,255);
                            ctx.fillStyle=`rgb(${rgb.join(',')})`;ctx.fillRect(x,y,1,1);
                        }
                        changed += ctx.getImageData(0,0,64,32).data.filter((v,i)=>v!==expected[i]).length;
                    }
                    const gradient=ctx.createLinearGradient(0,0,64,32);
                    gradient.addColorStop(0,'white');gradient.addColorStop(1,'black');
                    ctx.fillStyle=gradient;ctx.fillRect(0,0,64,32);
                    const pixels=ctx.getImageData(0,0,64,32).data;
                    const bitmap=await createImageBitmap(canvas);
                    const copy=new OffscreenCanvas(64,32).getContext('2d');copy.drawImage(bitmap,0,0);bitmap.close();
                    const image=new Image();image.src=canvas.toDataURL();await image.decode();
                    const exported=new OffscreenCanvas(64,32).getContext('2d');exported.drawImage(image,0,0);
                    const agree=c=>c.getImageData(0,0,64,32).data.every((v,i)=>v===pixels[i]);
                    let conversions=0;
                    ctx.createLinearGradient({valueOf(){conversions++;return 0;}},0,64,32);
                    let radiusError;try{ctx.createRadialGradient(0,0,-1,1,1,2)}catch(e){radiusError=e.name;}
                    return {changed,bitmap:agree(copy),export:agree(exported),conversions,radiusError};
                }''', isolated_context=False)
                assert result == dict(changed=0, bitmap=True, export=True,
                                      conversions=1, radiusError='IndexSizeError')
            finally:
                await browser.close()
    asyncio.run(scenario())
