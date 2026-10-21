// Private human-operated host. No solving, automatic input, traces or persistent context.
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import {Socket} from 'node:net';

export async function openManualPage(page, value, completion, onLoaded) {
  const navigation = page.goto(value, {timeout: 30000, waitUntil: 'domcontentloaded'})
    .then(async response => {
      if (!response || !response.ok()) return {status: 'failed', error_code: 'AUTH_CAPTCHA_PAGE_UNAVAILABLE'};
      if (onLoaded) await onLoaded();
      return {status: 'loaded'};
    })
    .catch(() => ({status: 'failed', error_code: 'AUTH_CAPTCHA_PAGE_UNAVAILABLE'}));
  const first = await Promise.race([completion.result, navigation]);
  if (first.status === 'completed') {
    const loaded = await navigation;
    return loaded.status === 'loaded' ? first : loaded;
  }
  return first.status === 'loaded' ? await completion.result : first;
}

export function validatePageUrl(value) {
  let url;
  try { url = new URL(value); } catch { /* fixed error below */ }
  if (typeof value !== 'string' || value.length > 16384 || !url ||
      url.protocol !== 'https:' || url.hostname !== 'game.bilibili.com' ||
      (url.port && url.port !== '443') || url.username || url.password || url.hash) {
    throw new Error('AUTH_CAPTCHA_INVALID');
  }
  return url;
}

export async function prepareManualBridge(page, value, timeoutMs) {
  const url = validatePageUrl(value);
  let complete;
  let settled = false;
  const result = new Promise(resolve => { complete = resolve; });
  const finish = report => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    complete(report);
  };
  const cancel = () => finish({status: 'cancelled'});
  const timer = setTimeout(() => finish({status: 'failed',
    error_code: 'AUTH_CAPTCHA_TIMEOUT'}), timeoutMs);
  page.on('close', cancel);
  await page.exposeBinding('__rumiManualCaptcha', async (source, handle) => {
    if (source.page !== page || source.frame !== page.mainFrame()) return false;
    let input;
    try {
      input = await handle.evaluate(value => {
        if (!value || typeof value !== 'object') return null;
        const raw = typeof value.raw === 'string' &&
          new TextEncoder().encode(value.raw).length <= 65536 ? value.raw : null;
        return {method: value.method === 'cancel' ? 'cancel' :
          value.method === 'finish' ? 'finish' : null, raw,
          origin: location.origin, path: location.pathname, top: window === window.top};
      });
    } catch { return false; }
    if (settled || !input || !input.top || input.origin !== url.origin || input.path !== url.pathname) return false;
    const {method, raw} = input;
    if (method === 'cancel') { cancel(); return true; }
    if (method !== 'finish') return false;
    if (typeof raw !== 'string' || Buffer.byteLength(raw, 'utf8') > 65536) {
      finish({status: 'failed', error_code: 'AUTH_CAPTCHA_INVALID'});
      return false;
    }
    finish({status: 'completed', result: raw});
    return true;
  }, {handle: true});
  await page.addInitScript(({origin, path}) => {
    if (window !== window.top || location.origin !== origin || location.pathname !== path) return;
    const relay = (method, result) => {
      window.__rumiManualCaptcha({method, raw: result}).catch(() => {});
    };
    window.BiliJsObject = {
      finishWithResult(result) { relay('finish', result); },
      goBackWithResult() { relay('cancel', null); },
    };
  }, {origin: url.origin, path: url.pathname});
  return {result, cancel};
}

const WIDTH = 520, HEIGHT = 900, MAX_IMAGE_BASE64 = 4 * 1024 * 1024;
const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
const RESOURCE_TYPES = ['document', 'script', 'stylesheet', 'image', 'font', 'xhr', 'fetch', 'other'];
const MAX_TIMING_COUNT = 100000, MAX_TIMING_DURATION_MS = 600000;

