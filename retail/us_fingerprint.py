"""Deterministic US desktop context settings, applied by Chromium itself."""

US_TIMEZONES = ('America/New_York', 'America/Chicago', 'America/Denver',
                'America/Los_Angeles', 'America/Phoenix', 'America/Anchorage', 'Pacific/Honolulu')
SCREEN_PROFILES = (
    (1366, 768, 1), (1440, 900, 1), (1536, 864, 1.25),
    (1600, 900, 1), (1920, 1080, 1), (2048, 1152, 1.25), (2560, 1440, 1),
)
SURFACES = ('fonts', 'navigator', 'screen')


async def browser_identity_user_agent(browser):
    """Derive a process-wide Chrome identity from the installed browser.

    Use the browser launch switch, not context emulation: service workers must
    retain the same identity and native client-hint metadata as documents.
    """
    session = await browser.new_browser_cdp_session()
    try:
        version = await session.send('Browser.getVersion')
    finally:
        await session.detach()
    user_agent = version.get('userAgent')
    if not isinstance(user_agent, str) or not user_agent:
        raise ValueError('Could not read the installed browser identity')
    return user_agent.replace('HeadlessChrome/', 'Chrome/')


def enabled(settings):
    return settings.get('fingerprint_backend', 'javascript') != 'fingerprint-suite' and any(
        settings.get('fingerprint_' + key) for key in SURFACES)


def context_options(profile, settings, region):
    if settings.get('fingerprint_backend') == 'fingerprint-suite':
        return {}
    if region != 'US':
        if enabled(settings):
            raise ValueError('US font/navigator/screen profiles require a US account region')
        return {}
    timezone = settings.get('fingerprint_timezone', 'America/New_York')
    if timezone not in US_TIMEZONES:
        raise ValueError('Choose a supported US timezone')
    options = {'locale': 'en-US', 'timezone_id': timezone}
    if settings.get('fingerprint_screen'):
        seed = int(profile['seed'], 16)
        width, height, scale = SCREEN_PROFILES[(seed >> 32) % len(SCREEN_PROFILES)]
        options.update(screen={'width': width, 'height': height},
                       viewport={'width': width, 'height': height - 120},
                       device_scale_factor=scale)
    return options
