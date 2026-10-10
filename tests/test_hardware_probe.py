"""Background inspection must never borrow a visible/account browser."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from retail.amazon import Amazon
from retail.fingerprint_profiles import inspect_hardware
from patchright.async_api import async_playwright


def test_background_probe_is_shared_by_concurrent_accounts_and_keyed_by_binary(monkeypatch):
    async def scenario():
        adapter = Amazon(None)
        # Any accidental inspection of the shared browser fails this test.
        adapter.browser = object()
        probes = []

        async def launch(**options):
            assert options['headless'] is True
            probe = SimpleNamespace(options=options, close=AsyncMock())
            probes.append(probe)
            return probe

        async def inspect(probe):
            assert probe is not adapter.browser
            await asyncio.sleep(0)
            return {'cpu': 8, 'memory': 8, 'renderer': 'Fixture GPU'}

        adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        monkeypatch.setattr('retail.amazon.inspect_hardware', inspect)
        settings = {'browser_identity': 'default', 'browser_channel': 'chrome', 'show_browser_window': True}
        results = await asyncio.gather(*(adapter.hardware_profile(settings) for _ in range(10)))
        assert len(probes) == 1
        assert all(result is results[0] for result in results)
        probes[0].close.assert_awaited_once()
        # Native profiles use the same unmodified hardware baseline, privately.
        assert await adapter.hardware_profile({**settings, 'fingerprint_backend': 'native'}) is results[0]
        assert len(probes) == 1
        await adapter.hardware_profile({**settings, 'browser_identity': 'msedge'})
        assert probes[-1].options['channel'] == 'msedge'
        assert len(probes) == 2
        # Changing a custom executable invalidates its cache even with the same identity.
        monkeypatch.setattr('retail.amazon.browser_options', lambda value: {'executable_path': value['brave_executable']})
        for binary in ('brave-one.exe', 'brave-two.exe'):
            await adapter.hardware_profile({'browser_identity': 'brave', 'brave_executable': binary})
        assert len(probes) == 4
        assert all(probe.close.await_count == 1 for probe in probes)

    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [RuntimeError('Inspection failed'), asyncio.CancelledError()])
def test_failed_or_cancelled_probe_closes_and_can_retry(monkeypatch, failure):
    async def scenario():
        adapter = Amazon(None)
        probe = SimpleNamespace(close=AsyncMock())
        launch = AsyncMock(return_value=probe)
        adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        inspect = AsyncMock(side_effect=failure)
        monkeypatch.setattr('retail.amazon.inspect_hardware', inspect)
        with pytest.raises(type(failure)):
            await adapter.hardware_profile({})
        probe.close.assert_awaited_once()
        assert not adapter.identity_hardware
        inspect.side_effect = None
        inspect.return_value = {'cpu': 8}
        assert await adapter.hardware_profile({}) == {'cpu': 8}
        assert launch.await_count == 2
        assert probe.close.await_count == 2

    asyncio.run(scenario())


def test_real_background_probe_reads_hardware_and_closes_local_page():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(channel='chromium', headless=True)
            try:
                result = await inspect_hardware(browser)
                assert result['cpu'] > 0 and result['memory'] > 0
                assert result['renderer'] != 'Unavailable'
                assert result['user_agent'] and result['platform']
                assert result['fonts']  # Local Font Access remains usable headlessly.
                assert browser.contexts == []
            finally:
                await browser.close()

    asyncio.run(scenario())
