"""Rendering variation must change the framebuffer, not invent readback bytes."""
import asyncio

import pytest
from patchright.async_api import async_playwright
from retail.fingerprint import build_scripts


@pytest.mark.parametrize('version', [1, 2])
def test_webgl_variation_is_real_repeatable_and_readback_coherent(version):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            context = await browser.new_context()
            async def render(seed, noise, object_source=False):
                page = await context.new_page()
                result = await page.evaluate(PROBE, {'version':version, 'objectSource':object_source, 'script':build_scripts(seed,
                    spoof_webgl=True, profile_values={'webgl_noise':noise, 'gpu_choice':'native'})[0]},
                    isolated_context=False)
                await page.close()
                return result
            baseline = await render(123, 0)
            varied = await render(123, .25)
            repeat = await render(123, .25)
            other_seed = await render(456, .25)
            coerced = await render(123, .25, True)
            assert varied['pixels'] != baseline['pixels']
            assert varied['pixels'] == repeat['pixels']
            assert varied['pixels'] != other_seed['pixels']
            assert varied['pixels'] == coerced['pixels']
            assert coerced['shaderCoercions'] == 2
            for result in [baseline, varied, repeat, other_seed, coerced]:
                assert result['readPixelsNative']
                assert result['sourceMethodsNative']
                assert result['copyAgrees'] and result['pboAgrees'] and result['cropAgrees']
                assert result['sourcePreserved'] and result['coercions'] == 1
                assert result['clearPixel'] == [51, 102, 153, 255]
                assert result['error'] == 0
            await browser.close()
    asyncio.run(scenario())


PROBE = r"""({version,script,objectSource}) => {
    const canvas = document.createElement('canvas');
    canvas.width=64; canvas.height=64;
    const gl=canvas.getContext(version===2?'webgl2':'webgl', {preserveDrawingBuffer:true,antialias:false});
    if (!gl) throw new Error('WebGL context unavailable');
    const readPixels = gl.readPixels;
    const shaderSource = gl.shaderSource, getShaderSource = gl.getShaderSource;
    (0,eval)(script);
    const vertex = version===2 ? '#version 300 es\nin vec2 p;out vec3 v;void main(){gl_Position=vec4(p,0.0,1.0);v=vec3(p*0.1+0.4,0.6);}'
        : 'attribute vec2 p;varying vec3 v;void main(){gl_Position=vec4(p,0.0,1.0);v=vec3(p*0.1+0.4,0.6);}';
    const fragment = version===2 ? '#version 300 es\nprecision highp float;in vec3 v;out vec4 color;void main(){color=vec4(v,1.0);}'
        : 'precision highp float;varying vec3 v;void /* comment */ main(void){gl_FragColor=vec4(v,1.0);}';
    let shaderCoercions=0;
    function shader(type,source) {
        const s=gl.createShader(type);gl.shaderSource(s,objectSource?{toString(){shaderCoercions++;return source;}}:source);gl.compileShader(s);
        if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(s));
        return s;
    }
    const vs=shader(gl.VERTEX_SHADER,vertex),fs=shader(gl.FRAGMENT_SHADER,fragment);
    const program=gl.createProgram();gl.attachShader(program,vs);gl.attachShader(program,fs);gl.linkProgram(program);
    if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(program));
    gl.useProgram(program);
    const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
    gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
    const attribute=gl.getAttribLocation(program,'p');gl.enableVertexAttribArray(attribute);gl.vertexAttribPointer(attribute,2,gl.FLOAT,false,0,0);
    gl.drawArrays(gl.TRIANGLES,0,3);
    const pixels=new Uint8Array(64*64*4);gl.readPixels(0,0,64,64,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
    const copy=document.createElement('canvas');copy.width=64;copy.height=64;
    const ctx=copy.getContext('2d',{willReadFrequently:true});ctx.drawImage(canvas,0,0);
    const image=ctx.getImageData(0,0,64,64).data;
    const copyAgrees=pixels.every((value,i)=>value===image[((63-Math.floor(i/256))*256)+(i%256)]);
    const crop=new Uint8Array(8*8*4);gl.readPixels(8,8,8,8,gl.RGBA,gl.UNSIGNED_BYTE,crop);
    const cropAgrees=crop.every((value,i)=>value===pixels[(8+Math.floor(i/32))*256+32+i%32]);
    let pboAgrees=true;
    if(version===2){
        const pack=gl.createBuffer();gl.bindBuffer(gl.PIXEL_PACK_BUFFER,pack);
        gl.bufferData(gl.PIXEL_PACK_BUFFER,pixels.length,gl.STREAM_READ);
        gl.readPixels(0,0,64,64,gl.RGBA,gl.UNSIGNED_BYTE,0);
        const pbo=new Uint8Array(pixels.length);gl.getBufferSubData(gl.PIXEL_PACK_BUFFER,0,pbo);
        pboAgrees=pbo.every((v,i)=>v===pixels[i]);gl.bindBuffer(gl.PIXEL_PACK_BUFFER,null);
    }
    const sourcePreserved=gl.getShaderSource(fs)===fragment&&gl.getShaderSource(vs)===vertex;
    let coercions=0;gl.shaderSource(fs,{toString(){coercions++;return fragment;}});
    gl.clearColor(.2,.4,.6,1);gl.clear(gl.COLOR_BUFFER_BIT);
    const clearPixel=new Uint8Array(4);gl.readPixels(0,0,1,1,gl.RGBA,gl.UNSIGNED_BYTE,clearPixel);
    return {pixels:Array.from(pixels),copyAgrees,pboAgrees,cropAgrees,sourcePreserved,coercions,shaderCoercions,
        clearPixel:Array.from(clearPixel),readPixelsNative:readPixels===gl.readPixels,
        sourceMethodsNative:shaderSource===gl.shaderSource&&getShaderSource===gl.getShaderSource,error:gl.getError()};
}"""
