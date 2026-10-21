import assert from 'node:assert/strict';
import {test} from 'node:test';
import {Readable} from 'node:stream';
import {EventEmitter} from 'node:events';
import * as host from './manual_captcha_host.mjs';

const url = 'https://game.bilibili.com/sdk/geetest/?challenge=synthetic-private';
const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 0]);
const command = (type, extra = {}) => JSON.stringify({id: 'synthetic_1', type, ...extra});
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
};

function fakePage() {
  const calls = [];
  const frame = {};
  return {calls, mainFrame: () => frame, on() {}, async exposeBinding() {}, async addInitScript() {},
    async goto() { return {ok: () => true}; },
    async screenshot(options) { calls.push(['screenshot', options]); return png; },
    mouse: Object.fromEntries(['click', 'move', 'down', 'up'].map(name =>
      [name, async (...args) => { calls.push([name, ...args]); }]))};
}

function controller(page, sent) {
  assert.equal(typeof host.createWebController, 'function', 'Web control dispatcher is missing');
  return host.createWebController(page, message => { sent.push(message); });
}

test('Web screenshot returns a bounded PNG frame for the matching command', async () => {
  const page = fakePage(), sent = [];
  await controller(page, sent)(command('screenshot'));
  assert.deepEqual(sent, [{kind: 'frame', id: 'synthetic_1', image: png.toString('base64'), width: 520, height: 900}]);
  assert.deepEqual(page.calls, [['screenshot', {type: 'png', fullPage: false, timeout: 2000}]]);
});

test('Web mouse controls forward only the supplied coordinates and acknowledge once', async () => {
  const page = fakePage(), sent = [], dispatch = controller(page, sent);
  for (const type of ['pointerdown', 'pointermove', 'pointerup', 'click']) {
    await dispatch(command(type, {x: 12.5, y: 899}));
  }
  assert.deepEqual(page.calls, [['move', 12.5, 899], ['down'], ['move', 12.5, 899],
    ['move', 12.5, 899], ['up'], ['click', 12.5, 899]]);
  assert.deepEqual(sent, Array.from({length: 4}, () => ({kind: 'ack', id: 'synthetic_1'})));
});

test('concurrent Web commands cannot interleave mouse operations', async () => {
  const page = fakePage(), sent = [], gate = deferred(), started = deferred();
  page.mouse.move = async (...args) => {
    page.calls.push(['move-start', ...args]); started.resolve(); await gate.promise;
    page.calls.push(['move-end']);
  };
  const dispatch = controller(page, sent);
  const down = dispatch(command('pointerdown', {x: 1, y: 2}));
  await started.promise;
  const click = dispatch(command('click', {x: 3, y: 4}));
  await Promise.resolve();
  assert.deepEqual(page.calls, [['move-start', 1, 2]]);
  gate.resolve();
  await Promise.all([down, click]);
  assert.deepEqual(page.calls, [['move-start', 1, 2], ['move-end'], ['down'], ['click', 3, 4]]);
  assert.equal(sent.length, 2);
});

test('invalid Web commands have no mouse or screenshot side effects', async () => {
  const invalid = [null, [], {}, {id: '', type: 'screenshot'}, {id: 'bad id', type: 'screenshot'},
    {id: 'a', type: 'unknown'}, {id: 'a', type: 'screenshot', x: 0},
    ...[NaN, Infinity, -1, 520, '1', null, true].map(x => ({id: 'a', type: 'click', x, y: 1})),
    {id: 'a', type: 'click', x: 1, y: 900}, {id: 'a', type: 'click', x: 1, y: 2, extra: 1}];
  for (const value of invalid) {
    const page = fakePage(), sent = [];
    await assert.rejects(controller(page, sent)(JSON.stringify(value)));
    assert.deepEqual(page.calls, []);
    assert.deepEqual(sent, []);
  }
  await assert.rejects(controller(fakePage(), [])(' '.repeat(1025)));
});

test('an oversized or non-PNG screenshot is rejected without emitting a frame', async () => {
  for (const data of [Buffer.alloc(3 * 1024 * 1024 + 1), Buffer.from('not-png')]) {
    const page = fakePage(), sent = [];
    page.screenshot = async () => data;
    await assert.rejects(controller(page, sent)(command('screenshot')));
    assert.deepEqual(sent, []);
  }
});

test('private pipe reader enforces byte limits across chunks before parsing', async () => {
  assert.equal(typeof host.readControlLines, 'function', 'Bounded private pipe reader is missing');
  const first = JSON.stringify({url, mode: 'web'});
  const lines = [];
  for await (const line of host.readControlLines(Readable.from([first.slice(0, 5), first.slice(5) + '\r\n', command('screenshot')]))) {
    lines.push(line);
  }
  assert.deepEqual(lines, [first, command('screenshot')]);
  for (const chunks of [['x'.repeat(32769)], [first + '\n', '中'.repeat(342)]]) {
    await assert.rejects(async () => { for await (const line of host.readControlLines(Readable.from(chunks))) void line; });
  }
});

