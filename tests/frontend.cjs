const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/../htdocs/luci-static/resources/view/e5-esim.js', 'utf8');

class Node {
    constructor(tag, attrs, children) {
        this.tag = tag;
        this.attrs = attrs || {};
        this.tagName = tag.toUpperCase();
        this.disabled = this.attrs.disabled != null;
        this.children = [];
        this.classList = { toggle() {} };
        this.style = {};
        this.appendChild(children);
    }
    appendChild(child) {
        if (Array.isArray(child)) child.forEach(value => this.appendChild(value == null ? String(value) : value));
        else if (child != null) this.children.push(child);
    }
    hasAttribute(name) { return Object.hasOwn(this.attrs, name); }
    setAttribute(name, value) { this.attrs[name] = String(value); }
    removeAttribute(name) { delete this.attrs[name]; }
    focus() { this.attrs.focused = ''; }
    replaceChildren(...children) { this.children = []; this.appendChild(children); }
    get textContent() { return this.children.map(value => value instanceof Node ? value.textContent : String(value)).join(''); }
    set textContent(value) { this.children = [value]; }
}
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
function matching(node, predicate) {
    if (!(node instanceof Node)) return [];
    return (predicate(node) ? [node] : []).concat(node.children.flatMap(child => matching(child, predicate)));
}
function harness(respond) {
    const calls = [], timers = new Map(), notices = [];
    let timer = 0, modal = null;
    const api = vm.runInNewContext('(function(){' + source + '\n})()', {
        E: (tag, attrs, children) => new Node(tag, attrs, children),
        L: { env: { lang: 'en' }, hasViewPermission: () => true },
        document: { documentElement: { lang: 'en' } },
        view: { extend: value => value },
        rpc: { declare: spec => (...args) => {
            calls.push([spec.method, ...args]);
            return Promise.resolve(respond(spec.method, args));
        } },
        ui: { showModal(title, children, ...classes) { modal = new Node('dialog', { class: classes.join(' ') }, children); }, hideModal() { modal = null; }, addNotification(title, content) { notices.push(content.textContent); } },
        window: { setTimeout(fn) { timers.set(++timer, fn); return timer; }, clearTimeout(id) { timers.delete(id); } }
    });
    let root;
    return {
        calls, notices, api, get root() { return root; }, get modal() { return modal; },
        get pendingTimers() { return timers.size; },
        async open(job = null, deps = null) { root = api.render({ job, deps }); await flush(); },
        async openViaLoad() { const loaded = await api.load(); root = api.render(loaded); await flush(); return loaded; },
        async click(text) {
            function find(node) {
                if (!(node instanceof Node)) return null;
                if ((node.tag === 'button' || node.attrs.role === 'button') && (node.textContent === text || node.attrs['aria-label'] === text)) return node;
                for (const child of node.children) { const match = find(child); if (match) return match; }
                return null;
            }
            const button = find(modal) || find(root);
            assert.ok(button, 'Missing button: ' + text);
            assert.equal(button.disabled, false, 'Disabled button: ' + text);
            button.attrs.click({ preventDefault() {} }); await flush();
        },
        async tick() { const entry = timers.entries().next().value; assert.ok(entry); timers.delete(entry[0]); entry[1](); await flush(); }
    };
}
const good = data => ({ ok: true, data });
const profiles = [{ iccid: '123', profileName: 'Test', profileState: 'disabled' }];
const snapshot = { ok: true, chip: good({ eidValue: 'EID' }), profiles: good(profiles), notifications: good([]), aid: '' };

