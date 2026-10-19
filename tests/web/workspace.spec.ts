import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';
const token = process.env.WEB_TEST_TOKEN;
test.skip(!token, 'Run with the isolated Go + Rust test fixture and WEB_TEST_TOKEN');
test('browser manages applications, retains task IDs and replays an uncertain submission', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '连接你的工作台' })).toBeVisible();
  await page.getByRole('textbox', { name: '管理令牌' }).fill(token!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '应用管理', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '部署应用', exact: true }).click();
  await page.getByRole('button', { name: '载入 Web 示例' }).click();
  await page.getByRole('button', { name: '提交部署' }).click();
  await expect(
    page.getByRole('dialog').filter({ has: page.getByText('执行结果', { exact: true }) }),
  ).toBeVisible();
  const dialog = page
    .getByRole('dialog')
    .filter({ has: page.getByText('执行结果', { exact: true }) });
  await expect(dialog.getByText('已完成', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('row').filter({ hasText: 'demo' })).toContainText('web');
  const row = page.getByRole('row').filter({ hasText: 'demo' });
  const search = page.getByRole('textbox', { name: '搜索应用或项目' });
  await search.fill('missing-project');
  await expect(page.getByText('没有找到匹配的应用', { exact: true })).toBeVisible();
  await search.fill('');
  await expect(row).toBeVisible();
  await row.getByRole('button', { name: '诊断', exact: true }).click();
  const diagnostics = page.getByRole('dialog', { name: '应用诊断', exact: true });
  await expect(diagnostics.getByText('HTTP 健康', { exact: true })).toBeVisible();
  await expect(diagnostics.getByLabel('应用日志')).toContainText('<script>text only</script>');
  await expect(diagnostics.getByText('dp-project-demo', { exact: true })).toBeVisible();
  await diagnostics
    .locator('.application-logs')
    .evaluate((node) =>
      node.getAnimations({ subtree: true }).forEach((animation) => animation.finish()),
    );
  await page.screenshot({
    path: 'test-results/application-diagnostics.png',
    fullPage: true,
    animations: 'disabled',
  });
  await diagnostics.getByRole('button', { name: '关闭', exact: true }).click();
  await row.getByRole('button', { name: '停止', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '停止应用', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('已完成', { exact: true })).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
  await expect(row.getByText('已停止', { exact: true })).toBeVisible();
  // Agent accepts start, but the browser loses its response. Retry must reuse the key.
  let requestID = '';
  let acceptedID = 0;
  let responseLost = false;
  await page.route(
    '**/api/v1/apps/demo/web/start',
    async (route) => {
      requestID = route.request().postDataJSON().request_id;
      const response = await route.fetch();
      acceptedID = (await response.json()).id;
      await route.abort('connectionfailed');
      responseLost = true;
    },
    { times: 1 },
  );
  await row.getByRole('button', { name: '启动', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '启动应用', exact: true }).click();
  // The pending banner exists before sending; wait for this deliberate network
  // failure to finish before reload, so the captured accepted ID is authoritative.
  await expect.poll(() => responseLost).toBe(true);
  expect(acceptedID).toBeGreaterThan(0);
  await expect(page.getByText('有一笔提交尚未确认', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '部署应用', exact: true })).toBeDisabled();
  await page.reload();
  const stored = await page.evaluate(() => JSON.stringify({ ...sessionStorage }));
  expect(stored).not.toContain(token!);
  await page.getByRole('textbox', { name: '管理令牌' }).fill(token!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByText('有一笔提交尚未确认', { exact: true })).toBeVisible();
  const retryRequest = page.waitForRequest('**/api/v1/apps/demo/web/start');
  await page.getByRole('button', { name: '使用原请求确认' }).click();
  expect((await retryRequest).postDataJSON().request_id).toBe(requestID);
  await expect(page.getByRole('dialog')).toContainText(`任务 #${acceptedID}`);
  await expect(page.getByRole('dialog').getByText('已完成', { exact: true })).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '任务中心', exact: true }).click();
  await expect(page.getByRole('row').filter({ hasText: `#${acceptedID}` })).toBeVisible();
  await page.getByRole('textbox', { name: '任务 ID' }).fill('999999');
  await page.getByRole('button', { name: '查询任务' }).click();
  await expect(page.getByText('未找到该任务，请核对任务 ID。', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '任务 ID' }).fill(String(acceptedID));
  await page.getByRole('button', { name: '查询任务' }).click();
  await expect(page.getByRole('dialog')).toContainText(`任务 #${acceptedID}`);
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
  await page.screenshot({ path: 'test-results/tasks-desktop.png', fullPage: true });
  await page.getByRole('button', { name: /应用管理/ }).click();
  await page.screenshot({ path: 'test-results/apps-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page
      .getByRole('region', { name: 'demo/web', exact: true })
      .getByText('进程已启动', { exact: true }),
  ).toBeVisible();
  await page.screenshot({ path: 'test-results/apps-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  expect(errors).toEqual([]);
});

test('write-only secrets and PostgreSQL template keep values out of browser storage', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByRole('textbox', { name: '管理令牌' }).fill(process.env.WEB_TEST_TOKEN!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '应用管理', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '数据与密钥', exact: true }).click();
  const project = `secret-${Date.now()}`;
  const secretValue = `value-only-in-memory-${Date.now()}`;
  await page.getByLabel('项目名称', { exact: true }).fill(project);
  await page.getByLabel('新密钥名称', { exact: true }).fill('db-password');
  await page.getByLabel('密钥内容', { exact: true }).fill(secretValue);
  let savedResponse: { status: number; body: string } | undefined;
  await page.route('**/api/v1/secrets', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    // Capture the real server response before delivery, independent of Chromium's response cache.
    const response = await route.fetch();
    savedResponse = { status: response.status(), body: await response.text() };
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: '保存密钥', exact: true }).click();
  await expect.poll(() => savedResponse).toBeDefined();
  expect(savedResponse!.status).toBe(202);
  expect(savedResponse!.body).not.toContain(secretValue);
  await expect(page.getByLabel('密钥内容', { exact: true })).toHaveValue('');
  await page.screenshot({ path: 'test-results/secrets-panel.png', fullPage: true });
  expect(
    await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage })),
  ).not.toContain(secretValue);
  await page.getByRole('button', { name: /^应用管理/ }).click();
  await page.getByRole('button', { name: '部署应用', exact: true }).click();
  await page.getByRole('button', { name: '载入 PostgreSQL 模板' }).click();
  await page.getByLabel('项目名称', { exact: true }).fill(project);
  await expect(page.getByLabel('数据卷路径 1')).toHaveValue('/var/lib/postgresql/data');
  await expect(page.getByLabel('密钥引用（逗号分隔名称）')).toHaveValue('db-password');
  await page.getByLabel('数据卷路径 1').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/stateful-deploy.png', fullPage: true });
  const deployment = page.waitForResponse((r) => r.url().endsWith('/api/v1/apps/apply'));
  await page.getByRole('button', { name: '提交部署', exact: true }).click();
  expect((await deployment).status()).toBe(202);
  expect(
    await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage })),
  ).not.toContain(secretValue);
});

