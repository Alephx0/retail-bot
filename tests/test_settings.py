import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from patchright.async_api import async_playwright, expect

from retail.app import create_app
from retail.ai_provider import AIProvider

HEADERS = {'X-Retail-Client': 'dashboard'}


def test_settings_defaults_secrets_conflicts_and_running_lock(tmp_path):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        initial = client.get('/api/state').json()['settings'][0]
        assert initial['show_browser_window'] is True
        assert 'webhook' not in initial
        assert initial['agent_mode'] == 'off'
        hook = 'https://discord.com/api/webhooks/123/fixture-token'
        assert client.post('/api/settings', json={'webhook': hook, 'show_browser_window': False}).status_code == 200
        assert client.post('/api/settings', json={'sound_volume': .6}).status_code == 200
        saved = client.app.state.store.get('settings', 'settings')
        assert saved['webhook'] == hook and saved['show_browser_window'] is False
        conflict = client.post('/api/settings', json={'sound_volume': .8, '_expected': {'sound_volume': .4}})
        assert conflict.status_code == 409
        assert client.post('/api/settings', json={'sound_volume': .8, '_expected': {'sound_volume': .6}}).status_code == 200
        client.app.state.engine.jobs['fixture'] = object()
        try:
            assert client.post('/api/settings', json={'max_running_tasks': 3}).status_code == 409
            assert client.post('/api/settings', json={'checkout_sound': False}).status_code == 200
        finally:
            client.app.state.engine.jobs.clear()
        assert client.post('/api/settings', json={'webhook': '', 'notifications': False}).status_code == 200
        assert client.get('/api/state').json()['settings'][0]['has_webhook'] is False
        assert client.post('/api/settings/test-discord').status_code == 422


def test_discord_test_only_sends_explicit_saved_test(tmp_path, monkeypatch):
    import httpx
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(204)

    monkeypatch.setattr(httpx.AsyncClient, 'post', fake_post)
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        hook = 'https://discord.com/api/webhooks/123/fixture-token'
        client.post('/api/settings', json={'webhook': hook})
        assert not calls
        assert client.post('/api/settings/test-discord').status_code == 200
        assert len(calls) == 1
        assert calls[0][0] == hook
        assert calls[0][1]['follow_redirects'] is False


def test_ai_connection_edit_invalidates_readiness(tmp_path, monkeypatch):
    async def fake_test(self):
        return {'ok': True, 'message': 'Ready'}
    monkeypatch.setattr(AIProvider, 'test', fake_test)
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        connection = client.post('/api/ai_connections', json={'name': 'Fixture', 'api_key': 'fixture-key'}).json()
        client.post(f"/api/ai-connections/{connection['id']}/test")
        renamed = client.put(f"/api/ai_connections/{connection['id']}", json={'name': 'Renamed'}).json()
        assert renamed['health']['ok'] is True
        changed = client.put(f"/api/ai_connections/{connection['id']}", json={'api_key': 'replacement'}).json()
        assert 'health' not in changed
        assert 'api_key' not in changed
        assert client.get('/api/state').json()['settings'][0]['agent_mode'] == 'off'
        async def edit_during_test(self):
            current = client.app.state.store.get('ai_connections', connection['id'])
            client.app.state.store.put('ai_connections', {**current, 'api_key': 'changed-during-test'})
            return {'ok': True, 'message': 'Old credentials passed'}
        monkeypatch.setattr(AIProvider, 'test', edit_during_test)
        assert client.post(f"/api/ai-connections/{connection['id']}/test").status_code == 409
        current = client.app.state.store.get('ai_connections', connection['id'])
        assert current['api_key'] == 'changed-during-test'
        assert 'health' not in current


