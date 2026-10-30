/* Polyseat for Unraid - WebUI logic (WIP). Vanilla JS; no external requests; data only enters the DOM as text nodes. */
(function (root) {
  'use strict';

  var PSU = {};
  var cfg = { api: '/plugins/polyseat-unraid/include/api.php', csrf: '', gate: 'closed' };

  /* ------------------------------------------------------------ pure helpers ---- */

  PSU.fmtKb = function (kb) {
    if (kb === null || kb === undefined || isNaN(kb)) { return '-'; }
    var units = ['KB', 'MB', 'GB', 'TB'];
    var v = Number(kb), i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
  };

  PSU.pct = function (part, whole) {
    if (!whole || whole <= 0 || part === null || part === undefined) { return 0; }
    return Math.max(0, Math.min(100, (part / whole) * 100));
  };

  PSU.level = function (pct, warn, crit) {
    warn = warn === undefined ? 70 : warn;
    crit = crit === undefined ? 90 : crit;
    return pct >= crit ? 'crit' : (pct >= warn ? 'warn' : '');
  };

  PSU.stateView = function (status) {
    switch (status && status.state) {
      case 'RUNNING': return { text: 'Running', cls: 'ok' };
      case 'ACTIVE': return { text: 'Installed, stopped', cls: 'muted' };
      case 'ABSENT': return { text: 'Not installed', cls: 'muted' };
      case 'JOURNAL_PENDING': return { text: 'Recovery needed', cls: 'crit' };
      case 'CORRUPT': return { text: 'State corrupt', cls: 'crit' };
      default: return { text: 'Unavailable', cls: 'crit' };
    }
  };

  PSU.gateView = function (gate) {
    if (gate === 'open') { return { text: 'Changes enabled', cls: 'ok' }; }
    if (gate === 'test-host') { return { text: 'Test build', cls: 'warn' }; }
    return { text: 'Locked (WIP)', cls: 'warn' };
  };

  /* Mirrors psu_confirm_phrase() in include/lib.php. */
  PSU.confirmPhrase = function (action, subject) {
    switch (action) {
      case 'activate': return 'ACTIVATE ' + String(subject || '').slice(0, 8);
      case 'rollback': return 'ROLLBACK';
      case 'recover': return 'RECOVER';
      case 'stop': return 'STOP';
      case 'restart': return 'RESTART';
      case 'config_reset': return 'RESET';
      default: return '';
    }
  };

  PSU.classifyLogLine = function (line) {
    if (/\[error\]|\berror\b/i.test(line)) { return 'l-error'; }
    if (/\[warn\]|\bwarning\b/i.test(line)) { return 'l-warn'; }
    return '';
  };

  PSU.validateSetting = function (rule, value) {
    if (!rule) { return false; }
    if (rule.type === 'enum') { return rule.values.indexOf(value) !== -1; }
    if (rule.type === 'int') {
      return /^[0-9]{1,10}$/.test(value) && Number(value) >= rule.min && Number(value) <= rule.max;
    }
    if (rule.type === 'path') { return value === '' || new RegExp(rule.pattern).test(value); }
    return false;
  };

  PSU.errorText = function (resp) {
    var map = {
      origin: 'The request was rejected (origin check). Reload the page and try again.',
      csrf: 'Your session token is no longer valid. Reload the page and try again.',
      confirm: 'The confirmation text did not match.',
      params: 'The request was not valid.',
      locked: 'Changes are disabled in this WIP build (release gate closed).',
      failed: 'The controller refused or failed the action.',
      timeout: 'The action took too long and was stopped.',
      unavailable: 'The controller is not available.',
      internal: 'Internal error; see the system log.'
    };
    var base = map[resp && resp.error] || 'The action failed.';
    return resp && resp.detail ? base + ' ' + resp.detail : base;
  };

  /* --------------------------------------------------------------- DOM utils ---- */

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') { el.textContent = attrs[k]; } else if (k === 'class') { el.className = attrs[k]; } else { el.setAttribute(k, attrs[k]); }
    });
    (children || []).forEach(function (c) { if (c) { el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); } });
    return el;
  }

  function setText(scope, bind, text) {
    var nodes = scope.querySelectorAll('[data-bind="' + bind + '"]');
    for (var i = 0; i < nodes.length; i++) { nodes[i].textContent = text; }
  }

  function setPill(el, view) {
    if (!el) { return; }
    el.className = 'psu-pill ' + view.cls;
    el.textContent = view.text;
  }

  function setMeter(scope, bind, pct, label) {
    var bar = scope.querySelector('[data-meter="' + bind + '"]');
    if (bar) { bar.style.width = pct.toFixed(0) + '%'; bar.className = PSU.level(pct); }
    setText(scope, bind, label);
  }

  /* ----------------------------------------------------------------- network ---- */

  PSU.api = function (action, params) {
    var q = new URLSearchParams(Object.assign({ action: action }, params || {}));
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 20000);
    return fetch(cfg.api + '?' + q.toString(), { credentials: 'same-origin', cache: 'no-store', signal: ctl.signal,
      headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { clearTimeout(timer); return r.json().catch(function () { return { ok: false, error: 'internal' }; }); })
      .catch(function () { clearTimeout(timer); return { ok: false, error: 'unavailable' }; });
  };

  PSU.post = function (action, fields) {
    var body = new URLSearchParams(Object.assign({ action: action, psu_csrf: cfg.csrf }, fields || {}));
    return fetch(cfg.api, { method: 'POST', credentials: 'same-origin', cache: 'no-store', body: body,
      headers: { 'X-CSRF-Token': cfg.csrf, 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'internal' }; }); })
      .catch(function () { return { ok: false, error: 'unavailable' }; });
  };

  /* ------------------------------------------------------- confirmation modal ---- */

  var modal = null;
  PSU.confirm = function (opts) {
    return new Promise(function (resolve) {
      if (!modal) {
        modal = h('div', { class: 'psu-modal-backdrop', hidden: '' });
        document.body.appendChild(modal);
      }
      var need = opts.phrase || '';
      var input = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Confirmation text' });
      var ok = h('button', { type: 'button', class: 'psu-btn ' + (opts.danger ? 'danger' : 'primary'), text: opts.button || 'Confirm' });
      var cancel = h('button', { type: 'button', class: 'psu-btn', text: 'Cancel' });
      ok.disabled = need !== '';
      var box = h('div', { class: 'psu psu-modal', role: 'dialog', 'aria-modal': 'true' }, [
        h('h3', { text: opts.title }),
        h('div', { text: opts.body }),
        need ? h('div', { class: 'psu-muted', text: 'Type ' + need + ' to confirm.' }) : null,
        need ? input : null,
        h('div', { class: 'psu-actions' }, [cancel, ok])
      ]);
      function close(value) { modal.hidden = true; modal.textContent = ''; document.removeEventListener('keydown', onKey); resolve(value); }
      function onKey(e) { if (e.key === 'Escape') { close(null); } }
      input.addEventListener('input', function () { ok.disabled = input.value !== need; });
      cancel.addEventListener('click', function () { close(null); });
      ok.addEventListener('click', function () { close(need ? input.value : 'confirmed'); });
      modal.textContent = '';
      modal.appendChild(box);
      modal.hidden = false;
      document.addEventListener('keydown', onKey);
      (need ? input : ok).focus();
    });
  };

  /* ------------------------------------------------------------------- tile ---- */

  function paintStatus(scope, status) {
    var sv = PSU.stateView(status), gv = PSU.gateView(status && status.release_gate);
    setPill(scope.querySelector('[data-bind="state-pill"]'), sv);
    setPill(scope.querySelector('[data-bind="gate-pill"]'), gv);
    setText(scope, 'version', (status && status.version) || '-');
    setText(scope, 'state-text', sv.text);
  }

  function paintMetrics(scope, m) {
    if (!m) { return; }
    var memUsed = m.memory.total_kb - m.memory.available_kb;
    var memPct = PSU.pct(memUsed, m.memory.total_kb);
    var loadPct = PSU.pct(m.load[0], m.cpus);
    setMeter(scope, 'cpu', loadPct, 'load ' + m.load[0].toFixed(2) + ' / ' + m.cpus + ' cores');
    setMeter(scope, 'mem', memPct, PSU.fmtKb(memUsed) + ' / ' + PSU.fmtKb(m.memory.total_kb));
    setText(scope, 'daemon', m.daemon.running ? 'pid ' + m.daemon.pid : 'stopped');
    setText(scope, 'instances', m.incus && m.incus.reachable ? m.incus.running + ' / ' + m.incus.total : '-');
    setText(scope, 'daemon-mem', m.daemon.running ? PSU.fmtKb(m.daemon.rss_kb) : '-');
    if (m.data_dir) {
      var used = m.data_dir.total_kb - m.data_dir.free_kb;
      setMeter(scope, 'disk', PSU.pct(used, m.data_dir.total_kb), PSU.fmtKb(m.data_dir.free_kb) + ' free (' + m.data_dir.fstype + ')');
    } else {
      setMeter(scope, 'disk', 0, 'not configured');
    }
  }

  function poll(scope, intervalMs) {
    var timer = null;
    function tick() {
      if (document.hidden) { return; }
      Promise.all([PSU.api('status'), PSU.api('metrics')]).then(function (r) {
        if (r[0].ok) { paintStatus(scope, r[0].data); cfg.gate = r[0].data.release_gate; scope.dispatchEvent(new CustomEvent('psu:status', { detail: r[0].data })); }
        else { paintStatus(scope, null); }
        if (r[1].ok) { paintMetrics(scope, r[1].data); }
      });
    }
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { tick(); } });
    tick();
    timer = setInterval(tick, intervalMs);
    return function stop() { clearInterval(timer); };
  }

  PSU.initTile = function (scope, config) {
    Object.assign(cfg, config || {});
    poll(scope, 15000);
    var refresh = scope.querySelector('[data-action="refresh"]');
    if (refresh) { refresh.addEventListener('click', function (e) { e.preventDefault(); poll(scope, 15000); }); }
  };

  /* ------------------------------------------------------------- tool page ---- */

  function initTabs(scope, onShow) {
    var tabs = scope.querySelectorAll('[role="tab"]');
    function show(id, focus) {
      for (var i = 0; i < tabs.length; i++) {
        var on = tabs[i].getAttribute('data-tab') === id;
        tabs[i].setAttribute('aria-selected', on ? 'true' : 'false');
        tabs[i].tabIndex = on ? 0 : -1;
        var panel = scope.querySelector('#' + tabs[i].getAttribute('aria-controls'));
        if (panel) { panel.hidden = !on; }
        if (on && focus) { tabs[i].focus(); }
      }
      if (onShow) { onShow(id); }
      try { history.replaceState(null, '', '#' + id); } catch (e) { /* ignore */ }
    }
    for (var i = 0; i < tabs.length; i++) {
      (function (tab, index) {
        tab.addEventListener('click', function () { show(tab.getAttribute('data-tab'), false); });
        tab.addEventListener('keydown', function (e) {
          var next = e.key === 'ArrowRight' ? index + 1 : (e.key === 'ArrowLeft' ? index - 1 : null);
          if (next !== null) { e.preventDefault(); show(tabs[(next + tabs.length) % tabs.length].getAttribute('data-tab'), true); }
        });
      })(tabs[i], i);
    }
    var wanted = (location.hash || '').replace('#', '');
    var known = Array.prototype.map.call(tabs, function (t) { return t.getAttribute('data-tab'); });
    show(known.indexOf(wanted) !== -1 ? wanted : known[0], false);
  }

  function notify(scope, kind, text) {
    var box = scope.querySelector('[data-bind="notice"]');
    if (!box) { return; }
    box.className = 'psu-banner ' + kind;
    box.textContent = text;
    box.hidden = !text;
  }

  function runAction(scope, label, action, fields, phraseSubject, opts) {
    var phrase = PSU.confirmPhrase(opts.phraseAction || action, phraseSubject);
    var go = phrase || opts.alwaysConfirm
      ? PSU.confirm({ title: label, body: opts.body, phrase: phrase, danger: !!opts.danger, button: label })
      : Promise.resolve('confirmed');
    return go.then(function (typed) {
      if (typed === null) { return; }
      notify(scope, 'ok', label + '...');
      var f = Object.assign({}, fields);
      if (phrase) { f.confirm = typed; }
      return PSU.post(action, f).then(function (resp) {
        notify(scope, resp.ok ? 'ok' : 'crit', resp.ok ? label + ' completed.' : PSU.errorText(resp));
        scope.dispatchEvent(new CustomEvent('psu:refresh'));
      });
    });
  }

  function renderBundles(scope, listResp, status) {
    var body = scope.querySelector('[data-bind="bundles"]');
    if (!body) { return; }
    body.textContent = '';
    var rows = (listResp.ok ? listResp.data : []).slice().sort(function (a, b) { return (b.current ? 1 : 0) - (a.current ? 1 : 0); });
    if (!rows.length) {
      body.appendChild(h('tr', {}, [h('td', { colspan: '5', class: 'psu-muted', text: 'No runtime bundle is staged on the flash drive.' })]));
    }
    var locked = !status || status.release_gate === 'closed';
    rows.forEach(function (b) {
      var role = b.current ? h('span', { class: 'psu-pill ok', text: 'active' }) : (b.previous ? h('span', { class: 'psu-pill muted', text: 'previous' }) : h('span', { class: 'psu-muted', text: 'staged' }));
      var btn = h('button', { type: 'button', class: 'psu-btn', text: 'Activate' });
      btn.disabled = locked || b.current || !b.has_archive || !b.has_signature;
      btn.addEventListener('click', function () {
        runAction(scope, 'Activate bundle', 'activate', { sha: b.sha }, b.sha, {
          danger: true, body: 'Switch the runtime to version ' + b.version + ' (' + b.sha.slice(0, 8) + '). The service restarts if it is running and rolls back automatically if the health check fails.' });
      });
      body.appendChild(h('tr', {}, [
        h('td', { 'data-label': 'Version', text: b.version }),
        h('td', { 'data-label': 'Incus', text: b.incus_version }),
        h('td', { 'data-label': 'Bundle' }, [h('code', { text: b.sha.slice(0, 12) })]),
        h('td', { 'data-label': 'Role' }, [role]),
        h('td', {}, [btn])
      ]));
    });
  }

  PSU.initTool = function (scope, config) {
    Object.assign(cfg, config || {});
    var logState = { which: 'controller', lines: '200' };
    var lastStatus = null;

    function refreshAll() {
      Promise.all([PSU.api('status'), PSU.api('metrics'), PSU.api('list')]).then(function (r) {
        if (r[0].ok) { lastStatus = r[0].data; paintStatus(scope, lastStatus); cfg.gate = lastStatus.release_gate; applyLocks(); }
        else { paintStatus(scope, null); }
        if (r[1].ok) { paintMetrics(scope, r[1].data); }
        renderBundles(scope, r[2], lastStatus);
      });
    }

    function applyLocks() {
      var locked = !lastStatus || lastStatus.release_gate === 'closed';
      var banner = scope.querySelector('[data-bind="gate-banner"]');
      if (banner) { banner.hidden = !locked; }
      var state = lastStatus && lastStatus.state;
      var map = { start: state === 'ACTIVE', stop: state === 'RUNNING', restart: state === 'RUNNING',
                  rollback: state === 'RUNNING' || state === 'ACTIVE', recover: state === 'JOURNAL_PENDING' };
      Object.keys(map).forEach(function (k) {
        var b = scope.querySelector('[data-do="' + k + '"]');
        if (b) { b.disabled = locked || !map[k]; }
      });
    }

    function loadLogs() {
      var pre = scope.querySelector('[data-bind="log"]');
      PSU.api('logs', logState).then(function (resp) {
        pre.textContent = '';
        if (!resp.ok) { pre.textContent = PSU.errorText(resp); return; }
        var lines = String(resp.data).split('\n');
        if (lines[lines.length - 1] === '') { lines.pop(); }
        if (!lines.length) { pre.textContent = '(empty)'; return; }
        lines.forEach(function (line) {
          pre.appendChild(h('span', { class: PSU.classifyLogLine(line), text: line + '\n' }));
        });
        pre.scrollTop = pre.scrollHeight;
      });
    }

    function loadDiag() {
      var pre = scope.querySelector('[data-bind="diag"]');
      pre.textContent = 'Collecting...';
      PSU.api('diag').then(function (resp) { pre.textContent = resp.ok ? resp.data : PSU.errorText(resp); });
    }

    scope.addEventListener('psu:refresh', function () { setTimeout(refreshAll, 400); });
    initTabs(scope, function (id) { if (id === 'logs') { loadLogs(); } });

    var service = {
      start: ['Start service', {}, { body: 'Start the Polyseat/Incus daemon.', alwaysConfirm: false }],
      stop: ['Stop service', {}, { danger: true, body: 'Gracefully stop all instances and the daemon.' }],
      restart: ['Restart service', {}, { danger: true, body: 'Restart the daemon. Running containers keep running.' }]
    };
    Object.keys(service).forEach(function (k) {
      var b = scope.querySelector('[data-do="' + k + '"]');
      if (b) { b.addEventListener('click', function () {
        var s = service[k];
        runAction(scope, s[0], 'service', { 'do': k }, '', Object.assign({ phraseAction: k }, s[2]));
      }); }
    });
    var rb = scope.querySelector('[data-do="rollback"]');
    if (rb) { rb.addEventListener('click', function () { runAction(scope, 'Roll back', 'rollback', {}, '', { danger: true, body: 'Return to the previous runtime bundle.' }); }); }
    var rc = scope.querySelector('[data-do="recover"]');
    if (rc) { rc.addEventListener('click', function () { runAction(scope, 'Recover', 'recover', {}, '', { danger: true, body: 'Finish or revert the interrupted update; the previous bundle is restored.' }); }); }

    var sel = scope.querySelector('[data-bind="log-which"]');
    var num = scope.querySelector('[data-bind="log-lines"]');
    if (sel) { sel.addEventListener('change', function () { logState.which = sel.value; loadLogs(); }); }
    if (num) { num.addEventListener('change', function () { logState.lines = num.value; loadLogs(); }); }
    var lr = scope.querySelector('[data-action="logs-refresh"]');
    if (lr) { lr.addEventListener('click', loadLogs); }
    var dr = scope.querySelector('[data-action="diag-run"]');
    if (dr) { dr.addEventListener('click', loadDiag); }
    var cp = scope.querySelector('[data-action="diag-copy"]');
    if (cp) { cp.addEventListener('click', function () {
      var text = scope.querySelector('[data-bind="diag"]').textContent;
      if (navigator.clipboard) { navigator.clipboard.writeText(text); }
    }); }

    refreshAll();
    setInterval(function () { if (!document.hidden) { refreshAll(); } }, 20000);
  };

  /* --------------------------------------------------------------- settings ---- */

  PSU.initSettings = function (scope, config) {
    Object.assign(cfg, config || {});
    var schema = (config && config.schema) || {};
    var form = scope.querySelector('form');
    var applyBtn = scope.querySelector('[data-action="apply"]');
    var resetBtn = scope.querySelector('[data-action="reset"]');
    var fields = Array.prototype.slice.call(form.querySelectorAll('[data-key]'));
    var initial = {};
    fields.forEach(function (f) { initial[f.getAttribute('data-key')] = f.value; });

    function fieldError(f, text) {
      var row = f.closest('.psu-field');
      var old = row.querySelector('.psu-err');
      if (old) { old.remove(); }
      if (text) { row.appendChild(h('div', { class: 'psu-err', role: 'alert', text: text })); }
    }

    function refresh() {
      var dirty = false, valid = true;
      fields.forEach(function (f) {
        var key = f.getAttribute('data-key');
        var changed = f.value !== initial[key];
        var label = f.closest('.psu-field').querySelector('label');
        if (label) { label.classList.toggle('psu-dirty', changed); }
        dirty = dirty || changed;
        var ok = PSU.validateSetting(schema[key], f.value);
        fieldError(f, ok ? '' : 'Invalid value.');
        valid = valid && ok;
      });
      applyBtn.disabled = cfg.gate === 'closed' || !dirty || !valid;
      setText(scope, 'dirty-note', dirty ? 'Unsaved changes' : '');
    }

    fields.forEach(function (f) { f.addEventListener('input', refresh); f.addEventListener('change', refresh); });

    initTabs(scope, null);

    resetBtn.addEventListener('click', function () {
      fields.forEach(function (f) {
        var key = f.getAttribute('data-key');
        if (!f.disabled) { f.value = schema[key].default; }
      });
      refresh();
      notify(scope, 'warn', 'Defaults filled in. Nothing is saved until you click Apply.');
    });

    applyBtn.addEventListener('click', function () {
      var changed = {};
      fields.forEach(function (f) { if (f.value !== initial[f.getAttribute('data-key')]) { changed[f.getAttribute('data-key')] = f.value; } });
      applyBtn.disabled = true;
      PSU.post('config_apply', changed).then(function (resp) {
        if (resp.ok) {
          Object.keys(changed).forEach(function (k) { initial[k] = changed[k]; });
          notify(scope, 'ok', 'Settings saved. They take effect the next time the service starts.');
        } else {
          notify(scope, 'crit', PSU.errorText(resp));
        }
        refresh();
      });
    });

    PSU.api('status').then(function (r) {
      if (r.ok) {
        cfg.gate = r.data.release_gate;
        var running = r.data.state === 'RUNNING';
        fields.forEach(function (f) {
          var rule = schema[f.getAttribute('data-key')];
          if (running && rule && rule.locked_while_running) {
            f.disabled = true;
            f.title = 'Stop the service to change this setting.';
          }
        });
        var lock = scope.querySelector('[data-bind="gate-banner"]');
        if (lock) { lock.hidden = cfg.gate !== 'closed'; }
      }
      refresh();
    });
    refresh();
  };

  root.PSU = PSU;
  if (typeof module !== 'undefined' && module.exports) { module.exports = PSU; }
})(typeof window !== 'undefined' ? window : globalThis);