function fakeBrowser(page) {
  const launched = [], contexts = [];
  let closed = 0;
  return {launched, contexts, get closed() { return closed; }, chromium: {
    async launch(options) { launched.push(options); return {
      async newContext(options) { contexts.push(options); return {async newPage() { return page; }}; },
      async close() { closed += 1; },
    }; },
  }};
}

test('passive timing aggregates fixed resource types without reading private request data', () => {
  assert.equal(typeof host.createTimingRecorder, 'function');
  const page = new EventEmitter();
  let clock = 10;
  const recorder = host.createTimingRecorder(page, () => clock);
  const request = type => ({resourceType: () => type,
    url() { assert.fail('Timing must not inspect URLs'); },
    headers() { assert.fail('Timing must not inspect headers'); },
    postData() { assert.fail('Timing must not inspect bodies'); }});
  const document = request('document'), xhr = request('xhr'), font = request('font'), other = request('synthetic-private');
  for (const value of [document, xhr, font, other]) page.emit('request', value);
  clock = 110; page.emit('requestfinished', document);
  clock = 210; page.emit('requestfailed', xhr);
  clock = 310;
  recorder.recordScreenshot(12.5, 64);
  recorder.recordInput(2);
  const value = recorder.finish();
  assert.deepEqual(Object.keys(value).sort(), ['input', 'kind', 'requests', 'screenshot']);
  assert.deepEqual(Object.keys(value.requests).sort(), ['document', 'fetch', 'font', 'image', 'other', 'script', 'stylesheet', 'xhr']);
  assert.deepEqual(value.requests.document, {completed: 1, failed: 0, pending: 0, total_ms: 100, max_ms: 100});
  assert.deepEqual(value.requests.xhr, {completed: 0, failed: 1, pending: 0, total_ms: 200, max_ms: 200});
  assert.deepEqual(value.requests.font, {completed: 0, failed: 0, pending: 1, total_ms: 300, max_ms: 300});
  assert.deepEqual(value.requests.other, value.requests.font);
  assert.deepEqual(value.screenshot, {count: 1, total_ms: 12.5, max_ms: 12.5, total_bytes: 64, max_bytes: 64});
  assert.deepEqual(value.input, {count: 1, total_ms: 2, max_ms: 2});
  assert.equal(JSON.stringify(value).includes('synthetic-private'), false);
  assert.ok(Buffer.byteLength(JSON.stringify(value)) < 4096);
  assert.equal(page.listenerCount('request'), 0);
});

test('diagnostics observe only existing Web actions and emit at most one summary', async () => {
  const page = fakePage(), browser = fakeBrowser(page), ready = deferred(), sent = [];
  const input = async function* () { await ready.promise; yield command('screenshot'); yield command('click', {x: 1, y: 2}); yield 'cancel'; };
  const report = await host.runManualSession(browser.chromium, {url, mode: 'web', diagnostics: true}, input()[Symbol.asyncIterator](), message => {
    sent.push(message); if (message.kind === 'ready') ready.resolve();
  });
  assert.deepEqual(report, {status: 'cancelled'});
  assert.equal(browser.closed, 1);
  assert.deepEqual(sent.map(value => value.kind), ['ready', 'frame', 'ack', 'timing']);
  assert.deepEqual(page.calls.map(value => value[0]), ['screenshot', 'click']);
  assert.equal(sent[3].screenshot.count, 1);
  assert.equal(sent[3].screenshot.total_bytes, png.length);
  assert.equal(sent[3].input.count, 1);
  assert.equal(JSON.stringify(sent[3]).includes('synthetic'), false);
});

test('local diagnostics emit only timing and do not automate input or take screenshots', async () => {
  const page = fakePage(), browser = fakeBrowser(page), sent = [];
  const input = async function* () { yield 'cancel'; };
  const report = await host.runManualSession(browser.chromium, {url, diagnostics: true}, input()[Symbol.asyncIterator](), message => sent.push(message));
  assert.deepEqual(report, {status: 'cancelled'});
  assert.deepEqual(sent.map(value => value.kind), ['timing']);
  assert.equal(sent[0].input.count, 0);
  assert.equal(sent[0].screenshot.count, 0);
  assert.deepEqual(page.calls, []);
});