(async () => {
    const h = harness((method, args) => {
        if (method === 'snapshot') return args[0] === 0 ? { ok: false, error: 'euicc_init' } : snapshot;
        if (method === 'job_status') return { running: false, state: 'done', data: profiles };
        return good(null);
    });
    await h.open();
    assert.equal(h.root.textContent.includes('null'), false);
    assert.deepEqual(h.calls.filter(c => c[0] === 'snapshot'), [['snapshot', 0, '', true], ['snapshot', 1, '', true]]);
    await h.click('SIM 1');
    assert.equal(h.calls.filter(c => c[0] === 'snapshot').length, 3);
    await h.click('SIM 2');
    await h.click('Test');
    assert.equal(h.modal.attrs.class, 'e5-esim-modal');
    const actions = matching(h.modal, node => node.attrs.class === 'right e5-actions');
    assert.equal(actions.length, 1);
    assert.equal(actions[0].children[0].textContent, 'Cancel');
    assert.equal(actions[0].children.filter(node => node.tag === 'button').length, 4);
    await h.click('Enable');
    await h.click('Enable');
    await h.tick();
    assert.deepEqual(h.calls.slice(-2).map(c => c[0]), ['operation_start', 'job_status']);
    const request = JSON.parse(h.calls.at(-2)[2]);
    assert.equal(request.action, 'switch');
    assert.equal(request.enabled, true);

    const details = harness(method => method === 'snapshot' ? { ...snapshot, chip: good({
        eidValue: 'EID', EUICCInfo2: { euiccCiPKIdListForSigning: ['key-a', 'key-b'], euiccCiPKIdListForVerification: ['key-c'] }
    }) } : good(null));
    await details.open();
    await details.click('Details ›');
    const rows = matching(details.root, node => node.attrs.class === 'cbi-value e5-info-row');
    assert.equal(rows.length, 10);
    const signing = rows.find(row => row.children[0].textContent === 'Signing certificate key IDs');
    assert.equal(signing.children[1].children.length, 2);
    assert.equal(signing.children[1].textContent, 'key-akey-b');
    details.api.unload();

    const enabled = harness(method => method === 'snapshot'
        ? { ...snapshot, profiles: good([{ ...profiles[0], profileState: 'enabled' }]) }
        : good(null));
    await enabled.open();
    await enabled.click('Test');
    assert.equal(enabled.modal.textContent.includes('null'), false);
    enabled.api.unload();

    const resumed = harness(method => {
        if (method === 'job_status') return { running: false, state: 'done', data: profiles };
        if (method === 'snapshot') return snapshot;
        return good(null);
    });
    await resumed.open({ type: 'operation', running: true, slot: 1, action: 'rename' });
    assert.equal(resumed.calls.length, 0);
    await resumed.tick();
    assert.ok(resumed.root.textContent.includes('Test'));
    assert.equal(resumed.calls.filter(call => call[0] === 'operation_start').length, 0);
    resumed.api.unload();

    const se0 = 'A06573746B6D65FFFF4953442D522030';
    const se1 = 'A06573746B6D65FFFF4953442D522031';
    let seSnapshots = [];
    const multi = harness((method, args) => {
        if (method === 'snapshot') { seSnapshots.push(args); return { ...snapshot, aid: args[1] || se0, ses: 3 }; }
        if (method === 'job_status') return { running: false, state: 'done', data: profiles };
        return good(null);
    });
    await multi.open();
    assert.ok(multi.root.textContent.includes('SE 0') && multi.root.textContent.includes('SE 1'));
    assert.deepEqual(seSnapshots[0], [0, '', true]);
    seSnapshots = [];
    await multi.click('SE 1');
    assert.deepEqual(seSnapshots[0], [0, se1, false]);
    await multi.click('Test');
    await multi.click('Enable');
    await multi.click('Enable');
    const operation = JSON.parse(multi.calls.filter(c => c[0] === 'operation_start').at(-1)[2]);
    assert.equal(operation.action, 'switch');
    assert.equal(multi.calls.filter(c => c[0] === 'operation_start').at(-1)[3], se1);
    await multi.tick();
    seSnapshots = [];
    await multi.click('SIM 1');
    assert.deepEqual(seSnapshots, [[0, se1, false]]);
    multi.api.unload();

    let progressJob = { type: 'download', slot: 1, aid: '', running: true, step: 'es9p_authenticate_client' };
    const progress = harness(() => progressJob);
    await progress.open({ type: 'download', running: true, slot: 1 });
    await progress.tick();
    assert.equal(progress.modal.textContent.includes('%'), false);
    progressJob = { type: 'download', slot: 1, aid: '', running: true, step: 'es10b_load_bound_profile_package', write: { written: 6300, total: 10000, percent: 63 } };
    await progress.tick();
    assert.ok(progress.modal.textContent.includes('63%'));
    progressJob.write = { written: 10000, total: 10000, percent: 100 };
    await progress.tick();
    assert.ok(progress.modal.textContent.includes('Confirming the installation'));
    progress.api.unload();

    let launch;
    const c = harness(method => {
        if (method === 'snapshot') return snapshot;
        if (method === 'notifications') return good([]);
        if (method === 'download_start') return new Promise(resolve => { launch = resolve; });
        if (method === 'job_status') return { type: 'download', slot: 0, aid: '', running: true, preview: true, metadata: { profileName: 'Preview' } };
        return good(null);
    });
    await c.open();
    const callsBeforeBack = c.calls.length;
    await c.click('Add profile');
    await c.click('Back');
    assert.ok(c.root.textContent.includes('Test'));
    assert.equal(c.calls.length, callsBeforeBack);
    await c.click('Add profile');
    function input(node) { if (node.tag === 'input') return node; for (const n of node.children) { if (n instanceof Node) { const found = input(n); if (found) return found; } } }
    input(c.root).attrs.input({ target: { value: 'LPA:1$server$code' } });
    await c.click('Download');
    await c.click('Cancel');
    launch(good(null)); await flush();
    assert.equal(c.calls.filter(call => call[0] === 'job_cancel').length, 1);
    await c.tick();
    assert.equal(c.calls.filter(call => call[0] === 'job_confirm').length, 0);
    assert.ok(c.root.textContent.includes('Cancelling download'));
    c.api.unload();

    const downloadJob = extra => ({ type: 'download', slot: 0, aid: '', running: true, ...extra });
    async function beginDownload(page) {
        await page.open();
        await page.click('Add profile');
        matching(page.root, node => node.tag === 'input')[0].attrs.input({ target: { value: 'LPA:1$example.invalid$unused' } });
        await page.click('Download');
    }
    let activeJob = downloadJob({ step: 'es9p_initiate_authentication' });
    const lostReply = harness(method => {
        if (method === 'snapshot') return snapshot;
        if (method === 'download_start') return Promise.reject(new Error('start reply lost'));
        if (method === 'job_status') return activeJob;
        return good(null);
    });
    await beginDownload(lostReply);
    assert.ok(lostReply.modal, 'lost start reply must reconnect to the running task');
    assert.equal(lostReply.calls.filter(call => call[0] === 'download_start').length, 1);
    await lostReply.click('Cancel');
    activeJob = downloadJob({ running: false, state: 'cancelled', error: 'cancelled' });
    await lostReply.tick();
    assert.ok(lostReply.root.textContent.includes('Activation code'));
    assert.ok(lostReply.notices.includes('Download cancelled'));
    assert.equal(lostReply.pendingTimers, 0);
    lostReply.api.unload();

    const noTask = harness(method => {
        if (method === 'snapshot') return snapshot;
        if (method === 'download_start') return Promise.reject(new Error('request did not arrive'));
        if (method === 'job_status') return downloadJob({ running: false, state: 'done' });
        return good(null);
    });
    await beginDownload(noTask);
    assert.ok(noTask.root.textContent.includes('Activation code'));
    assert.equal(noTask.pendingTimers, 0);
    assert.ok(noTask.notices.includes('request did not arrive'));
    assert.equal(noTask.notices.includes('Profile downloaded'), false, 'stale successful task must not acknowledge a failed start');
    noTask.api.unload();

    let offline = true;
    activeJob = downloadJob({ step: 'es9p_initiate_authentication' });
    const uncertain = harness(method => {
        if (method === 'snapshot') return snapshot;
        if (method === 'download_start') return Promise.reject(new Error('start reply lost'));
        if (method === 'job_status') return offline ? Promise.reject(new Error('offline')) : activeJob;
        return good(null);
    });
    await beginDownload(uncertain);
    assert.ok(uncertain.root.textContent.includes('Check status'));
    assert.equal(uncertain.pendingTimers, 0);
    await uncertain.click('Cancel');
    assert.ok(uncertain.root.textContent.includes('Check status'));
    offline = false;
    await uncertain.click('Check status');
    assert.equal(uncertain.calls.filter(call => call[0] === 'job_cancel').length, 1);
    activeJob = downloadJob({ running: false, state: 'cancelled', error: 'cancelled' });
    await uncertain.tick();
    assert.ok(uncertain.root.textContent.includes('Activation code'));
    assert.equal(uncertain.calls.filter(call => call[0] === 'download_start').length, 1);
    uncertain.api.unload();

    let cancellationFails = true;
    activeJob = downloadJob({ preview: true, metadata: { profileName: 'Preview' } });
    const retryCancel = harness(method => {
        if (method === 'snapshot') return snapshot;
        if (method === 'job_status') return activeJob;
        if (method === 'job_cancel' && cancellationFails) return { ok: false, error: 'busy' };
        return good(null);
    });
    await beginDownload(retryCancel);
    await retryCancel.tick();
    await retryCancel.click('Cancel');
    assert.ok(retryCancel.root.textContent.includes('Cancellation failed'));
    assert.ok(retryCancel.root.textContent.includes('Check status'));
    cancellationFails = false;
    await retryCancel.click('Cancel');
    assert.equal(retryCancel.calls.filter(call => call[0] === 'job_cancel').length, 2);
    activeJob = downloadJob({ running: false, state: 'cancelled', error: 'cancelled' });
    await retryCancel.tick();
    assert.equal(retryCancel.pendingTimers, 0);
    retryCancel.api.unload();

    let cancelReply;
    activeJob = downloadJob({ step: 'es9p_initiate_authentication' });
    const lateCancel = harness(method => {
        if (method === 'snapshot') return snapshot;
        if (method === 'job_status') return activeJob;
        if (method === 'job_cancel') return new Promise(resolve => { cancelReply = resolve; });
        return good(null);
    });
    await beginDownload(lateCancel);
    await lateCancel.click('Cancel');
    activeJob = downloadJob({ running: false, state: 'cancelled', error: 'cancelled' });
    await lateCancel.tick();
    cancelReply({ ok: false, error: 'cancel' });
    await flush();
    assert.ok(lateCancel.root.textContent.includes('Activation code'));
    assert.equal(lateCancel.root.textContent.includes('Check status'), false);
    assert.equal(lateCancel.pendingTimers, 0);
    lateCancel.api.unload();

    let cancelOffline = true;
    activeJob = downloadJob({ step: 'es9p_initiate_authentication' });
    const cancelOutage = harness(method => {
        if (method === 'snapshot') return snapshot;
        if (method === 'job_status') return activeJob;
        if (method === 'job_cancel' && cancelOffline) return Promise.reject(new Error('cancel transport offline'));
        return good(null);
    });
    await beginDownload(cancelOutage);
    await cancelOutage.click('Cancel');
    assert.ok(cancelOutage.root.textContent.includes('cancel transport offline'));
    cancelOffline = false;
    await cancelOutage.click('Cancel');
    activeJob = downloadJob({ running: false, state: 'cancelled', error: 'cancelled' });
    await cancelOutage.tick();
    assert.ok(cancelOutage.root.textContent.includes('Activation code'));
    assert.equal(cancelOutage.calls.filter(call => call[0] === 'download_start').length, 1);
    cancelOutage.api.unload();

    offline = true;
    activeJob = downloadJob({ step: 'es9p_initiate_authentication' });
    const outage = harness(method => {
        if (method === 'snapshot') return snapshot;
        if (method === 'job_status') return offline ? Promise.reject(new Error('status offline')) : activeJob;
        return good(null);
    });
    await beginDownload(outage);
    await outage.tick(); await outage.tick(); await outage.tick();
    assert.equal(outage.pendingTimers, 0);
    assert.ok(outage.root.textContent.includes('Check status'));
    offline = false;
    await outage.click('Check status');
    assert.ok(outage.modal);
    await outage.click('Cancel');
    activeJob = downloadJob({ running: false, state: 'done' });
    await outage.tick();
    assert.ok(outage.notices.includes('Profile downloaded'), 'completed installation must remain successful after a late cancel');
    assert.equal(outage.notices.includes('Download cancelled'), false);
    outage.api.unload();

    const depItems = [
        { package: 'lpac', path: '/usr/lib/lpac', present: false },
        { package: 'libcurl-gnutls4', path: '/usr/lib/libcurl-gnutls.so.4', present: true }
    ];
    let deps = { ok: true, data: { ready: false, missing: ['lpac'], items: depItems } };
    const missing = harness(method => {
        if (method === 'dependencies') return deps;
        if (method === 'snapshot') return snapshot;
        return good(null);
    });
    const loaded = await missing.openViaLoad();
    assert.deepEqual(loaded.deps, deps.data);
    assert.ok(missing.root.textContent.includes('lpac is missing'));
    assert.ok(missing.root.textContent.includes('/usr/lib/lpac'));
    assert.ok(missing.root.textContent.includes('apk update && apk add lpac'));
    const depsLink = matching(missing.root, node => node.tag === 'a' && node.textContent === 'Open package manager');
    assert.equal(depsLink[0].attrs.href, '/cgi-bin/luci/admin/system/opkg');
    assert.equal(missing.calls.filter(call => call[0] === 'snapshot').length, 0);
    deps = { ok: true, data: { ready: true, missing: [],
        items: depItems.map(item => ({ ...item, present: true })) } };
    const dependencyCalls = missing.calls.filter(call => call[0] === 'dependencies').length;
    await missing.click('Check again');
    assert.equal(missing.calls.filter(call => call[0] === 'dependencies').length, dependencyCalls + 1);
    assert.ok(missing.root.textContent.includes('Test'));
    missing.api.unload();

    const partialDeps = { ok: true, data: { ready: false, missing: ['libcurl-gnutls4'], items: [
        { package: 'lpac', path: '/usr/lib/lpac', present: true },
        { package: 'libcurl-gnutls4', path: '/usr/lib/libcurl-gnutls.so.4', present: false }
    ] } };
    const partial = harness(method => {
        if (method === 'dependencies') return partialDeps;
        if (method === 'snapshot') return snapshot;
        return good(null);
    });
    await partial.openViaLoad();
    assert.ok(partial.root.textContent.includes('Test'));
    assert.ok(partial.root.textContent.includes('apk update && apk add libcurl-gnutls4'));
    partial.api.unload();

    console.log('Frontend flows passed: slot fallback/selection, multi-SE routing, switch sequence, download cancellation/recovery, dependency guidance.');

})().catch(error => { console.error(error); process.exitCode = 1; });
