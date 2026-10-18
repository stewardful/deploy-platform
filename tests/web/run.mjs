// Starts isolated real Go/Rust processes for browser tests; runtime is in-memory only.
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
const root = process.cwd();
const temp = await mkdtemp('/tmp/cloud-web-');
const token = randomBytes(32).toString('hex');
await writeFile(`${temp}/token`, token, { mode: 0o600 });
const reservation = createServer();
await new Promise((r) => reservation.listen(0, '127.0.0.1', r));
const port = reservation.address().port;
await new Promise((r) => reservation.close(r));
const children = [];
let exitCode = 1;
try {
  children.push(
    spawn(
      resolve(root, 'target/debug/examples/api_fixture'),
      [`${temp}/state`, `${temp}/run/api.sock`],
      { stdio: 'inherit' },
    ),
  );
  children.push(
    spawn(
      resolve(root, 'target/controlplane/controld'),
      [
        '--listen',
        `127.0.0.1:${port}`,
        '--agent-socket',
        `${temp}/run/api.sock`,
        '--token-file',
        `${temp}/token`,
        '--web-dir',
        resolve(root, 'apps/web/dist'),
      ],
      { stdio: 'inherit' },
    ),
  );
  for (const child of children) child.on('error', (error) => console.error(error.message));
  const baseURL = `http://127.0.0.1:${port}`;
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try {
      const response = await fetch(`${baseURL}/api/v1/health`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (response.ok) {
        ready = true;
        break;
      }
    } catch {}
    await delay(100);
  }
  if (!ready) throw Error('Test control API did not become ready');
  const test = spawn(
    process.execPath,
    [resolve(root, 'node_modules/@playwright/test/cli.js'), 'test', ...process.argv.slice(2)],
    { stdio: 'inherit', env: { ...process.env, WEB_TEST_URL: baseURL, WEB_TEST_TOKEN: token } },
  );
  exitCode = await new Promise((r, j) => {
    test.on('exit', (code) => r(code ?? 1));
    test.on('error', j);
  });
} finally {
  for (const child of children.reverse()) {
    if (child.exitCode === null) {
      child.kill('SIGTERM');
      await Promise.race([
        new Promise((r) => child.once('exit', r)),
        delay(5000, undefined, { ref: false }).then(() => {
          if (child.exitCode === null) child.kill('SIGKILL');
        }),
      ]);
    }
  }
  await rm(temp, { recursive: true, force: true });
}
process.exitCode = exitCode;
