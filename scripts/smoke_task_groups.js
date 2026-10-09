// Playwright MCP browser_run_code_unsafe input. Use only the isolated fixture
// application on port 8771, seeded with fixture-a and fixture-b accounts.
async (page) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://127.0.0.1:8771/');
  await page.waitForFunction(() => stateLoaded);
  await page.locator('[data-view="task_groups"]').click();
  await page.locator('[data-tg-create]').click();
  const editor = page.locator('#tg-editor');
  await editor.locator('[name="name"]').fill('Independent account fixture');
  await editor.locator('#tg-paste').fill('B012345678;20');
  await editor.locator('[data-tg-import]').click();
  await editor.locator('[data-tg-account-pick="fixture-a"]').check();
  await editor.locator('[data-tg-account-pick="fixture-b"]').check();
  await editor.locator('[name="action"]').selectOption('automatic');
  await editor.locator('[name="goal_mode"]').selectOption('multiple_success');
  await editor.locator('[name="target_orders"]').fill('2');
  await editor.locator('[name="max_spend"]').fill('200');
  await editor.locator('[data-tg-edit-account="fixture-a"]').click();
  const settings = page.locator('dialog[open]').last();
  for (const key of ['units_per_order','per_account_units']) {
    await settings.locator(`[data-override-key="${key}"]`).check();
    await settings.locator(`[data-override-value="${key}"]`).fill('2');
  }
  await settings.getByRole('button', {name:'Save account settings'}).click();
  await editor.getByRole('button', {name:'Save group',exact:true}).click();
  await page.waitForFunction(() => taskGroupData?.name === 'Independent account fixture');
  await page.locator('[data-tg-tab="accounts"]').first().click();
  const configured = await page.locator('#screen').innerText();
  if (!configured.includes('2 overrides') || !configured.includes('All settings inherited')) {
    throw new Error('Account inheritance is not visible');
  }
  await page.locator('[data-tg-start]').click();
  await page.waitForFunction(() => taskGroupData?.run?.state === 'completed', null, {timeout:15000});
  const result = await page.evaluate(() => ({
    progress:taskGroupData.progress,
    attempts:taskGroupData.attempts.map(a=>({account:a.account_id,units:a.units,state:a.state})),
    group:taskGroupData.id
  }));
  if (result.progress.confirmed_orders !== 2 || result.progress.confirmed_units !== 3) {
    throw new Error('Independent quantities or order goal did not match: '+JSON.stringify(result));
  }
  await page.screenshot({path:'artifacts/task-group-build/account-settings.png',fullPage:true});
  // Exercise persisted enable/disable without deleting overrides.
  await page.locator('[data-tg-settings="fixture-a"]').click();
  await page.locator('dialog[open]').last().locator('[name="enabled"]').uncheck();
  await page.locator('dialog[open]').last().getByRole('button',{name:'Save account settings'}).click();
  await page.waitForFunction(() => taskGroupData?.plan.account_settings['fixture-a'].enabled === false);
  const saved = await page.evaluate(() => taskGroupData.plan.account_settings['fixture-a']);
  if (saved.overrides.units_per_order !== 2) throw new Error('Disabling discarded overrides');
  await page.getByRole('button',{name:'Goal fulfilled',exact:true}).waitFor();
  if (await page.locator('#screen .badge').filter({hasText:/^reserved$/}).count()) throw new Error('Stale reservation status');
  if (errors.length) throw new Error(errors.join('\n'));
  return {...result, disabledSettings:saved, errors};
}