test('domain bindings are durable, editable and removable from the Web', async ({ page }) => {
  // This case owns its fixture; it must also pass alone or under repeated execution.
  const headers = { Authorization: `Bearer ${token!}` };
  const deployed = await page.request.post('/api/v1/apps/apply', {
    headers,
    data: {
      request_id: randomUUID(),
      spec: {
        schema_version: 1,
        app: { project: 'domain-e2e', name: 'web' },
        image: 'docker.io/library/nginx:stable-alpine',
        resources: { memory_mib: 128, cpu_millis: 500, pids: 64 },
        network: 'project',
      },
    },
  });
  expect(deployed.status()).toBe(202);
  const job = await deployed.json();
  await expect
    .poll(async () => {
      const response = await page.request.get(`/api/v1/jobs/${job.id}`, { headers });
      expect(response.ok()).toBeTruthy();
      return (await response.json()).state;
    })
    .toBe('succeeded');
  // Deliberately hold the first apps response: opening the panel need not mean
  // the asynchronous app list has loaded or supplied the form's initial value.
  let releaseApps!: () => void;
  const appsReady = new Promise<void>((resolve) => {
    releaseApps = resolve;
  });
  await page.route(
    '**/api/v1/apps',
    async (route) => {
      await appsReady;
      await route.continue();
    },
    { times: 1 },
  );
  await page.goto('/');
  await page.getByRole('textbox', { name: '管理令牌' }).fill(token!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await page.getByRole('button', { name: '域名与 HTTPS', exact: true }).click();
  await expect(page.getByRole('heading', { name: '域名与 HTTPS', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '绑定域名', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '域名绑定', exact: true });
  await expect(dialog).toBeVisible();
  releaseApps();
  await expect(dialog).not.toHaveClass(
    /(?:^|\s)t-dialog-zoom-(?:enter|appear)(?:-active)?(?:\s|$)/,
  );
  await dialog.getByLabel('公网域名', { exact: true }).fill('web.example.com');
  await dialog.getByRole('textbox', { name: '目标应用', exact: true }).click();
  await page.getByText('domain-e2e / web', { exact: true }).click();
  await expect(dialog.getByRole('textbox', { name: '目标应用', exact: true })).toHaveValue(
    'domain-e2e / web',
  );
  await dialog.getByRole('button', { name: '保存绑定', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const card = page.locator('.domain-card').filter({ hasText: 'web.example.com' });
  await expect(card.getByText('代理已同步', { exact: true })).toBeVisible({ timeout: 15000 });
  await page.screenshot({
    path: 'test-results/domain-bindings.png',
    fullPage: true,
    animations: 'disabled',
  });
  await card.getByRole('button', { name: '查看证书', exact: true }).click();
  const cert = page.getByRole('dialog', { name: 'HTTPS 证书', exact: true });
  await expect(cert.getByText('尚未获取到证书', { exact: true })).toBeVisible();
  await cert.getByRole('button', { name: '关闭', exact: true }).click();
  await card.getByRole('button', { name: '停用', exact: true }).click();
  await expect(card.getByText('已停用', { exact: true })).toBeVisible();
  await card.getByRole('button', { name: '启用', exact: true }).click();
  await expect(card.getByRole('button', { name: '停用', exact: true })).toBeVisible();
  await card.getByRole('button', { name: '编辑', exact: true }).click();
  await page.getByRole('dialog').getByLabel('容器 HTTP 端口', { exact: true }).fill('8081');
  await page.getByRole('dialog').getByRole('button', { name: '保存绑定', exact: true }).click();
  await expect(card).toContainText('容器端口 8081');
  await card.getByRole('button', { name: '移除', exact: true }).click();
  await page
    .getByRole('dialog', { name: '移除域名', exact: true })
    .getByRole('button', { name: '移除绑定', exact: true })
    .click();
  await expect(card).not.toBeVisible();
});