test('timing write failure cannot change the normal session result', async () => {
  const page = fakePage(), browser = fakeBrowser(page), sent = [];
  const input = async function* () { yield 'cancel'; };
  const report = await host.runManualSession(browser.chromium, {url, diagnostics: true}, input()[Symbol.asyncIterator](), message => {
    sent.push(message); throw new Error('synthetic-private-pipe-error');
  });
  assert.deepEqual(report, {status: 'cancelled'});
  assert.equal(browser.closed, 1);
  assert.equal(sent.length, 1);
});

test('diagnostics option is strictly boolean before launching a browser', async () => {
  for (const diagnostics of [null, 1, 'true', {}, []]) {
    const browser = fakeBrowser(fakePage());
    const report = await host.runManualSession(browser.chromium, {url, diagnostics}, {async next() { return {done: true}; }}, () => assert.fail('Unexpected message'));
    assert.deepEqual(report, {status: 'failed', error_code: 'AUTH_CAPTCHA_UNAVAILABLE'});
    assert.equal(browser.launched.length, 0);
  }
});

test('Web session becomes ready after navigation, then cancels and closes its owned browser', async () => {
  assert.equal(typeof host.runManualSession, 'function', 'Web session lifecycle is missing');
  const page = fakePage(), browser = fakeBrowser(page), ready = deferred(), sent = [];
  const input = async function* () { await ready.promise; yield command('screenshot'); yield 'cancel'; };
  const report = await host.runManualSession(browser.chromium, {url, mode: 'web'}, input()[Symbol.asyncIterator](), message => {
    sent.push(message); if (message.kind === 'ready') ready.resolve();
  });
  assert.deepEqual(report, {status: 'cancelled'});
  assert.equal(browser.closed, 1);
  assert.equal(browser.launched[0].headless, true);
  assert.equal(Object.hasOwn(browser.launched[0], 'executablePath'), false);
  assert.deepEqual(browser.contexts, [{acceptDownloads: false, viewport: {width: 520, height: 900}, deviceScaleFactor: 1}]);
  assert.equal(sent[0].kind, 'ready');
  assert.equal(sent[1].kind, 'frame');
});

test('local session keeps a visible window and second-line or EOF cancellation', async () => {
  assert.equal(typeof host.runManualSession, 'function', 'Session lifecycle is missing');
  for (const executablePath of [undefined, null, 'synthetic-browser']) {
    const browser = fakeBrowser(fakePage());
    const input = async function* () { yield 'anything-cancels-local'; };
    const report = await host.runManualSession(browser.chromium, {url, executablePath}, input()[Symbol.asyncIterator](), () => assert.fail('Local mode emitted Web messages'));
    assert.deepEqual(report, {status: 'cancelled'});
    assert.equal(browser.launched[0].headless, false);
    assert.deepEqual(browser.contexts, [{acceptDownloads: false, viewport: null}]);
    assert.equal(browser.closed, 1);
    assert.equal(Object.hasOwn(browser.launched[0], 'executablePath'), typeof executablePath === 'string');
  }
});

test('Web EOF and invalid controls close the browser without disclosing private values', async () => {
  assert.equal(typeof host.runManualSession, 'function', 'Session lifecycle is missing');
  for (const invalid of [false, true]) {
    const page = fakePage(), browser = fakeBrowser(page), ready = deferred();
    const input = async function* () { await ready.promise; if (invalid) yield '{private-invalid'; };
    const report = await host.runManualSession(browser.chromium, {url, mode: 'web'}, input()[Symbol.asyncIterator](), message => {
      if (message.kind === 'ready') ready.resolve();
    });
    assert.deepEqual(report, invalid ? {status: 'failed', error_code: 'AUTH_CAPTCHA_INVALID'} : {status: 'cancelled'});
    assert.equal(browser.closed, 1);
    assert.equal(JSON.stringify(report).includes('private'), false);
  }
});

test('a queued Web command cannot accept an early callback from a failed navigation', async () => {
  const page = fakePage(), browser = fakeBrowser(page), called = deferred();
  let bridge;
  page.exposeBinding = async (_name, callback) => { bridge = callback; };
  page.goto = async () => {
    await bridge({page, frame: page.mainFrame()}, {evaluate: async () => ({method: 'finish',
      raw: 'synthetic-private-result', origin: 'https://game.bilibili.com', path: '/sdk/geetest/', top: true})});
    called.resolve();
    await new Promise(done => setTimeout(done, 20));
    return {ok: () => false};
  };
  const iterator = {async next() { await called.promise; return {value: command('screenshot'), done: false}; }};
  const report = await host.runManualSession(browser.chromium, {url, mode: 'web'}, iterator,
    () => assert.fail('An HTTP error page cannot become ready'));
  assert.deepEqual(report, {status: 'failed', error_code: 'AUTH_CAPTCHA_PAGE_UNAVAILABLE'});
  assert.equal(browser.closed, 1);
});
