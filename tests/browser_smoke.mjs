import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { chromium } from 'playwright';

const app = spawn('python', ['-u', 'START_CONTEXT.py', '--demo', '--no-browser'], {stdio: ['ignore', 'pipe', 'inherit']});
const browser = await chromium.launch();
try {
  const output = createInterface({input: app.stdout});
  let url;
  for await (const line of output) {
    if (line.startsWith('http://')) { url = line.trim(); break; }
  }
  assert.ok(url, 'The standalone server should print its address');
  for (const width of [1360, 390]) {
    const page = await browser.newPage({ viewport: {width, height: 850} });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('.mail-item').first().waitFor();
    assert.equal(await page.locator('#letter').isVisible(), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: '/tmp/context-inbox-' + width + '.png'});
    if (width < 760) await page.getByRole('button', {name: 'Ящики и папки'}).click();
    await page.locator('#mailboxes button', {hasText: 'Поездки'}).click();
    await page.getByRole('searchbox').fill('Где писали, что самолёт улетит позже?');
    await page.getByRole('searchbox').press('Enter');
    await page.locator('.mail-item[data-doc="f-401"]').first().waitFor();
    assert.equal(await page.locator('.mail-item').first().getAttribute('data-doc'), 'f-401');
    await page.locator('.mail-item').first().click();
    await page.locator('#letter:visible').waitFor();
    assert.match(await page.locator('#evidence-text').innerText(), /18:40/);
    await page.locator('#save').click();
    assert.equal(await page.locator('#save').getAttribute('aria-pressed'), 'true');
    await page.locator('#back').click();
    await page.getByRole('button', {name: 'По словам',exact:true}).click();
    await page.locator('.empty-items').waitFor();
    if (width < 760) await page.getByRole('button', {name: 'Ящики и папки'}).click();
    await page.locator('#saved-mail').click();
    await page.locator('.mail-item[data-doc="f-401"]').waitFor();
    assert.deepEqual(errors, []);
    await page.close();
    console.log('PASS browser viewport ' + width);
  }
} finally {
  app.kill();
  await browser.close();
}