def test_settings_browser_drafts_validation_and_layout(tmp_path, monkeypatch):
    """Real DOM/browser; all requests served by an isolated TestClient or fixtures."""
    async def fake_ai_test(self):
        return {'ok': True, 'message': 'Fixture connection verified'}

    monkeypatch.setattr(AIProvider, 'test', fake_ai_test)
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        async def scenario():
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors, saves = [], []
                page.on('pageerror', lambda e: errors.append(str(e)))

                async def route_request(route):
                    request = route.request
                    from urllib.parse import urlsplit
                    path = urlsplit(request.url).path
                    if path == '/api/settings/browser-options':
                        await route.fulfill(json=[{'id': 'chromium', 'label': 'Bundled Chromium', 'available': True},
                                                  {'id': 'chrome', 'label': 'Chrome', 'available': False},
                                                  {'id': 'msedge', 'label': 'Edge', 'available': True}])
                        return
                    if path == '/api/settings' and request.method == 'POST':
                        saves.append(request.post_data_json)
                    response = client.request(request.method, path + ('?' + urlsplit(request.url).query if urlsplit(request.url).query else ''),
                                              content=request.post_data, headers=HEADERS)
                    await route.fulfill(status=response.status_code, body=response.content,
                                        headers={'content-type': response.headers.get('content-type', 'text/plain')})

                await page.route('**/*', route_request)
                await page.goto('http://127.0.0.1/')
                await page.locator('[data-view=settings]').click()
                await expect(page.locator('[data-settings-tab]')).to_have_count(5)
                interval = page.get_by_label('Check interval (seconds)', exact=True)
                await interval.fill('6.5')
                await expect(page.locator('[data-settings-status]')).to_contain_text('Unsaved changes')
                # Navigation preserves the draft without writing it to the server.
                await page.locator('[data-view=home]').click()
                await page.locator('[data-view=settings]').click()
                await expect(interval).to_have_value('6.5')
                await page.locator('[data-settings-tab=integrations]').click()
                await page.get_by_role('button', name='Add API connection', exact=True).click()
                await page.get_by_label('Connection name', exact=True).fill('Fixture connection')
                await page.get_by_label('API key', exact=True).fill('fixture-secret')
                await page.get_by_role('button', name='Save connection', exact=True).click()
                await expect(page.locator('[data-ai-test]')).to_have_count(1)
                assert not saves, 'Saving a key must not enable AI or save other preferences'
                await page.locator('[data-ai-test]').click()
                await expect(page.locator('.ai-connections').get_by_text('Fixture connection verified', exact=True)).to_be_visible()
                await expect(page.get_by_label('When AI may assist', exact=True)).to_have_value('off')
                await page.locator('[data-settings-tab=general]').click()
                await expect(interval).to_have_value('6.5')
                # Errors in hidden panels are brought into view and kept inline.
                await interval.fill('1')
                await page.locator('[data-settings-tab=notifications]').click()
                await page.get_by_role('button', name='Save changes', exact=True).click()
                await expect(interval).to_be_focused()
                await expect(page.locator('#settings-error')).to_contain_text('Check interval')
                assert not saves
                await interval.fill('6.5')
                await page.get_by_role('button', name='Save changes', exact=True).click()
                await expect(page.locator('[data-settings-status]')).to_contain_text('Saved.')
                assert saves[-1] == {'default_monitor_delay': 6500, '_expected': {'default_monitor_delay': 4500}}
                assert client.get('/api/state').json()['settings'][0]['default_monitor_delay'] == 6500
                # Headless is explicit and persists; it does not silently change other settings.
                await page.locator('[data-settings-tab=browser]').click()
                await expect(page.get_by_label('Window mode', exact=True)).to_have_value('true')
                await page.get_by_label('Window mode', exact=True).select_option('false')
                await page.get_by_role('button', name='Save changes', exact=True).click()
                await expect(page.locator('[data-settings-status]')).to_contain_text('Saved.')
                assert client.get('/api/state').json()['settings'][0]['show_browser_window'] is False
                # Keyboard navigation follows the tab pattern.
                await page.locator('[data-settings-tab=browser]').focus()
                await page.keyboard.press('ArrowRight')
                await expect(page.locator('[data-settings-tab=notifications]')).to_be_focused()
                await expect(page.locator('#settings-panel-notifications')).to_be_visible()
                # Secret replacement is explicit; removal disables Discord and erases the stored secret.
                await page.get_by_label('Webhook', exact=True).select_option('replace')
                await page.get_by_label('Discord webhook URL', exact=True).fill('https://discord.com/api/webhooks/123/fixture-token')
                await page.get_by_label('Send Discord notifications', exact=True).check()
                await page.get_by_role('button', name='Save changes', exact=True).click()
                await expect(page.locator('[data-settings-status]')).to_contain_text('Saved.')
                await page.get_by_label('Webhook', exact=True).select_option('remove')
                await expect(page.get_by_label('Send Discord notifications', exact=True)).not_to_be_checked()
                await page.get_by_role('button', name='Save changes', exact=True).click()
                await expect(page.locator('[data-settings-status]')).to_contain_text('Saved.')
                assert client.get('/api/state').json()['settings'][0]['has_webhook'] is False
                # A stale save fails visibly and retains the draft; Discard reloads it.
                await page.locator('[data-settings-tab=general]').click()
                await interval.fill('7.5')
                client.post('/api/settings', json={'default_monitor_delay': 7000})
                await page.get_by_role('button', name='Save changes', exact=True).click()
                await expect(page.locator('#settings-error')).to_contain_text('changed in another window')
                await expect(interval).to_have_value('7.5')
                page.once('dialog', lambda dialog: dialog.accept())
                await page.get_by_role('button', name='Discard changes', exact=True).click()
                await expect(interval).to_have_value('7')
                await expect(page.locator('[data-settings-status]')).to_contain_text('All changes saved')
                # Runtime restrictions are visible before a rejected save.
                client.app.state.engine.jobs['fixture'] = object()
                try:
                    await expect(page.get_by_label('Maximum active browser workers', exact=True)).to_be_disabled()
                    await expect(page.locator('[data-running-note]')).to_contain_text('locked')
                finally:
                    client.app.state.engine.jobs.clear()
                await expect(page.get_by_label('Maximum active browser workers', exact=True)).to_be_enabled()
                # Recording has a persistent app-wide indication.
                await page.locator('[data-settings-tab=data]').click()
                await page.get_by_label('Record browser traces for troubleshooting', exact=True).check()
                await page.get_by_role('button', name='Save changes', exact=True).click()
                await expect(page.locator('#trace-recording')).to_be_visible()
                await page.get_by_label('Record browser traces for troubleshooting', exact=True).uncheck()
                await page.get_by_role('button', name='Save changes', exact=True).click()
                await expect(page.locator('#trace-recording')).to_be_hidden()
                await page.locator('[data-settings-tab=browser]').click()
                await page.get_by_label('Window mode', exact=True).select_option('true')
                await page.get_by_role('button', name='Save changes', exact=True).click()
                await expect(page.locator('[data-settings-status]')).to_contain_text('Saved.')
                artifacts = Path('artifacts/settings-redesign')
                artifacts.mkdir(parents=True, exist_ok=True)
                for key in ['general','browser','notifications','integrations','data']:
                    await page.locator(f'[data-settings-tab={key}]').click()
                    await expect(page.locator('[data-settings-tab][aria-selected=true]')).to_have_count(1)
                    await expect(page.locator(f'[data-settings-tab={key}]')).to_have_class('selected')
                    await page.screenshot(path=str(artifacts / f'{key}-desktop.png'), full_page=True, animations='disabled')
                await page.set_viewport_size({'width': 390, 'height': 844})
                for key in ['general','browser','notifications','integrations','data']:
                    await page.locator(f'[data-settings-tab={key}]').click()
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), key
                await page.screenshot(path=str(artifacts / 'data-mobile.png'), full_page=True, animations='disabled')
                assert not errors, errors
                await browser.close()

        asyncio.run(scenario())
