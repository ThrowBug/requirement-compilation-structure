"""Real Chromium requests reveal duplicate mutations across generated endpoint aliases."""

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from infrastructure.stages.frontend.triggers.e2e_runtime import OBSERVATION_SOURCE


class MutationObservation(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('ARC_DB_TEST_NODE_MODULES'), 'Set ARC_DB_TEST_NODE_MODULES for real browser checks')
    def test_real_navigation_and_alias_posts_are_observed_without_response_body_races(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'node_modules').symlink_to(Path(os.environ['ARC_DB_TEST_NODE_MODULES']).resolve())
            (root / 'package.json').write_text(json.dumps({'type': 'module'}))
            (root / 'observations.ts').write_text(OBSERVATION_SOURCE)
            (root / 'check.mjs').write_text('''
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { chromium } from '@playwright/test';
import { observeMutations } from './observations.ts';
const writes = [];
const server = createServer((req, res) => {
  if (req.method === 'POST') {
    writes.push(req.url);
    req.resume(); res.setHeader('content-type', 'application/json'); res.end('{"ok":true}'); return;
  }
  const duplicate = req.url.includes('duplicate');
  res.setHeader('content-type', 'text/html');
  res.end(`<button id="submit">Submit</button><script>
    document.querySelector('button').onclick = async () => {
      await fetch('/api/traveler-accounts', {method:'POST',body:JSON.stringify({username:'alice'})});
      ${duplicate ? "await fetch('/api/accounts', {method:'POST',body:JSON.stringify({username:'alice'})});" : ''}
      location.assign('/done');
    };
  </script>`);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({headless:true});
  const page = await browser.newPage();
  for (const duplicate of [false, true]) {
    await page.goto(origin + (duplicate ? '/duplicate' : '/single'));
    const listeners = page.listenerCount('request');
    const observed = await observeMutations(page, async () => {
      await page.click('#submit'); await page.waitForURL('**/done');
    }, request => JSON.parse(request.postData() || '{}').username === 'alice');
    assert.equal(observed.length, duplicate ? 2 : 1);
    assert.equal(page.listenerCount('request'), listeners);
    assert.equal(observed[0].postDataJSON().username, 'alice');
    if (duplicate) assert.deepEqual(observed.map(request => new URL(request.url()).pathname),
      ['/api/traveler-accounts','/api/accounts']);
  }
  assert.equal(writes.length, 3);
  const listeners = page.listenerCount('request');
  await assert.rejects(observeMutations(page, async () => { throw new Error('action failed'); }), /action failed/);
  assert.equal(page.listenerCount('request'), listeners);
  await page.goto(origin + '/single');
  await assert.rejects(observeMutations(page, async () => {
    await page.click('#submit'); await page.waitForURL('**/done');
  }, () => { throw new Error('invalid observer'); }), /invalid observer/);
  assert.equal(page.listenerCount('request'), listeners);
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
''')
            compiled = subprocess.run(['node', 'node_modules/typescript/bin/tsc', '--noEmit',
                '--target', 'ES2022', '--module', 'NodeNext', '--skipLibCheck', 'observations.ts'],
                cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run(['node', '--import', 'tsx', 'check.mjs'], cwd=root,
                capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
