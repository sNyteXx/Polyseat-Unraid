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

  PSU.gateView = function (gate, mode) {
    if (gate === 'open') { return { text: 'Changes enabled', cls: 'ok' }; }
    if (gate === 'test-host') { return { text: 'Test build', cls: 'warn' }; }
    /* an owner build is a canary build whose approval is the admin's click: not "locked", and said so */
    if (gate === 'canary' && mode === 'owner') { return { text: 'Owner mode', cls: 'ok' }; }
    return { text: 'Locked (WIP)', cls: 'warn' };
  };

  /* What this build isolates, in an owner build (the page ships the older texts for every other build). */
  PSU.isolationText = function (status) {
    if (!status || status.release_gate !== 'canary' || status.approval_mode !== 'owner') { return null; }
    return {
      network: 'polyseatd is reachable on the LAN at :47800 while a session runs (nothing listens otherwise); seats and workload ' +
        'containers are bridged onto br0; no firewall or routing changes',
      gpu: 'Seats use the host\'s NVIDIA RTX by device access, admitted by the GPU admission guard: a VM that owns the GPU always ' +
        'wins and a session that cannot get it does not start',
      data: 'Seats and Incus: /mnt/cache/polyseat; games: the library dataset (LIBRARY_PATH); workloads: /mnt/cache/polyseat/workloads. ' +
        'The plugin never removes any of it'
    };
  };

  /* Mirrors psu_confirm_phrase() in include/lib.php. */
  PSU.confirmPhrase = function (action, subject) {
    switch (action) {
      case 'activate': return 'ACTIVATE ' + String(subject || '').slice(0, 8);
      case 'rollback': return 'ROLLBACK';
      case 'recover': return 'RECOVER';
      case 'stop': return 'STOP';
      case 'play_stop': return 'STOP';
      case 'play_recover': return 'WIEDERHERSTELLEN';
      case 'workloads_stop': return 'STOP WORKLOADS';
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
    if (rule.type === 'path' || rule.type === 'string') { return value === '' || new RegExp(rule.pattern).test(value); }
    return false;
  };

  /* Only a LAN address polyseatd serves can be opened; anything else from the API is dropped. */
  PSU.playUrl = function (url) {
    return typeof url === 'string' && /^https:\/\/[0-9]{1,3}(\.[0-9]{1,3}){3}:[0-9]{1,5}$/.test(url) ? url : '';
  };

  PSU.fmtDuration = function (seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) { return '-'; }
    if (seconds <= 0) { return 'ended'; }
    var min = Math.floor(seconds / 60);
    if (min < 1) { return 'under 1 min'; }
    return min >= 60 ? Math.floor(min / 60) + ' h ' + (min % 60) + ' min' : min + ' min';
  };

  /* Why a play session ended the way it did, in the owner's words. The reasons are the controller's (watchdog and recovery). */
  var END_TEXT = {
    'memory-floor': 'Session wegen Speichermangel beendet',
    'memory-floor-hard': 'Session wegen Speichermangel beendet (sofort gestoppt, weniger als 2 GiB frei)',
    'memory-floor-timeout': 'Session wegen Speichermangel beendet (das saubere Beenden dauerte zu lange)',
    'expired': 'Sitzungszeit abgelaufen',
    'stop-requested': 'Session gestoppt',
    'gpu-ownership-changed': 'GPU wurde einer VM zugeteilt, Session hart beendet',
    'resource-policy-or-budget': 'Ressourcengrenze verletzt, Session hart beendet',
    'host-not-restored': 'Die Host-Änderungen der Session wurden nicht zurückgesetzt',
    'session-process-gone': 'Der Prozess der Session ist verschwunden',
    'no-clean-stop': 'Kein sauberer Stopp aufgezeichnet'
  };

  PSU.endText = function (reason) {
    if (typeof reason !== 'string' || !/^[a-z][a-z0-9-]{0,63}$/.test(reason)) { return ''; }
    return END_TEXT[reason] || 'Session abnormal beendet (' + reason + ')';
  };

  /* resp is the API reply of action=play: data is null while no play session runs, state is idle | busy | running |
     needs-recovery (an older controller sends no state). Mirrors psu_play_state() and psu_play_view() in lib.php. */
  PSU.playState = function (resp) {
    if (!resp || !resp.ok) { return 'unknown'; }
    if (resp.state === 'idle' || resp.state === 'busy' || resp.state === 'running' || resp.state === 'needs-recovery') { return resp.state; }
    return resp.data ? 'running' : 'idle';
  };

  PSU.playView = function (resp) {
    var none = { text: 'No play session', cls: 'muted', url: '', seats: '-', remaining: '-', note: '' };
    if (!resp || !resp.ok) { return { text: 'Play status unavailable', cls: 'warn', url: '', seats: '-', remaining: '-', note: '' }; }
    var state = PSU.playState(resp);
    if (state === 'needs-recovery') {
      return { text: 'Wiederherstellung nötig', cls: 'crit', url: '', seats: '-', remaining: '-', note: PSU.endText(resp.reason) || 'Session abnormal beendet' };
    }
    if (state === 'busy') { return { text: 'Controller busy', cls: 'warn', url: '', seats: '-', remaining: '-', note: '' }; }
    var d = resp.data;
    if (!d) {
      var le = resp.last_end;
      none.note = le && le.reason ? PSU.endText(le.reason) + (le.recovered ? ' (wiederhergestellt)' : '') : '';
      return none;
    }
    var seats = '-';
    if (Array.isArray(d.seats)) {
      seats = d.seats.length
        ? d.seats.length + ': ' + d.seats.map(function (s) { return s.name + ' (' + s.status + ')'; }).join(', ')
        : 'none yet';
    }
    var now = typeof resp.now === 'number' ? resp.now : Math.floor(Date.now() / 1000);
    var left = typeof d.expires_at === 'number' ? PSU.fmtDuration(d.expires_at - now) : '-';
    if (!d.alive) { return { text: 'Polyseat is down', cls: 'crit', url: '', seats: seats, remaining: left, note: '' }; }
    return { text: 'Play session running', cls: 'ok', url: PSU.playUrl(d.url), seats: seats, remaining: left, note: '' };
  };

  /* Which Polyseat-card buttons may be pressed. Mirrors what the controller and lib.php enforce (they are the guards):
     a session starts only where the build lets an admin's click approve it, never while one runs or Incus is up. */
  PSU.playButtons = function (status, playResp) {
    var off = { start: false, stop: false, recover: false };
    if (!status || status.release_gate === 'closed' || !playResp || !playResp.ok) { return off; }
    var state = PSU.playState(playResp), owner = status.approval_mode === 'owner';
    return {
      start: owner && state === 'idle' && (status.state === 'ACTIVE' || status.state === 'ABSENT'),
      stop: state === 'running',
      /* only an abnormal end that the controller says needs recovery, and only where an admin's click may approve it */
      recover: owner && state === 'needs-recovery'
    };
  };

  /* A Start or Stop that was just confirmed stays disabled, and says what it is doing, until the play state changes: a second
     click on a slow Start would be a second start. pending maps 'start' | 'stop' to the time of the click. */
  var PENDING_TEXT = { start: 'Starting\u2026', stop: 'Stopping\u2026' };
  var PENDING_LABEL = { start: 'Start', stop: 'Stop' };
  var PENDING_MAX_MS = { start: 180000, stop: 360000 }; /* a start that fails between two polls must not lock the button for good */

  PSU.settlePending = function (pending, playResp, now) {
    var out = {};
    var known = !!playResp && !!playResp.ok;
    var state = PSU.playState(playResp);
    Object.keys(pending || {}).forEach(function (k) {
      var since = pending[k];
      if (!PENDING_TEXT[k] || typeof since !== 'number' || now - since > PENDING_MAX_MS[k]) { return; }
      if (known && ((k === 'start' && state !== 'idle') || (k === 'stop' && state !== 'running'))) { return; }
      out[k] = since;
    });
    return out;
  };

  PSU.playButtonView = function (key, allowed, pending) {
    if (pending && typeof pending[key] === 'number') { return { disabled: true, text: PENDING_TEXT[key] }; }
    return { disabled: !allowed, text: PENDING_LABEL[key] };
  };

  /* resp is the API reply of action=workloads: data is null in a build without the workloads daemon. Mirrors
     psu_workloads_view() in lib.php. */
  PSU.workloadsView = function (resp) {
    var states = {
      stopped: ['Stopped', 'muted'], starting: ['Starting', 'warn'], running: ['Running', 'ok'], degraded: ['Degraded', 'warn'],
      stopping: ['Stopping', 'warn'], failed: ['Failed', 'crit'], orphaned: ['Daemon without supervisor', 'crit']
    };
    if (!resp || !resp.ok) { return { available: true, text: 'Workloads status unavailable', cls: 'warn', instances: [], violations: [], note: '' }; }
    var d = resp.data;
    if (!d) { return { available: false, text: 'Not in this build', cls: 'muted', instances: [], violations: [], note: '' }; }
    var st = states[d.state] || states.stopped;
    var text = st[0], cls = st[1];
    if (d.state === 'running' && d.policy_ok === false) { text = 'Running, outside the policy'; cls = 'crit'; }
    return { available: true, text: text, cls: cls, state: d.state, enabled: !!d.enabled, instances: Array.isArray(d.instances) ? d.instances : [],
             violations: Array.isArray(d.violations) ? d.violations : [], note: d.note || '', cleanStop: d.clean_stop };
  };

  /* Which Workloads-card buttons may be pressed; the controller and lib.php are the guards. The daemon starts where an admin's
     click is the approval; stop is open while anything of it may run, including a daemon whose supervisor is gone. */
  PSU.workloadsButtons = function (status, resp) {
    var off = { start: false, stop: false };
    if (!status || status.release_gate === 'closed' || status.approval_mode !== 'owner' || !resp || !resp.ok || !resp.data) { return off; }
    var state = resp.data.state;
    return { start: state === 'stopped' || state === 'failed',
             stop: state === 'starting' || state === 'running' || state === 'degraded' || state === 'orphaned' };
  };

  /* metrics.persistent: where the plugin keeps data that outlives a session, with the room left there. Only well-formed rows. */
  PSU.persistentRows = function (rows) {
    if (!Array.isArray(rows)) { return []; }
    return rows.filter(function (r) {
      return r && typeof r.name === 'string' && typeof r.path === 'string' && typeof r.fstype === 'string' &&
        typeof r.total_kb === 'number' && typeof r.free_kb === 'number';
    }).slice(0, 8).map(function (r) {
      return { name: r.name, path: r.path, fstype: r.fstype, total: PSU.fmtKb(r.total_kb), free: PSU.fmtKb(r.free_kb) };
    });
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
    var sv = PSU.stateView(status), gv = PSU.gateView(status && status.release_gate, status && status.approval_mode);
    setPill(scope.querySelector('[data-bind="state-pill"]'), sv);
    setPill(scope.querySelector('[data-bind="gate-pill"]'), gv);
    setText(scope, 'version', (status && status.version) || '-');
    setText(scope, 'state-text', sv.text);
    var iso = PSU.isolationText(status);
    if (iso) { setText(scope, 'iso-network', iso.network); setText(scope, 'iso-gpu', iso.gpu); setText(scope, 'iso-data', iso.data); }
    var tileNote = scope.querySelector('[data-bind="tile-note"]');
    if (tileNote && iso) { tileNote.textContent = 'Owner mode: sessions are started and stopped on the control page'; }
  }

  function paintPlay(scope, resp) {
    var v = PSU.playView(resp);
    var pills = scope.querySelectorAll('[data-bind="play-pill"]');
    for (var i = 0; i < pills.length; i++) { setPill(pills[i], v); }
    setText(scope, 'play-url', v.url || '-');
    setText(scope, 'play-seats', v.seats);
    setText(scope, 'play-remaining', v.remaining);
    setText(scope, 'play-note', v.note || '-');
    setText(scope, 'play-text', v.text);
    var opens = scope.querySelectorAll('[data-bind="play-open"]');
    for (var j = 0; j < opens.length; j++) {
      if (v.url) { opens[j].setAttribute('href', v.url); } else { opens[j].removeAttribute('href'); }
      opens[j].hidden = !v.url;
    }
  }

  function paintWorkloads(scope, resp) {
    var v = PSU.workloadsView(resp);
    var card = scope.querySelector('[data-bind="workloads-card"]');
    if (card) { card.hidden = !v.available; }
    var pills = scope.querySelectorAll('[data-bind="workloads-pill"]');
    for (var i = 0; i < pills.length; i++) { setPill(pills[i], v); }
    setText(scope, 'workloads-note', v.note || (v.violations.length ? v.violations.join('; ') : ''));
    var body = scope.querySelector('[data-bind="workloads-instances"]');
    if (!body) { return; }
    body.textContent = '';
    if (!v.instances.length) {
      body.appendChild(h('tr', {}, [h('td', { colspan: '3', class: 'psu-muted', text: v.state === 'stopped' ? 'Not running.' : 'No instances.' })]));
    }
    v.instances.forEach(function (inst) {
      body.appendChild(h('tr', {}, [
        h('td', { 'data-label': 'Instance', text: inst.name }),
        h('td', { 'data-label': 'State', text: inst.status + (inst.autostart ? ' (autostart)' : '') }),
        h('td', { 'data-label': 'Address', text: (inst.ips && inst.ips.length) ? inst.ips.join(', ') : '-' })
      ]));
    });
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
    /* The directories that really hold the data of an owner build (the DATA_DIR setting above is the old, bounded canary's). */
    var persistent = PSU.persistentRows(m.persistent);
    var meter = scope.querySelector('[data-bind="disk-meter"]');
    if (meter) { meter.hidden = !m.data_dir && persistent.length > 0; }
    var list = scope.querySelector('[data-bind="persistent"]');
    if (list) {
      list.textContent = '';
      persistent.forEach(function (row) {
        list.appendChild(h('dt', { text: row.name }));
        list.appendChild(h('dd', { text: row.path + ': ' + row.free + ' free of ' + row.total + ' (' + row.fstype + ')' }));
      });
      list.hidden = !persistent.length;
    }
  }

  function poll(scope, intervalMs) {
    var timer = null;
    function tick() {
      if (document.hidden) { return; }
      Promise.all([PSU.api('status'), PSU.api('metrics'), PSU.api('play'), PSU.api('workloads')]).then(function (r) {
        if (r[0].ok) { paintStatus(scope, r[0].data); cfg.gate = r[0].data.release_gate; scope.dispatchEvent(new CustomEvent('psu:status', { detail: r[0].data })); }
        else { paintStatus(scope, null); }
        if (r[1].ok) { paintMetrics(scope, r[1].data); }
        paintPlay(scope, r[2]);
        paintWorkloads(scope, r[3]);
      });
    }
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { tick(); } });
    tick();
    timer = setInterval(tick, intervalMs);
    return function stop() { clearInterval(timer); };
  }

  PSU.initTile = function (scope, config) {
    Object.assign(cfg, config || {});
    scope = scope.closest('tbody') || scope; /* the header and quick links live outside the body cell */
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
      /* the button is off from here on, before the request is even sent */
      if (opts.pending) { scope.dispatchEvent(new CustomEvent('psu:pending', { detail: { key: opts.pending, on: true } })); }
      return PSU.post(action, f).then(function (resp) {
        notify(scope, resp.ok ? 'ok' : 'crit', resp.ok ? label + ' completed.' : PSU.errorText(resp));
        /* a refused or failed request changes no state: nothing to wait for, the button is free again */
        if (opts.pending && !resp.ok) { scope.dispatchEvent(new CustomEvent('psu:pending', { detail: { key: opts.pending, on: false } })); }
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
      /* an owner build activates a bundle only as part of starting a session */
      btn.disabled = locked || b.current || !b.has_archive || !b.has_signature || (status && status.approval_mode === 'owner');
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
    var lastPlay = null;
    var lastWorkloads = null;
    var pending = {};

    function refreshAll() {
      Promise.all([PSU.api('status'), PSU.api('metrics'), PSU.api('list'), PSU.api('play'), PSU.api('workloads')]).then(function (r) {
        if (r[0].ok) { lastStatus = r[0].data; paintStatus(scope, lastStatus); cfg.gate = lastStatus.release_gate; applyLocks(); }
        else { paintStatus(scope, null); }
        if (r[1].ok) { paintMetrics(scope, r[1].data); }
        lastPlay = r[3];
        lastWorkloads = r[4];
        pending = PSU.settlePending(pending, lastPlay, Date.now());
        renderBundles(scope, r[2], lastStatus);
        paintPlay(scope, r[3]);
        paintWorkloads(scope, r[4]);
        applyLocks();
      });
    }

    function applyLocks() {
      var locked = !lastStatus || lastStatus.release_gate === 'closed';
      var banner = scope.querySelector('[data-bind="gate-banner"]');
      if (banner) { banner.hidden = !locked; }
      var state = lastStatus && lastStatus.state;
      /* In an owner build a session is started from the Polyseat card; the service itself is not started on its own. */
      var owner = !!lastStatus && lastStatus.approval_mode === 'owner';
      var map = { start: state === 'ACTIVE' && !owner, stop: state === 'RUNNING', restart: state === 'RUNNING' && !owner,
                  rollback: state === 'RUNNING' || state === 'ACTIVE', recover: state === 'JOURNAL_PENDING' };
      Object.keys(map).forEach(function (k) {
        var b = scope.querySelector('[data-do="' + k + '"]');
        if (b) { b.disabled = locked || !map[k]; }
      });
      var play = PSU.playButtons(lastStatus, lastPlay);
      Object.keys(play).forEach(function (k) {
        var b = scope.querySelector('[data-do="play-' + k + '"]');
        if (!b) { return; }
        if (k === 'start' || k === 'stop') {
          var view = PSU.playButtonView(k, play[k], pending);
          b.disabled = view.disabled;
          b.textContent = view.text;
        } else {
          b.disabled = !play[k];
        }
      });
      /* an owner build has no service of its own to start, restart or stop: the Polyseat and Workloads cards are the controls */
      var svc = scope.querySelector('[data-bind="service-actions"]');
      if (svc) { svc.hidden = owner; }
      var note = scope.querySelector('[data-bind="service-note"]');
      if (note) { note.hidden = !owner; }
      /* Wiederherstellen exists on the card only while an abnormal end needs it */
      var rec = scope.querySelector('[data-do="play-recover"]');
      if (rec) { rec.hidden = !play.recover; }
      var wl = PSU.workloadsButtons(lastStatus, lastWorkloads);
      Object.keys(wl).forEach(function (k) {
        var b = scope.querySelector('[data-do="workloads-' + k + '"]');
        if (b) { b.disabled = !wl[k]; }
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
    scope.addEventListener('psu:pending', function (e) {
      var d = e.detail || {};
      var next = Object.assign({}, pending);
      if (d.on) { next[d.key] = Date.now(); } else { delete next[d.key]; }
      pending = next;
      applyLocks();
    });
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
    var ps = scope.querySelector('[data-do="play-start"]');
    if (ps) { ps.addEventListener('click', function () {
      if (pending.start) { return; }
      runAction(scope, 'Start play session', 'play_start', {}, '', { alwaysConfirm: true, pending: 'start',
        body: 'Verify the runtime, seat image and daemon on the flash drive, stage them and start a play session. ' +
          'This takes up to a minute; the session ends by itself after the configured length.' });
    }); }
    var pp = scope.querySelector('[data-do="play-stop"]');
    if (pp) { pp.addEventListener('click', function () {
      if (pending.stop) { return; }
      runAction(scope, 'Stop play session', 'play_stop', {}, '', { danger: true, pending: 'stop',
        body: 'End the play session: players are disconnected, seats stop in order, Incus records a clean stop and the host is restored.' });
    }); }
    var pr = scope.querySelector('[data-do="play-recover"]');
    if (pr) { pr.addEventListener('click', function () {
      runAction(scope, 'Wiederherstellen', 'play_recover', {}, '', { danger: true,
        body: 'Die letzte Session wurde nicht sauber beendet. Zuerst wird geprüft, dass nichts mehr läuft (kein incusd, kein polyseatd, ' +
          'kein Prozess in einem Seat oder in den Polyseat-cgroups, keine Mounts unter dem Datenverzeichnis) und dass keine Instanz ' +
          'automatisch startet. Erst danach wird der Host aus den Aufzeichnungen zurückgesetzt (Konsole, SysRq, CPU-Energieprofil, udev) ' +
          'und der saubere Stopp für Incus eingetragen. Schlägt eine Prüfung fehl, bleibt alles unverändert.' });
    }); }
    var ws = scope.querySelector('[data-do="workloads-start"]');
    if (ws) { ws.addEventListener('click', function () {
      runAction(scope, 'Start workloads', 'workloads_start', {}, '', { alwaysConfirm: true,
        body: 'Start the permanent workloads daemon. Its containers that are set to autostart start with it; nothing else changes.' });
    }); }
    var wp = scope.querySelector('[data-do="workloads-stop"]');
    if (wp) { wp.addEventListener('click', function () {
      runAction(scope, 'Stop workloads', 'workloads_stop', {}, '', { danger: true,
        body: 'Stop the workloads daemon: every container is shut down cleanly (up to 2 minutes each), then Incus stops. ' +
          'The services in those containers are unavailable until the daemon is started again.' });
    }); }
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