export function createTimingRecorder(page, now = () => performance.now()) {
  const requests = Object.fromEntries(RESOURCE_TYPES.map(type => [type,
    {completed: 0, failed: 0, pending: 0, total_ms: 0, max_ms: 0}]));
  const screenshot = {count: 0, total_ms: 0, max_ms: 0, total_bytes: 0, max_bytes: 0};
  const input = {count: 0, total_ms: 0, max_ms: 0};
  const pending = new Map();
  let count = 0, closed = false;
  const elapsed = value => Number.isFinite(value) ? Math.max(0, Math.min(MAX_TIMING_DURATION_MS, value)) : 0;
  const addDuration = (target, duration) => {
    const value = Math.round(elapsed(duration) * 1000) / 1000;
    target.total_ms = Math.round((target.total_ms + value) * 1000) / 1000;
    target.max_ms = Math.max(target.max_ms, value);
  };
  const observe = callback => value => { try { if (!closed) callback(value); } catch { /* No private exceptions. */ } };
  const started = observe(request => {
    if (count >= MAX_TIMING_COUNT || pending.has(request)) return;
    const resourceType = request.resourceType();
    pending.set(request, {type: RESOURCE_TYPES.includes(resourceType) ? resourceType : 'other', start: now()});
    count += 1;
  });
  const ended = status => observe(request => {
    const active = pending.get(request);
    if (!active) return;
    pending.delete(request);
    const target = requests[active.type];
    target[status] += 1;
    addDuration(target, now() - active.start);
  });
  const listeners = [['request', started], ['requestfinished', ended('completed')], ['requestfailed', ended('failed')]];
  try { for (const [name, listener] of listeners) page.on(name, listener); }
  catch { /* Timing cannot prevent a normal session. */ }
  return {
    recordScreenshot(duration, bytes) {
      if (closed || screenshot.count >= MAX_TIMING_COUNT) return;
      screenshot.count += 1;
      addDuration(screenshot, duration);
      const size = Number.isSafeInteger(bytes) ? Math.max(0, Math.min(MAX_IMAGE_BASE64 / 4 * 3, bytes)) : 0;
      screenshot.total_bytes += size;
      screenshot.max_bytes = Math.max(screenshot.max_bytes, size);
    },
    recordInput(duration) {
      if (closed || input.count >= MAX_TIMING_COUNT) return;
      input.count += 1;
      addDuration(input, duration);
    },
    finish() {
      if (!closed) {
        closed = true;
        for (const active of pending.values()) {
          requests[active.type].pending += 1;
          addDuration(requests[active.type], now() - active.start);
        }
        pending.clear();
        for (const [name, listener] of listeners) {
          try { page.off(name, listener); } catch { /* Owned page is closing. */ }
        }
      }
      return {kind: 'timing', requests, screenshot, input};
    },
  };
}

export async function* readControlLines(input) {
  let pending = Buffer.alloc(0), limit = 32768;
  const decoder = new TextDecoder('utf-8', {fatal: true});
  for await (const value of input) {
    const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
    let offset = 0;
    while (offset < chunk.length) {
      const newline = chunk.indexOf(10, offset);
      const end = newline === -1 ? chunk.length : newline;
      if (pending.length + end - offset > limit) throw new Error('AUTH_CAPTCHA_INVALID');
      pending = Buffer.concat([pending, chunk.subarray(offset, end)]);
      if (newline === -1) break;
      if (pending.at(-1) === 13) pending = pending.subarray(0, -1);
      yield decoder.decode(pending);
      pending = Buffer.alloc(0);
      limit = 1024;
      offset = newline + 1;
    }
  }
  if (pending.length) yield decoder.decode(pending);
}

export function createWebController(page, send, timing) {
  let queue = Promise.resolve();
  return line => {
    queue = queue.then(async () => {
      if (typeof line !== 'string' || Buffer.byteLength(line, 'utf8') > 1024) throw new Error();
      const input = JSON.parse(line);
      if (!input || typeof input !== 'object' || Array.isArray(input) ||
          typeof input.id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(input.id)) throw new Error();
      const keys = Object.keys(input).sort().join(',');
      if (input.type === 'screenshot') {
        if (keys !== 'id,type') throw new Error();
        const start = performance.now();
        let image;
        try { image = await page.screenshot({type: 'png', fullPage: false, timeout: 2000}); }
        finally { timing?.recordScreenshot(performance.now() - start, Buffer.isBuffer(image) ? image.length : 0); }
        if (!Buffer.isBuffer(image) || image.length > MAX_IMAGE_BASE64 / 4 * 3 ||
            !image.subarray(0, 8).equals(PNG_SIGNATURE)) throw new Error();
        await send({kind: 'frame', id: input.id, image: image.toString('base64'), width: WIDTH, height: HEIGHT});
        return;
      }
      if (keys !== 'id,type,x,y' || !['click', 'pointerdown', 'pointermove', 'pointerup'].includes(input.type) ||
          !Number.isFinite(input.x) || !Number.isFinite(input.y) ||
          input.x < 0 || input.x >= WIDTH || input.y < 0 || input.y >= HEIGHT) throw new Error();
      const start = performance.now();
      try {
        if (input.type === 'click') await page.mouse.click(input.x, input.y);
        else {
          await page.mouse.move(input.x, input.y);
          if (input.type === 'pointerdown') await page.mouse.down();
          if (input.type === 'pointerup') await page.mouse.up();
        }
      } finally { timing?.recordInput(performance.now() - start); }
      await send({kind: 'ack', id: input.id});
    });
    return queue;
  };
}

