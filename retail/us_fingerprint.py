"""Deterministic US desktop context settings, applied by Chromium itself."""

US_TIMEZONES = ('America/New_York', 'America/Chicago', 'America/Denver',
                'America/Los_Angeles', 'America/Phoenix', 'America/Anchorage', 'Pacific/Honolulu')
SCREEN_PROFILES = (
    (1366, 768, 1), (1440, 900, 1), (1536, 864, 1.25),
    (1600, 900, 1), (1920, 1080, 1), (2048, 1152, 1.25), (2560, 1440, 1),
)
SURFACES = ('fonts', 'navigator', 'screen')


def enabled(settings):
    return settings.get('fingerprint_backend', 'javascript') == 'javascript' and any(
        settings.get('fingerprint_' + key) for key in SURFACES)


def context_options(profile, settings, region):
    if not enabled(settings):
        return {}
    if region != 'US':
        raise ValueError('US font/navigator/screen profiles require a US account region')
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
