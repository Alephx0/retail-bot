"""Optional, non-interactive pointer marker for browser input testing."""
import asyncio

SCRIPT = r'''(() => {
    if (document.getElementById('__retail_visible_mouse')) return;
    const marker = document.createElement('div');
    marker.id = '__retail_visible_mouse';
    marker.setAttribute('aria-hidden', 'true');
    marker.style.cssText = 'all:initial!important;position:fixed!important;left:0!important;top:0!important;width:0!important;height:0!important;pointer-events:none!important;z-index:2147483647!important;';
    const root = marker.attachShadow({mode:'open'});
    root.innerHTML = `<style>
        :host, * { pointer-events:none!important; }
        .cursor { display:none; position:absolute; left:-10px; top:-10px;
            box-sizing:border-box; width:20px; height:20px; border:2px solid white;
            border-radius:50%; background:rgba(24,119,242,.55);
            box-shadow:0 0 0 1px #172554,0 1px 5px #0008; }
        .cursor::after { content:''; position:absolute; width:4px; height:4px;
            border-radius:50%; background:white; left:6px; top:6px; }
        .cursor[data-pressed=true] { background:#f59e0b; transform:scale(1.35); }
    </style><span class="cursor"></span>`;
    const cursor = root.querySelector('.cursor');
    const mount = () => {
        if (!marker.isConnected && document.documentElement) document.documentElement.append(marker);
    };
    const update = event => {
        mount();
        marker.style.setProperty('transform', `translate(${event.clientX}px,${event.clientY}px)`, 'important');
        cursor.style.display = 'block';
        cursor.dataset.pressed = String(event.buttons !== 0);
    };
    for (const type of ['mousemove','mousedown','mouseup'])
        window.addEventListener(type, update, {capture:true, passive:true});
    window.addEventListener('mouseout', event => {
        if (!event.relatedTarget || event.relatedTarget.tagName === 'IFRAME') cursor.style.display = 'none';
    }, {capture:true, passive:true});
    window.addEventListener('blur', () => { cursor.style.display = 'none'; });
    mount();
    document.addEventListener('DOMContentLoaded', mount, {once:true});
})()'''


async def install(context):
    """Cover new documents/popups and already opened persistent-context pages."""
    if getattr(context, '_retail_visible_mouse_installed', False):
        return
    context._retail_visible_mouse_installed = True
    await context.add_init_script(SCRIPT)

    async def show_frame(frame):
        try:
            await asyncio.wait_for(frame.evaluate(SCRIPT, isolated_context=False), timeout=2)
        except Exception:
            # A closing/navigating document is covered at its next lifecycle event.
            pass

    def watch_page(page):
        # Patchright may defer init scripts until an execution context is used.
        # Lifecycle hooks also cover documents without an HTML response (srcdoc).
        page.on('framenavigated', show_frame)
        page.on('domcontentloaded', lambda: show_frame(page.main_frame))

    context.on('page', watch_page)
    for page in context.pages:
        watch_page(page)
        for frame in page.frames:
            await show_frame(frame)