export async function runManualSession(chromium, input, iterator, send) {
  let browser;
  let completion;
  let timing;
  let finished = false;
  let report = {status: 'failed', error_code: 'AUTH_CAPTCHA_UNAVAILABLE'};
  try {
    if (!input || typeof input !== 'object' || Array.isArray(input) ||
        Object.keys(input).some(key => !['url', 'mode', 'executablePath', 'diagnostics'].includes(key)) ||
        (input.mode !== undefined && input.mode !== 'web') ||
        (input.diagnostics !== undefined && typeof input.diagnostics !== 'boolean')) throw new Error();
    validatePageUrl(input.url);
    const web = input.mode === 'web';
    const options = {headless: web, args: ['--window-size=520,950']};
    if (input.executablePath !== undefined && input.executablePath !== null) {
      if (typeof input.executablePath !== 'string' || !input.executablePath || input.executablePath.length > 4096) throw new Error();
      options.executablePath = input.executablePath;
    }
    browser = await chromium.launch(options);
    const context = await browser.newContext(web ?
      {acceptDownloads: false, viewport: {width: WIDTH, height: HEIGHT}, deviceScaleFactor: 1} :
      {acceptDownloads: false, viewport: null});
    const page = await context.newPage();
    if (input.diagnostics) timing = createTimingRecorder(page);
    completion = await prepareManualBridge(page, input.url, 300000);
    completion.result.then(() => { finished = true; });
    let ready = false;
    const publish = async message => { if (!finished) await send(message); };
    const dispatch = createWebController(page, publish, timing);
    const navigation = openManualPage(page, input.url, completion, web ? async () => {
      if (!finished) {
        ready = true;
        await publish({kind: 'ready', width: WIDTH, height: HEIGHT});
      }
    } : undefined);
    const controls = (async () => {
      try {
        while (true) {
          const line = await iterator.next();
          if (line.done || !web || line.value === 'cancel') {
            completion.cancel();
            return {status: 'cancelled'};
          }
          if (finished) return await navigation;
          if (!ready) throw new Error();
          await dispatch(line.value);
        }
      } catch { return {status: 'failed', error_code: 'AUTH_CAPTCHA_INVALID'}; }
    })();
    report = await Promise.race([navigation, controls]);
  } catch { /* Never echo a URL, result or Playwright exception. */ }
  finally {
    finished = true;
    let summary;
    if (timing) {
      try { summary = timing.finish(); } catch { /* Diagnostics cannot replace a result. */ }
    }
    if (completion) completion.cancel();
    if (browser) {
      try { await browser.close(); }
      catch { report = {status: 'failed', error_code: 'AUTH_CAPTCHA_UNAVAILABLE'}; }
    }
    if (summary) {
      try { await send(summary); } catch { /* The final result keeps its original semantics. */ }
    }
  }
  return report;
}

async function main() {
  if (!(process.stdin instanceof Socket) || !(process.stdout instanceof Socket) ||
      process.stdin.isTTY || process.stdout.isTTY) return;
  let report = {status: 'failed', error_code: 'AUTH_CAPTCHA_UNAVAILABLE'};
  const send = message => new Promise((done, reject) => {
    process.stdout.write(JSON.stringify(message) + '\n', error => error ? reject(error) : done());
  });
  try {
    const iterator = readControlLines(process.stdin)[Symbol.asyncIterator]();
    const first = await iterator.next();
    if (first.done) throw new Error();
    const input = JSON.parse(first.value);
    const {chromium} = await import(pathToFileURL(resolve(process.argv[2])).href);
    report = await runManualSession(chromium, input, iterator, send);
  } catch { /* Keep all private input and host exceptions off stderr. */ }
  finally { process.stdin.destroy(); }
  // stdout is a private parent pipe, never a terminal or log file.
  await send(report).catch(() => {});
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await main();
}
