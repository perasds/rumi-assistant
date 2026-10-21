import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createRequire} from 'node:module';
import {prepareManualBridge, validatePageUrl, openManualPage} from './manual_captcha_host.mjs';

const require = createRequire(import.meta.url);
const {chromium} = require('playwright-core');
const url = 'https://game.bilibili.com/sdk/geetest/?challenge=synthetic-private';

test('only the HTTPS official page can host the callback', () => {
  assert.equal(validatePageUrl(url).origin, 'https://game.bilibili.com');
  for (const bad of ['http://game.bilibili.com/sdk/geetest/',
    'https://game.bilibili.com.evil.invalid/sdk/geetest/',
    'https://user:password@game.bilibili.com/sdk/geetest/',
    'https://game.bilibili.com:444/sdk/geetest/', 'file:///private',
    'https://game.bilibili.com/sdk/geetest/#private']) {
    assert.throws(() => validatePageUrl(bad), /^Error: AUTH_CAPTCHA_INVALID$/);
  }
});

async function pageWithBridge(run) {
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.RUMI_CHROMIUM_EXECUTABLE});
  try {
    const context = await browser.newContext();
    await context.route('**/*', route => route.fulfill({contentType: 'text/html',
      body: '<!doctype html><title>Synthetic callback test</title>'}));
    const page = await context.newPage();
    const completion = await prepareManualBridge(page, url, 5000);
    await page.goto(url);
    await run(page, completion);
  } finally {
    await browser.close();
  }
}

test('official bridge delivers a human result without printing or saving it', async () => {
  await pageWithBridge(async (page, completion) => {
    await page.evaluate(() => window.BiliJsObject.finishWithResult(
      '{"validate":"synthetic-private-result"}'));
    assert.deepEqual(await completion.result,
      {status: 'completed', result: '{"validate":"synthetic-private-result"}'});
  });
});

test('goBack and a window close cancel rather than complete authentication', async () => {
  await pageWithBridge(async (page, completion) => {
    await page.evaluate(() => window.BiliJsObject.goBackWithResult('ignored'));
    assert.deepEqual(await completion.result, {status: 'cancelled'});
  });
  await pageWithBridge(async (page, completion) => {
    await page.close();
    assert.deepEqual(await completion.result, {status: 'cancelled'});
  });
});

test('callbacks are absent from an iframe and rejected on another page path', async () => {
  await pageWithBridge(async (page, completion) => {
    const frameNavigation = page.waitForEvent('framenavigated', {predicate: f => f !== page.mainFrame()});
    await page.evaluate(() => {
      const frame = document.createElement('iframe');
      frame.src = 'https://game.bilibili.com/unrelated';
      document.body.append(frame);
    });
    const frame = await frameNavigation;
    assert.equal(await frame.evaluate(() => typeof window.BiliJsObject), 'undefined');
    await page.goto('https://game.bilibili.com/unrelated');
    assert.equal(await page.evaluate(() => typeof window.BiliJsObject), 'undefined');
    assert.equal(await page.evaluate(() => window.__rumiManualCaptcha(
      {method: 'finish', raw: 'private'})), false);
    completion.cancel();
    assert.deepEqual(await completion.result, {status: 'cancelled'});
  });
});

test('non-string and oversized completion cannot be treated as successful', async () => {
  for (const value of [null, 'x'.repeat(65537)]) {
    await pageWithBridge(async (page, completion) => {
      await page.evaluate(value => window.BiliJsObject.finishWithResult(value), value);
      assert.deepEqual(await completion.result,
        {status: 'failed', error_code: 'AUTH_CAPTCHA_INVALID'});
    });
  }
});

test('closing during pending navigation preserves cancellation immediately', async () => {
  const completion = {result: Promise.resolve({status: 'cancelled'})};
  const page = {goto: () => new Promise(() => {})};
  assert.deepEqual(await openManualPage(page, url, completion), {status: 'cancelled'});
});

test('HTTP error page cannot count as an available human verification page', async () => {
  const completion = {result: new Promise(() => {})};
  const page = {goto: async () => ({ok: () => false})};
  assert.deepEqual(await openManualPage(page, url, completion),
    {status: 'failed', error_code: 'AUTH_CAPTCHA_PAGE_UNAVAILABLE'});
});

test('early completion is rejected when the official page returns an HTTP error', async () => {
  const completion = {result: Promise.resolve({status: 'completed', result:'synthetic-private'})};
  const page = {goto: () => new Promise(resolve => setTimeout(() => resolve({ok: () => false}), 10))};
  assert.deepEqual(await openManualPage(page,url,completion),
    {status: 'failed', error_code: 'AUTH_CAPTCHA_PAGE_UNAVAILABLE'});
});
