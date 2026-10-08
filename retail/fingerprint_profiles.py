"""Finite, repeatable profile choices constrained by the actual browser hardware."""
import re

from .us_fingerprint import SCREEN_PROFILES

GPU_FAMILIES = [
    (r'NVIDIA GeForce RTX 30\d{2}(?: Ti)?', ['NVIDIA GeForce RTX 3060 Ti', 'NVIDIA GeForce RTX 3070', 'NVIDIA GeForce RTX 3070 Ti', 'NVIDIA GeForce RTX 3080']),
    (r'NVIDIA GeForce RTX 40\d{2}(?: Ti| SUPER)?', ['NVIDIA GeForce RTX 4060', 'NVIDIA GeForce RTX 4060 Ti', 'NVIDIA GeForce RTX 4070', 'NVIDIA GeForce RTX 4080']),
    (r'AMD Radeon RX 6\d{3}(?: XT)?', ['AMD Radeon RX 6600', 'AMD Radeon RX 6600 XT', 'AMD Radeon RX 6700 XT', 'AMD Radeon RX 6800']),
    (r'Intel\(R\) UHD Graphics 6[23]0', ['Intel(R) UHD Graphics 620', 'Intel(R) UHD Graphics 630']),
]
FONT_SETS = {'core': ['Arial', 'Times New Roman', 'Courier New', 'Segoe UI', 'Segoe UI Emoji'],
             'office': ['Arial', 'Times New Roman', 'Courier New', 'Segoe UI', 'Segoe UI Emoji', 'Calibri', 'Cambria', 'Consolas', 'Georgia', 'Verdana', 'Tahoma']}

# Desktop model/device pairs published by NVIDIA. A model can have multiple
# valid device IDs; these are concrete variants, not IDs inferred from names.
# https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/README.md
GPU_DEVICE_IDS = {'NVIDIA GeForce RTX 3060 Ti': '2486', 'NVIDIA GeForce RTX 3070': '2484',
                  'NVIDIA GeForce RTX 3070 Ti': '2482', 'NVIDIA GeForce RTX 3080': '2206',
                  'NVIDIA GeForce RTX 4060': '2882', 'NVIDIA GeForce RTX 4060 Ti': '2803',
                  'NVIDIA GeForce RTX 4070': '2786', 'NVIDIA GeForce RTX 4080': '2704'}


def mix(value):
    value &= 0xffffffff
    value = ((value ^ (value >> 16)) * 0x7feb352d) & 0xffffffff
    value = ((value ^ (value >> 15)) * 0x846ca68b) & 0xffffffff
    return (value ^ (value >> 16)) & 0xffffffff


def gpu_choices(renderer):
    result = [('native', renderer)]
    if re.search(r'laptop|mobile|max-q', renderer, re.I):
        return result
    for pattern, choices in GPU_FAMILIES:
        if re.search(pattern, renderer, re.I):
            for index, name in enumerate(choices):
                alias = re.sub(pattern, name, renderer, flags=re.I)
                if alias != renderer:
                    if name in GPU_DEVICE_IDS:
                        alias = re.sub(r'\(0x[0-9a-f]+\)', '(0x0000'+GPU_DEVICE_IDS[name]+')', alias, flags=re.I)
                    else:
                        alias = re.sub(r'\s*\(0x[0-9a-f]+\)', '', alias, flags=re.I)
                result.append((f'family-{index}', alias))
            break
    return result


def generated(profile, values, hardware):
    seed = int(profile['seed'], 16)
    cpu_choices = [n for n in (2, 4, 8, 12, 16) if n <= hardware['cpu']]
    memory_choices = [n for n in (8, 16, 32) if n <= hardware['memory']]
    def number(key, choices, salt):
        selected = values.get(key, 'auto')
        if selected == 'native':
            return hardware[key]
        if selected == 'auto':
            return choices[mix(seed ^ salt) % len(choices)] if choices else hardware[key]
        if int(selected) not in choices:
            raise ValueError(f'{key}: choose a value no greater than the actual browser hardware ({hardware[key]})')
        return int(selected)
    screen = values.get('screen', 'auto')
    if screen == 'auto':
        width, height, scale = SCREEN_PROFILES[(seed >> 32) % len(SCREEN_PROFILES)]
    else:
        dimensions, scale = screen.split('@')
        width, height = map(int, dimensions.split('x'))
        scale = float(scale)
    gpus = gpu_choices(hardware['renderer'])
    selected_gpu = values.get('gpu', 'auto')
    if selected_gpu == 'auto':
        alternatives = [(key, renderer) for key, renderer in gpus if renderer != hardware['renderer']]
        selected_gpu = alternatives[mix(seed) % len(alternatives)][0] if alternatives else 'native'
    if selected_gpu not in dict(gpus):
        raise ValueError('This GPU selection is not compatible with the actual graphics hardware')
    font_mode = values.get('fonts', 'auto')
    fonts = hardware.get('fonts', [])
    if font_mode in FONT_SETS:
        allowed = {name.lower() for name in FONT_SETS[font_mode]}
        fonts = [name for name in fonts if name.lower() in allowed]
    elif font_mode == 'auto':
        def allowed(name):
            if name.lower() in {s.lower() for s in FONT_SETS['core']}:
                return True
            h = (seed & 0xffffffff) ^ 0x491fc
            for char in name.lower():
                h = ((h ^ ord(char)) * 16777619) & 0xffffffff
            return mix(h) % 3 != 0
        fonts = [name for name in fonts if allowed(name)]
    return {'cpu': number('cpu', cpu_choices, 0x37a15), 'memory': number('memory', memory_choices, 0x96b31),
            'screen': {'width': width, 'height': height}, 'viewport': {'width': width, 'height': height-120},
            'device_scale_factor': scale, 'gpu': dict(gpus)[selected_gpu], 'gpu_choice': selected_gpu,
            'fonts': fonts, 'font_mode': font_mode,
            'canvas_noise': .1 if values.get('canvas_noise') == 'subtle' else .5,
            'webgl_noise': {'off': 0, 'subtle': .05, 'standard': .25}[values.get('webgl_noise', 'standard')],
            'webgpu_limits': values.get('webgpu_limits', 'compatible')}


async def inspect_hardware(browser):
    context = await browser.new_context()
    try:
        await context.route('https://profile.invalid/', lambda route: route.fulfill(body='<title>Local profile preview</title>', content_type='text/html'))
        await context.grant_permissions(['local-fonts'], origin='https://profile.invalid')
        page = await context.new_page()
        await page.goto('https://profile.invalid/')
        return await page.evaluate('''async () => {
            const gl=document.createElement('canvas').getContext('webgl');
            const ext=gl?.getExtension('WEBGL_debug_renderer_info');
            let fonts=[];try {fonts=[...new Set((await queryLocalFonts()).map(font=>font.family))].sort()}catch {}
            return {cpu:navigator.hardwareConcurrency,memory:navigator.deviceMemory||8,
                renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):'Unavailable',
                vendor:ext?gl.getParameter(ext.UNMASKED_VENDOR_WEBGL):'Unavailable',fonts,
                user_agent:navigator.userAgent,platform:navigator.platform,
                webgpu: navigator.gpu ? 'Native adapter capabilities' : 'Unavailable'};
        }''', isolated_context=False)
    finally:
        await context.close()
