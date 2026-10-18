import { expect, test } from '@playwright/test';
const token = process.env.WEB_TEST_TOKEN;
test.skip(!token, 'Requires isolated Web fixture');
test('new release is shown and upgrade requires explicit confirmation; reconnect never resubmits', async ({
  page,
}) => {
  let submissions = 0;
  const status = {
    enabled: true,
    current: '0.5.1',
    candidate: { version: '0.5.2', asset_id: 12, size: 42, digest: 'sha256:' + 'a'.repeat(64) },
    checked_at: new Date().toISOString(),
    error: '',
    execution: null as null | { version: string; phase: string; message: string },
  };
  await page.route('**/api/v1/updates**', async (route) => {
    if (route.request().url().endsWith('/upgrade')) {
      submissions++;
      expect(route.request().postDataJSON()).toEqual({ version: '0.5.2' });
      status.execution = { version: '0.5.2', phase: 'queued', message: '升级请求已排队' };
      await route.fulfill({ status: 202, json: status });
    } else
      await route.fulfill({
        status: route.request().method() === 'POST' ? 202 : 200,
        json: status,
      });
  });
  await page.goto('/');
  await page.getByRole('textbox', { name: '管理令牌' }).fill(token!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByText('发现新版本 0.5.2')).toBeVisible();
  expect(submissions).toBe(0);
  await page.getByRole('button', { name: '升级到 0.5.2' }).click();
  await expect(page.getByRole('dialog', { name: '升级平台' })).toBeVisible();
  expect(submissions).toBe(0);
  await page.getByRole('button', { name: '确认升级', exact: true }).click();
  await expect(page.getByText('升级请求已排队', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '升级到 0.5.2' })).toBeDisabled();
  await page.reload();
  await page.getByRole('textbox', { name: '管理令牌' }).fill(token!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByText('升级请求已排队', { exact: true })).toBeVisible();
  expect(submissions).toBe(1);
});
