"""Exercise automatic and manual task assignment on the isolated test server."""
import asyncio
import os
from uuid import uuid4

from patchright.async_api import async_playwright, expect

BASE = os.environ.get('RETAIL_TEST_URL', 'http://127.0.0.1:8785')


async def main():
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        api = await driver.request.new_context(base_url=BASE, extra_http_headers={'X-Retail-Client': 'dashboard'})
        tag = uuid4().hex[:8]
        email = f'match-{tag}@example.com'
        account = await (await api.post('/api/accounts', data={'name': 'Automatic '+tag, 'email': email})).json()
        manual = await (await api.post('/api/accounts', data={'name': 'Manual '+tag, 'email': f'manual-{email}'})).json()
        profile = await (await api.post('/api/profiles', data={'name': 'Profile '+tag, 'email': email.upper()})).json()
        await page.goto(BASE)
        await page.locator('[data-view=task_groups]').click()
        await page.locator('#primary').click()
        await page.get_by_label('Group name', exact=True).fill('Matching '+tag)
        await page.locator('dialog[open] [name=products]').fill('B012345678')
        await page.locator('dialog[open]').get_by_role('button', name='Create group', exact=True).click()
        await page.locator('[data-work-add]').first.click()
        toggle = page.get_by_label('Match Accounts to Profiles', exact=True)
        await expect(toggle).not_to_be_checked()
        await toggle.check()
        await expect(page.locator('[data-manual-accounts]')).to_be_hidden()
        await page.get_by_label('Profile', exact=True).select_option(profile['id'])
        await expect(page.locator('.assignment-preview')).to_contain_text('Automatic '+tag)
        await expect(page.locator('.assignment-preview')).to_contain_text('Matching email')
        await toggle.uncheck()
        await page.get_by_label('Account', exact=True).select_option(manual['id'])
        await expect(page.locator('.assignment-preview')).to_contain_text('Manual '+tag)
        await expect(page.locator('.assignment-preview')).to_contain_text('Manual selection')
        await toggle.check()
        await expect(page.locator('.assignment-preview')).to_contain_text('Automatic '+tag)
        duplicate = await (await api.post('/api/accounts', data={'name': 'Duplicate '+tag, 'email': email})).json()
        await page.get_by_label('Task quantity', exact=True).fill('2')
        await expect(page.locator('.assignment-errors')).to_contain_text('2 accounts match')
        await expect(page.locator('#assignment-editor [type=submit]')).to_be_disabled()
        await api.delete('/api/accounts/'+duplicate['id'])
        await page.get_by_label('Task quantity', exact=True).fill('1')
        await expect(page.locator('.assignment-errors')).to_be_empty()
        await page.screenshot(path='artifacts/automatic-matching.png', full_page=True)
        await page.get_by_role('button', name='Create 1 task', exact=True).click()
        await expect(page.locator('#assignment-editor')).to_have_count(0)
        state = await (await api.get('/api/state')).json()
        task = next(t for t in state['tasks'] if t['profile_id'] == profile['id'])
        assert task['account_id'] == account['id']
        assert not any(link['profile_id'] == profile['id'] for link in state['account_profiles'])
        assert not errors, errors
        await api.dispose()
        await browser.close()
        print('PASS: explicit automatic matching, preview reasons, manual selection, duplicate blocking, automatic task creation')


if __name__ == '__main__':
    asyncio.run(main())
