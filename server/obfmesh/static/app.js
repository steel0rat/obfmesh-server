/* obfmesh web UI. No build step, no dependencies.
 * Secrets (API key, client token, obfuscation key, WireGuard keys) are never
 * written to console, URLs or storage other than the explicit key box below.
 */
'use strict';

(function () {

  // ---------------------------------------------------------------- constants

  var MAX_SPOKES = 10;
  var SPOKE_CEILING_MBPS = 180;   // measured ceiling of one spoke, and of one TCP stream
  var OPTIMAL_SPOKES = 2;         // measured optimum for a NanoPi R3S class client
  var HANDSHAKE_FRESH_S = 180;    // wg keepalive/rekey window
  var STATUS_POLL_MS = 4000;      // counters are only readable by polling
  var SSE_BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000];
  var KEY_STORE = 'obfmesh.apikey';
  var PORT_BASE_DEFAULT = 48200;  // SPEC: server port = port_base + i
  var WG_PORT_BASE = 51820;       // SPEC: internal wireguard port = 51820 + i

  // Measured on the live stand, never extrapolated: the limit is the router's
  // CPU, so more spokes past the optimum make the total worse, not better.
  var MEASURED_MBPS = { 1: '~180 Мбит/с', 2: '359–373 Мбит/с', 3: '210–222 Мбит/с' };

  // ---------------------------------------------------------------- state

  var state = {
    apiKey: null,
    status: null,
    settings: null,
    clients: [],
    clientsLoaded: false,
    samples: new Map(),   // spoke index -> { t, rx, tx } for rate deltas
    rates: new Map(),     // spoke index -> { rx, tx } bits/s
    lastUpdate: null,
    conn: 'idle',         // idle | online | pending | offline
    serverOk: null,       // last HTTP call reachable?
    sseUp: false,         // event stream currently attached?
    sseRunning: false,
    sseAttempt: 0,
    sseOpenedAt: 0,
    sseCtrl: null,
    retryAt: 0,
    formTouched: false,
    busy: false,
    pollTimer: null,
    tickTimer: null,
    refreshTimer: null,
    staleTimer: null,
    confirmResolve: null,
    tokenClient: null
  };

  // ---------------------------------------------------------------- dom utils

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'class') node.className = attrs[k];
        else if (k === 'text') node.textContent = attrs[k];
        else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
      });
    }
    (kids || []).forEach(function (kid) {
      if (kid === null || kid === undefined) return;
      node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
    });
    return node;
  }

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var XLINK_NS = 'http://www.w3.org/1999/xlink';

  function icon(id) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#' + id);
    use.setAttributeNS(XLINK_NS, 'xlink:href', '#' + id);
    svg.appendChild(use);
    return svg;
  }

  function setText(sel, value) {
    var node = typeof sel === 'string' ? $(sel) : sel;
    if (node) node.textContent = value;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // ---------------------------------------------------------------- format

  var nf0 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
  var nf1 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
  var dtf = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
  var tf = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  var DASH = '—';

  function plural(n, forms) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return forms[2];
    if (b > 1 && b < 5) return forms[1];
    if (b === 1) return forms[0];
    return forms[2];
  }

  function spokeWord(n) { return plural(n, ['луч', 'луча', 'лучей']); }

  function fmtBytes(v) {
    if (v === null || v === undefined || !isFinite(v)) return DASH;
    var units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ', 'ПБ'], i = 0, n = v;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? nf0.format(n) : nf1.format(n)) + ' ' + units[i];
  }

  // bits per second -> human string
  function fmtRate(bps) {
    if (bps === null || bps === undefined || !isFinite(bps)) return DASH;
    var units = ['бит/с', 'Кбит/с', 'Мбит/с', 'Гбит/с'], i = 0, n = bps;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    return (i === 0 ? nf0.format(n) : nf1.format(n)) + ' ' + units[i];
  }

  function fmtAgo(sec) {
    if (sec === null || sec === undefined || !isFinite(sec)) return DASH;
    if (sec < 0) sec = 0;
    if (sec < 5) return 'только что';
    if (sec < 60) return Math.round(sec) + ' с назад';
    if (sec < 3600) return Math.round(sec / 60) + ' мин назад';
    if (sec < 86400) return Math.round(sec / 3600) + ' ч назад';
    var d = Math.round(sec / 86400);
    return d + ' ' + plural(d, ['день', 'дня', 'дней']) + ' назад';
  }

  function fmtDate(v) {
    var d = toDate(v);
    return d ? dtf.format(d) : DASH;
  }

  function toDate(v) {
    if (v === null || v === undefined || v === '') return null;
    if (typeof v === 'number') return new Date(v > 1e11 ? v : v * 1000);
    var s = String(v);
    // naive ISO without zone designator is server-local UTC in FastAPI dumps
    var iso = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(s) ? s + 'Z' : s;
    var d = new Date(iso);
    return isNaN(d.getTime()) ? null : d;
  }

  // ---------------------------------------------------------------- tolerant field access

  function pick(obj, names) {
    if (!obj || typeof obj !== 'object') return null;
    for (var i = 0; i < names.length; i++) {
      var v = obj[names[i]];
      if (v !== undefined && v !== null && v !== '') return v;
    }
    return null;
  }

  function pickNum(obj, names) {
    var v = pick(obj, names);
    if (v === null) return null;
    var n = typeof v === 'number' ? v : Number(v);
    return isFinite(n) ? n : null;
  }

  function pickBool(obj, names) {
    if (!obj || typeof obj !== 'object') return null;
    for (var i = 0; i < names.length; i++) {
      var v = obj[names[i]];
      if (v === undefined || v === null) continue;
      if (typeof v === 'boolean') return v;
      if (typeof v === 'number') return v !== 0;
      if (typeof v === 'string') {
        var s = v.toLowerCase();
        if (s === 'up' || s === 'true' || s === 'running' || s === 'active' || s === 'ok') return true;
        if (s === 'down' || s === 'false' || s === 'stopped' || s === 'inactive') return false;
        continue;
      }
      if (typeof v === 'object') {
        var inner = pickBool(v, ['up', 'running', 'active', 'alive', 'ok']);
        if (inner !== null) return inner;
      }
    }
    return null;
  }

  function spokeList(status) {
    if (!status) return [];
    var direct = status.spokes;
    if (Array.isArray(direct)) return direct;
    var alt = pick(status, ['spoke_status', 'spoke_states', 'spokes_status', 'beams', 'tunnels', 'links']);
    return Array.isArray(alt) ? alt : [];
  }

  function spokeIndex(sp, fallback) {
    var n = pickNum(sp, ['index', 'i', 'id', 'num', 'spoke']);
    return n === null ? fallback : n;
  }

  function handshakeAge(sp, nowSec) {
    var age = pickNum(sp, ['handshake_age', 'last_handshake_age', 'handshake_ago', 'seconds_since_handshake']);
    if (age !== null) return age;
    var ts = pickNum(sp, ['latest_handshake', 'last_handshake', 'handshake', 'latest_handshake_at']);
    if (ts === null || ts === 0) return null;
    if (ts > 1e11) ts = ts / 1000;   // milliseconds
    return nowSec - ts;
  }

  // ---------------------------------------------------------------- api

  function ApiError(status, message, body) {
    var e = new Error(message);
    e.name = 'ApiError';
    e.status = status;
    e.body = body;
    return e;
  }

  function basePath() {
    var p = location.pathname;
    if (p.charAt(p.length - 1) === '/') return p;
    var last = p.slice(p.lastIndexOf('/') + 1);
    return last.indexOf('.') !== -1 ? p.slice(0, p.lastIndexOf('/') + 1) : p + '/';
  }

  function apiUrl(path) { return basePath() + path; }

  function headers(extra) {
    var h = extra || {};
    // API key travels in a header, never in the URL — URLs land in access logs.
    if (state.apiKey) h['X-API-Key'] = state.apiKey;
    return h;
  }

  function rawFetch(path, opts) {
    opts = opts || {};
    var init = {
      method: opts.method || 'GET',
      headers: headers(opts.headers || {}),
      cache: 'no-store',
      credentials: 'same-origin'
    };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    if (opts.signal) init.signal = opts.signal;
    return fetch(apiUrl(path), init);
  }

  function errorMessage(res, payload) {
    if (payload && typeof payload === 'object') {
      var d = payload.detail !== undefined ? payload.detail : payload.message;
      if (typeof d === 'string' && d) return d;
      if (Array.isArray(d) && d.length && d[0] && d[0].msg) return String(d[0].msg);
    }
    if (typeof payload === 'string' && payload && payload.length < 300) return payload;
    return 'HTTP ' + res.status + ' ' + (res.statusText || '');
  }

  function api(path, opts) {
    return rawFetch(path, opts).then(function (res) {
      var ct = res.headers.get('content-type') || '';
      var parse = ct.indexOf('application/json') !== -1 ? res.json() : res.text();
      return parse.catch(function () { return null; }).then(function (payload) {
        if (res.ok) return payload;
        if (res.status === 401 || res.status === 403) {
          onAuthLost();
          throw ApiError(res.status, 'Ключ не принят сервером', payload);
        }
        throw ApiError(res.status, errorMessage(res, payload), payload);
      });
    });
  }

  // ---------------------------------------------------------------- auth

  function loadStoredKey() {
    try {
      return sessionStorage.getItem(KEY_STORE) || localStorage.getItem(KEY_STORE) || null;
    } catch (e) { return null; }
  }

  function storeKey(key, persist) {
    try {
      sessionStorage.setItem(KEY_STORE, key);
      if (persist) localStorage.setItem(KEY_STORE, key);
      else localStorage.removeItem(KEY_STORE);
    } catch (e) { /* private mode: key lives in memory only */ }
  }

  function forgetKey() {
    try {
      sessionStorage.removeItem(KEY_STORE);
      localStorage.removeItem(KEY_STORE);
    } catch (e) { /* nothing to clean */ }
    state.apiKey = null;
  }

  function onAuthLost() {
    if ($('#gate').hidden === false) return;
    forgetKey();
    stopSse();
    stopPolling();
    showGate('Сервер отклонил ключ. Введите действующий API-ключ.');
  }

  function showGate(message) {
    $('#app').hidden = true;
    $('#gate').hidden = false;
    var err = $('#gate-error');
    err.hidden = !message;
    err.textContent = message || '';
    $('#gate-key').value = '';
    $('#gate-key').focus();
  }

  function hideGate() {
    $('#gate').hidden = true;
    $('#app').hidden = false;
    $('#btn-logout').hidden = !state.apiKey;
  }

  // ---------------------------------------------------------------- rates

  function updateRates(spokes) {
    var now = Date.now();
    var seen = new Set();
    spokes.forEach(function (sp, i) {
      var idx = spokeIndex(sp, i + 1);
      seen.add(idx);
      var rx = pickNum(sp, ['rx_bytes', 'rx', 'bytes_rx', 'received', 'transfer_rx', 'rx_total']);
      var tx = pickNum(sp, ['tx_bytes', 'tx', 'bytes_tx', 'sent', 'transfer_tx', 'tx_total']);
      if (rx === null || tx === null) { state.rates.delete(idx); return; }
      var prev = state.samples.get(idx);
      if (prev) {
        var dt = (now - prev.t) / 1000;
        // counters reset when an interface is recreated: skip that interval
        if (dt >= 0.5 && rx >= prev.rx && tx >= prev.tx) {
          state.rates.set(idx, { rx: (rx - prev.rx) * 8 / dt, tx: (tx - prev.tx) * 8 / dt });
        } else if (rx < prev.rx || tx < prev.tx) {
          state.rates.delete(idx);
        }
      }
      state.samples.set(idx, { t: now, rx: rx, tx: tx });
    });
    state.samples.forEach(function (_, idx) { if (!seen.has(idx)) state.samples.delete(idx); });
    state.rates.forEach(function (_, idx) { if (!seen.has(idx)) state.rates.delete(idx); });
  }

  // ---------------------------------------------------------------- status view

  function spokeVerdict(sp, nowSec) {
    var up = pickBool(sp, ['up', 'is_up', 'interface_up', 'iface_up', 'wg_up', 'active', 'state']);
    var obf = pickBool(sp, ['obfuscator_up', 'obf_up', 'obfuscator_running', 'obfuscator', 'process_up', 'proc_up']);
    var age = handshakeAge(sp, nowSec);

    if (up === false && obf === false) return { kind: 'critical', icon: 'i-stop', label: 'остановлен' };
    if (up === false) return { kind: 'critical', icon: 'i-stop', label: 'интерфейс не поднят' };
    if (obf === false) return { kind: 'critical', icon: 'i-warn', label: 'обфускатор не запущен' };
    if (up === null && obf === null && age === null) return { kind: 'muted', icon: 'i-dash', label: 'нет данных' };
    if (age === null) return { kind: 'warning', icon: 'i-clock', label: 'ждёт клиента' };
    if (age <= HANDSHAKE_FRESH_S) return { kind: 'good', icon: 'i-check', label: 'работает' };
    return { kind: 'warning', icon: 'i-warn', label: 'рукопожатие устарело' };
  }

  function badge(verdict) {
    return el('span', { class: 'badge badge--' + verdict.kind }, [icon(verdict.icon), verdict.label]);
  }

  function meter(rateBps) {
    var wrap = el('div', { class: 'meter' });
    var fill = el('div', { class: 'meter__fill' });
    var ratio = rateBps === null ? 0 : rateBps / (SPOKE_CEILING_MBPS * 1e6);
    if (ratio > 1) ratio = 1;
    if (ratio >= 0.9) fill.className = 'meter__fill meter__fill--critical';
    else if (ratio >= 0.7) fill.className = 'meter__fill meter__fill--warning';
    fill.style.width = (ratio * 100).toFixed(1) + '%';
    wrap.appendChild(fill);
    return wrap;
  }

  function renderStatus() {
    var st = state.status;
    var body = $('#spokes-body');
    var nowSec = Date.now() / 1000;
    var spokes = spokeList(st);
    var settings = state.settings || {};
    var portBase = pickNum(settings, ['port_base']) || PORT_BASE_DEFAULT;

    clear(body);

    if (!st) {
      body.appendChild(el('tr', null, [el('td', { colspan: '6', class: 'empty', text: 'Нет данных от сервера' })]));
    } else if (!spokes.length) {
      body.appendChild(el('tr', null, [el('td', { colspan: '6', class: 'empty', text: 'Лучи не подняты' })]));
    }

    var upCount = 0, totalBytes = 0, sumRx = 0, sumTx = 0, haveRates = false, haveCounters = false;

    spokes.forEach(function (sp, i) {
      var idx = spokeIndex(sp, i + 1);
      var verdict = spokeVerdict(sp, nowSec);
      var carrying = verdict.kind === 'good' || verdict.kind === 'warning';
      if (carrying) upCount++;

      var iface = pick(sp, ['interface', 'iface', 'name', 'wg']) || ('swg' + idx);
      var srvPort = pickNum(sp, ['server_port', 'obf_port', 'external_port']);
      if (srvPort === null) srvPort = portBase + idx;
      var wgPort = pickNum(sp, ['wg_port', 'listen_port', 'internal_port']);
      if (wgPort === null) wgPort = WG_PORT_BASE + idx;

      var rx = pickNum(sp, ['rx_bytes', 'rx', 'bytes_rx', 'received', 'transfer_rx', 'rx_total']);
      var tx = pickNum(sp, ['tx_bytes', 'tx', 'bytes_tx', 'sent', 'transfer_tx', 'tx_total']);
      if (rx !== null) { totalBytes += rx; haveCounters = true; }
      if (tx !== null) { totalBytes += tx; haveCounters = true; }

      var rate = state.rates.get(idx) || null;
      if (rate) { haveRates = true; sumRx += rate.rx; sumTx += rate.tx; }

      var age = handshakeAge(sp, nowSec);
      var load = rate ? Math.max(rate.rx, rate.tx) : null;
      var pct = load === null ? null : Math.min(100, load / (SPOKE_CEILING_MBPS * 1e6) * 100);

      // Подсеть берётся из ответа: на swg{i} висит /24, внутри которого клиентам
      // нарезаны /30-слоты, и рисовать здесь /30 было бы неправдой.
      var subnet = pick(sp, ['subnet', 'network']) || ('10.77.' + idx + '.0/24');

      var tdSpoke = el('td', null, [
        el('span', { class: 'spoke-name', text: 'Луч ' + idx + ' · ' + iface }),
        el('span', { class: 'col-sub', text: srvPort + ' → ' + wgPort + ' · ' + subnet })
      ]);

      var tdRx = el('td', { class: 'num' }, [
        document.createTextNode(fmtBytes(rx)),
        el('span', { class: 'col-sub', text: rate ? fmtRate(rate.rx) : (rx === null ? '' : 'скорость считается…') })
      ]);

      var tdTx = el('td', { class: 'num' }, [
        document.createTextNode(fmtBytes(tx)),
        el('span', { class: 'col-sub', text: rate ? fmtRate(rate.tx) : (tx === null ? '' : 'скорость считается…') })
      ]);

      var tdLoad = el('td', { class: 'num' }, [
        document.createTextNode(pct === null ? DASH : nf0.format(pct) + '% от ' + SPOKE_CEILING_MBPS + ' Мбит/с'),
        meter(load)
      ]);

      body.appendChild(el('tr', null, [
        tdSpoke,
        el('td', null, [badge(verdict)]),
        el('td', { class: 'num' }, [
          document.createTextNode(fmtAgo(age)),
          el('span', { class: 'col-sub', text: age === null ? 'ещё не было' : '' })
        ]),
        tdRx, tdTx, tdLoad
      ]));
    });

    // hero: what the client actually downloads = what the server sends out
    setText('#hero-value', haveRates ? nf1.format(sumTx / 1e6) : DASH);
    setText('#hero-tx', haveRates ? fmtRate(sumTx) : DASH);
    setText('#hero-rx', haveRates ? fmtRate(sumRx) : DASH);
    // Every spoke carries traffic, but only for the consumers bound to it: the
    // ceiling is reachable only when they are spread over the spokes.
    setText('#hero-ceiling', upCount
      ? 'потолок сейчас ≈ ' + (upCount * SPOKE_CEILING_MBPS) + ' Мбит/с (' +
        upCount + ' × ' + SPOKE_CEILING_MBPS + '), если потребители разведены по лучам'
      : (spokes.length ? 'нет лучей, несущих трафик' : 'нет поднятых лучей'));

    var configured = spokes.length || pickNum(st, ['spokes', 'spoke_count', 'n_spokes']) || 0;
    setText('#tile-spokes', upCount + ' / ' + configured);
    setText('#tile-spokes-sub', configured ? 'настроено ' + configured + ' ' + spokeWord(configured) : DASH);
    setText('#tile-total', fmtBytes(haveCounters ? totalBytes : null));
    setText('#tile-clients', state.clientsLoaded ? String(state.clients.length) : DASH);
    setText('#tile-clients-sub', state.clients.length ? 'токен виден один раз при создании' : 'ни одного клиента');

    var version = pickNum(st, ['config_version', 'version']);
    setText('#version-value', version === null ? DASH : String(version));
    setText('#masking-chip', 'маскировка ' + (pick(st, ['masking']) || pick(settings, ['masking']) || DASH));

    var host = pick(st, ['external_host', 'host', 'server_host']) || pick(settings, ['external_host']);
    setText('#brand-host', host || location.host);

    renderMeta(st, settings, spokes, portBase);
    renderStatusNotices(st, spokes, upCount);
  }

  function renderMeta(st, settings, spokes, portBase) {
    var line = $('#meta-line');
    clear(line);
    var host = pick(st, ['external_host', 'host', 'server_host']) || pick(settings, ['external_host']);
    var mtu = pickNum(st, ['mtu']) !== null ? pickNum(st, ['mtu']) : pickNum(settings, ['mtu']);
    var items = [];
    if (host) items.push(['сервер', String(host)]);
    if (mtu !== null) items.push(['MTU', String(mtu)]);
    if (spokes.length) {
      items.push(['порты сервера', (portBase + 1) + (spokes.length > 1 ? '–' + (portBase + spokes.length) : '')]);
      items.push(['подсети', '10.77.1.0/24' + (spokes.length > 1 ? ' … 10.77.' + spokes.length + '.0/24' : '')]);
    }
    if (state.lastUpdate) items.push(['обновлено', tf.format(state.lastUpdate)]);
    items.forEach(function (pair) {
      line.appendChild(el('span', null, [pair[0] + ' ', el('b', { text: pair[1] })]));
    });
  }

  function notice(kind, iconId, message, actionLabel, onAction) {
    var kids = [icon(iconId), el('span', { text: message })];
    if (actionLabel) {
      var btn = el('button', { type: 'button', class: 'btn btn--ghost btn--sm', text: actionLabel });
      btn.addEventListener('click', onAction);
      kids.push(btn);
    }
    return el('div', { class: 'notice notice--' + kind }, kids);
  }

  function renderStatusNotices(st, spokes, upCount) {
    var box = $('#status-notices');
    clear(box);
    if (!st) return;

    var broken = [];
    var nowSec = Date.now() / 1000;
    spokes.forEach(function (sp, i) {
      if (spokeVerdict(sp, nowSec).kind === 'critical') broken.push(spokeIndex(sp, i + 1));
    });
    if (broken.length) {
      box.appendChild(notice('critical', 'i-warn',
        'Не работают лучи: ' + broken.join(', ') + '. Трафик идёт по оставшимся, скорость ниже расчётной.'));
    }

    if (upCount > OPTIMAL_SPOKES) {
      box.appendChild(notice('warning', 'i-info',
        'Поднято ' + upCount + ' ' + spokeWord(upCount) + ', а замер даёт оптимум в два: на трёх лучах вышло ' +
        '210–222 Мбит/с против 359–373 на двух. Лишний луч — ещё один обфускатор на процессоре клиента.'));
    }

    if (upCount > 1) {
      box.appendChild(notice('warning', 'i-info',
        'Скорость складывается только тогда, когда потребители разведены по лучам: ' +
        'bind_interface owg1 у одной секции, bind_interface owg2 у другой. Один поток остаётся в одном луче.'));
    }
  }

  // ---------------------------------------------------------------- settings form

  function formValues() {
    var mtuRaw = $('#set-mtu').value.trim();
    return {
      spokes: Number($('#spokes-range').value),
      external_host: $('#set-host').value.trim(),
      mtu: mtuRaw === '' ? null : Number(mtuRaw)
    };
  }

  function fillForm(s) {
    var spokes = pickNum(s, ['spokes', 'spoke_count']);
    if (spokes === null) spokes = 1;
    spokes = Math.min(MAX_SPOKES, Math.max(1, Math.round(spokes)));
    $('#spokes-range').value = String(spokes);

    $('#set-host').value = pick(s, ['external_host', 'host']) || '';
    var mtu = pickNum(s, ['mtu']);
    $('#set-mtu').value = mtu === null ? '' : String(mtu);

    state.formTouched = false;
    onFormInput();
  }

  function serverValues() {
    var s = state.settings || {};
    var spokes = pickNum(s, ['spokes', 'spoke_count']);
    var mtu = pickNum(s, ['mtu']);
    return {
      spokes: spokes === null ? null : Math.round(spokes),
      external_host: pick(s, ['external_host', 'host']) || '',
      mtu: mtu
    };
  }

  function diffFields() {
    var now = formValues(), was = serverValues(), out = [];
    if (was.spokes !== null && now.spokes !== was.spokes) {
      out.push({
        field: 'spokes', value: now.spokes,
        label: 'лучей', from: String(was.spokes), to: String(now.spokes),
        shrink: now.spokes < was.spokes,
        // above the measured optimum an extra spoke takes more CPU than it gives back
        warn: now.spokes < was.spokes || now.spokes > OPTIMAL_SPOKES
      });
    }
    if (now.external_host !== was.external_host && (now.external_host || was.external_host)) {
      out.push({ field: 'external_host', value: now.external_host, label: 'адрес сервера', from: was.external_host || DASH, to: now.external_host || DASH, warn: true });
    }
    if (now.mtu !== was.mtu && !(now.mtu === null && was.mtu === null)) {
      out.push({ field: 'mtu', value: now.mtu, label: 'MTU', from: was.mtu === null ? DASH : String(was.mtu), to: now.mtu === null ? DASH : String(now.mtu), warn: true });
    }
    return out;
  }

  function isDirty() { return state.settings !== null && diffFields().length > 0; }

  function onFormInput() {
    var v = formValues();
    var range = $('#spokes-range');
    range.style.setProperty('--fill', ((v.spokes - 1) / (MAX_SPOKES - 1) * 100) + '%');
    setText('#spokes-value', String(v.spokes));
    setText('#spokes-word', spokeWord(v.spokes));
    $('#spokes-minus').disabled = v.spokes <= 1;
    $('#spokes-plus').disabled = v.spokes >= MAX_SPOKES;

    renderEstimate(v);
    renderDiff();
  }

  function renderEstimate(v) {
    var n = v.spokes;
    var measured = MEASURED_MBPS[n];

    setText('#estimate-value', measured ? 'Замер: ' + measured : 'Замера нет: больше трёх лучей не проверяли');

    if (n === 1) {
      setText('#estimate-note',
        'Один луч, один обфускатор. Это же и потолок одного соединения: распараллелить одну ' +
        'TCP-сессию нельзя, разными лучами набирается только сумма разных сервисов.');
    } else if (n === OPTIMAL_SPOKES) {
      setText('#estimate-note',
        'Оптимум для этого клиента, проверен четырьмя замерами. Скорость складывается только тогда, ' +
        'когда потребители разведены по лучам: bind_interface owg1, bind_interface owg2.');
    } else {
      setText('#estimate-note',
        n + ' ' + spokeWord(n) + ' — это ' + n + ' процесса обфускатора на процессоре клиента, который на ' +
        'двух лучах уже загружен на 92–98 %. На трёх лучах вышло 210–222 Мбит/с против 359–373 на двух; ' +
        'больше двух имеет смысл только на более сильном клиенте.');
    }
  }

  function renderDiff() {
    var box = $('#settings-diff');
    clear(box);
    box.className = 'diff';
    var changes = state.settings ? diffFields() : [];
    var apply = $('#settings-apply');
    var reset = $('#settings-reset');

    apply.disabled = state.busy || changes.length === 0;
    reset.disabled = state.busy || changes.length === 0;

    if (!state.settings) { box.textContent = 'Настройки не загружены'; return; }
    if (!changes.length) { box.textContent = 'Изменений нет'; return; }

    var warn = changes.some(function (c) { return c.warn; });
    box.className = 'diff' + (warn ? ' diff--warning' : '');
    box.appendChild(document.createTextNode('Будет применено: '));
    changes.forEach(function (c, i) {
      if (i) box.appendChild(document.createTextNode(', '));
      box.appendChild(document.createTextNode(c.label + ' '));
      box.appendChild(el('b', { text: c.from }));
      box.appendChild(document.createTextNode(' → '));
      box.appendChild(el('b', { text: c.to }));
    });
    var spokeChange = changes.filter(function (c) { return c.field === 'spokes'; })[0];
    if (spokeChange && spokeChange.shrink) {
      box.appendChild(document.createTextNode('. Лишние лучи будут погашены, остальные не тронуты.'));
    } else if (spokeChange && spokeChange.value > OPTIMAL_SPOKES) {
      box.appendChild(document.createTextNode('. Замер даёт оптимум в два луча: на трёх было 210–222 Мбит/с против 359–373.'));
    }
  }

  function renderSettingsSide() {
    var s = state.settings;
    var chip = $('#key-chip');
    clear(chip);
    if (!s) { chip.appendChild(document.createTextNode(DASH)); return; }
    var hasKey = pick(s, ['key', 'obfuscation_key', 'key_set', 'has_key']) !== null;
    chip.className = 'chip ' + (hasKey ? 'chip--good' : 'chip--warning');
    chip.appendChild(icon(hasKey ? 'i-check' : 'i-warn'));
    chip.appendChild(document.createTextNode(hasKey ? 'задан, не показывается' : 'не задан'));
    chip.setAttribute('title', hasKey
      ? 'Ключ обфускации выдаётся только в бандле клиента и в интерфейсе не отображается'
      : 'Сервер не сообщил ключ обфускации');
  }

  function applySettings(ev) {
    ev.preventDefault();
    var changes = diffFields();
    if (!changes.length || state.busy) return;   // idempotent: nothing changed, nothing sent

    var body = {};
    for (var i = 0; i < changes.length; i++) {
      var c = changes[i];
      if (c.field === 'external_host' && !c.value) { toast('critical', 'Адрес сервера не может быть пустым'); return; }
      if (c.field === 'mtu') {
        if (c.value === null || !isFinite(c.value) || c.value < 1000 || c.value > 1500) {
          toast('critical', 'MTU должен быть числом от 1000 до 1500');
          return;
        }
      }
      body[c.field] = c.value;
    }

    setBusy(true);
    api('api/settings', { method: 'PATCH', body: body })
      .then(function (payload) {
        if (payload && typeof payload === 'object' && pick(payload, ['spokes']) !== null) {
          state.settings = payload;
        }
        toast('good', 'Применено. Сервер перестраивает лучи.');
        return refreshAll({ quiet: true });
      })
      .catch(function (e) {
        if (e.status !== 401 && e.status !== 403) toast('critical', 'Не применилось: ' + e.message);
      })
      .then(function () { setBusy(false); });
  }

  function setBusy(on) {
    state.busy = on;
    $('#settings-form').setAttribute('aria-busy', on ? 'true' : 'false');
    $$('#settings-form input, #settings-form button').forEach(function (n) {
      if (n.id === 'settings-apply' || n.id === 'settings-reset') return;
      n.disabled = on;
    });
    if (!on) onFormInput(); else { $('#settings-apply').disabled = true; $('#settings-reset').disabled = true; }
  }

  // ---------------------------------------------------------------- clients

  function renderClients() {
    var body = $('#clients-body');
    clear(body);

    if (!state.clients.length) {
      body.appendChild(el('tr', null, [el('td', { colspan: '4', class: 'empty', text: 'Клиентов нет. Создайте первого — он получит токен и бандл со всеми лучами.' })]));
      return;
    }

    state.clients.forEach(function (c) {
      var name = String(pick(c, ['name', 'client', 'id']) || '');
      var created = pick(c, ['created_at', 'created', 'ctime']);
      var lastBundle = pick(c, ['last_bundle_at', 'bundle_at', 'last_seen', 'last_seen_at']);

      var dl = el('button', { type: 'button', class: 'icon-btn', title: 'Скачать бандл', 'aria-label': 'Скачать бандл клиента ' + name }, [icon('i-download')]);
      dl.addEventListener('click', function () { downloadBundle(name, dl); });

      var rm = el('button', { type: 'button', class: 'icon-btn icon-btn--danger', title: 'Удалить клиента', 'aria-label': 'Удалить клиента ' + name }, [icon('i-trash')]);
      rm.addEventListener('click', function () { askDelete(name); });

      body.appendChild(el('tr', null, [
        el('td', null, [el('span', { class: 'spoke-name', text: name })]),
        el('td', { class: 'num dim', text: fmtDate(created) }),
        el('td', { class: 'num dim', text: fmtDate(lastBundle) }),
        el('td', null, [el('div', { class: 'row-actions' }, [dl, rm])])
      ]));
    });
  }

  function createClient(ev) {
    ev.preventDefault();
    var input = $('#client-name');
    var name = input.value.trim();
    if (!name) return;
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,30}$/.test(name)) {
      toast('critical', 'Имя: латиница, цифры, точка, дефис, подчёркивание; 1–31 символ');
      return;
    }
    var btn = $('#client-create');
    btn.disabled = true;
    api('api/clients', { method: 'POST', body: { name: name } })
      .then(function (payload) {
        input.value = '';
        var token = pick(payload, ['token']) || (payload && payload.client ? pick(payload.client, ['token']) : null);
        if (token) showToken(name, String(token));
        else toast('warning', 'Клиент создан, но сервер не вернул токен. Выдать его повторно нельзя — пересоздайте клиента.');
        return refreshAll({ quiet: true });
      })
      .catch(function (e) {
        if (e.status !== 401 && e.status !== 403) toast('critical', 'Не создан: ' + e.message);
      })
      .then(function () { btn.disabled = false; });
  }

  function askDelete(name) {
    $('#confirm-text').textContent = 'Клиент «' + name + '» и его ключи на всех лучах будут удалены, токен перестанет работать. Действие необратимо.';
    var dlg = $('#confirm-dialog');
    state.confirmResolve = function (ok) {
      if (!ok) return;
      api('api/clients/' + encodeURIComponent(name), { method: 'DELETE' })
        .then(function () {
          toast('good', 'Клиент «' + name + '» удалён');
          return refreshAll({ quiet: true });
        })
        .catch(function (e) {
          if (e.status !== 401 && e.status !== 403) toast('critical', 'Не удалён: ' + e.message);
        });
    };
    dlg.showModal();
  }

  function downloadBundle(name, btn) {
    if (btn) btn.disabled = true;
    rawFetch('api/clients/' + encodeURIComponent(name) + '/bundle')
      .then(function (res) {
        if (res.status === 401 || res.status === 403) { onAuthLost(); throw ApiError(res.status, 'Ключ не принят'); }
        if (!res.ok) {
          return res.text().catch(function () { return null; }).then(function (t) {
            throw ApiError(res.status, errorMessage(res, t));
          });
        }
        return res.blob();
      })
      .then(function (blob) {
        // bundle carries private keys: straight to a file, never through the console
        var url = URL.createObjectURL(blob);
        var a = el('a', { href: url, download: 'obfmesh-' + name + '.json' });
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 20000);
        toast('good', 'Бандл клиента «' + name + '» скачан');
      })
      .catch(function (e) {
        if (e.status !== 401 && e.status !== 403) toast('critical', 'Бандл не скачался: ' + e.message);
      })
      .then(function () { if (btn) btn.disabled = false; });
  }

  function shellQuote(s) { return "'" + String(s).split("'").join("'\\''") + "'"; }

  function showToken(name, token) {
    state.tokenClient = name;
    $('#token-client').textContent = name;
    $('#token-value').textContent = token;
    var url = location.origin + basePath().replace(/\/$/, '');
    $('#token-uci').textContent =
      'uci set obfmesh.main.server_url=' + shellQuote(url) + '; ' +
      'uci set obfmesh.main.token=' + shellQuote(token) + '; ' +
      'uci set obfmesh.main.enabled=1; uci commit obfmesh; /etc/init.d/obfmesh restart';
    $('#token-dialog').showModal();
  }

  function clearTokenDialog() {
    // secret must not linger in the DOM after the dialog is closed
    $('#token-value').textContent = DASH;
    $('#token-uci').textContent = DASH;
    state.tokenClient = null;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }

  // plain-http origins have no async clipboard API
  function legacyCopy(text) {
    var ta = el('textarea', { readonly: '' });
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  }

  function copyFrom(sel, what) {
    var value = $(sel).textContent;
    if (!value || value === DASH) return;
    copyText(value).then(function (ok) {
      toast(ok ? 'good' : 'warning', ok ? what + ' скопирован' : 'Скопировать не вышло — выделите текст вручную');
    });
  }

  // ---------------------------------------------------------------- data flow

  function markStale(on) {
    $('#section-status').classList.toggle('is-stale', on);
  }

  function refreshStatus(opts) {
    opts = opts || {};
    clearTimeout(state.staleTimer);
    state.staleTimer = setTimeout(function () { markStale(true); }, 500);
    return api('api/status')
      .then(function (data) {
        state.status = data;
        state.lastUpdate = new Date();
        updateRates(spokeList(data));
        renderStatus();
        state.serverOk = true;
        updateConn();
      })
      .catch(function (e) {
        if (e.status === 401 || e.status === 403) throw e;
        state.serverOk = false;
        updateConn();
        if (!opts.quiet) toast('critical', 'Статус не получен: ' + e.message);
        throw e;
      })
      .then(function () { clearTimeout(state.staleTimer); markStale(false); },
            function (e) { clearTimeout(state.staleTimer); markStale(false); throw e; });
  }

  function sameValues(a, b) {
    return a.spokes === b.spokes &&
           a.external_host === b.external_host && a.mtu === b.mtu;
  }

  function refreshSettings() {
    return api('api/settings').then(function (data) {
      var before = serverValues();
      var had = state.settings !== null;
      state.settings = data;
      // conflict only when the server itself moved, not merely because the form has edits
      var serverChanged = !had || !sameValues(before, serverValues());

      if (!state.formTouched || !isDirty()) {
        fillForm(data);
        clearFormConflict();
      } else if (serverChanged) {
        showFormConflict();
      }
      renderSettingsSide();
      renderDiff();
    });
  }

  function clearFormConflict() {
    var n = $('#form-conflict');
    if (n) n.remove();
  }

  function showFormConflict() {
    if ($('#form-conflict')) return;
    var n = notice('warning', 'i-warn',
      'Настройки на сервере изменились, пока вы правили форму. Ваши правки не отправлены.',
      'Взять с сервера', function () {
        fillForm(state.settings);
        clearFormConflict();
      });
    n.id = 'form-conflict';
    $('#settings-notices').appendChild(n);
  }

  function refreshClients() {
    return api('api/clients').then(function (data) {
      var list = Array.isArray(data) ? data : (data && Array.isArray(data.clients) ? data.clients : []);
      state.clients = list;
      state.clientsLoaded = true;
      renderClients();
    });
  }

  function refreshAll(opts) {
    opts = opts || {};
    var reported = { done: false };   // one failing server should not raise three toasts
    return Promise.all([
      refreshSettings().catch(reportOr(opts, reported, 'Настройки не получены')),
      refreshClients().catch(reportOr(opts, reported, 'Список клиентов не получен'))
    ]).then(function () {
      return refreshStatus({ quiet: true }).catch(reportOr(opts, reported, 'Статус не получен'));
    });
  }

  function reportOr(opts, reported, prefix) {
    return function (e) {
      if (e.status === 401 || e.status === 403) return;
      state.serverOk = false;
      updateConn();
      if (opts.quiet || reported.done) return;
      reported.done = true;
      toast('critical', prefix + ': ' + e.message);
    };
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(function () {
      if (document.hidden) return;
      refreshStatus({ quiet: true }).catch(function () { /* indicator already shows it */ });
    }, STATUS_POLL_MS);
  }

  function stopPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  // ---------------------------------------------------------------- SSE

  // one indicator for two independent channels: the SSE stream and the counter poll
  function updateConn() {
    var kind, text;
    if (state.serverOk === null) { kind = 'idle'; text = 'подключение…'; }
    else if (!state.serverOk) { kind = 'offline'; text = 'нет связи с сервером'; }
    else if (state.sseUp) { kind = 'online'; text = 'живое обновление'; }
    else { kind = 'pending'; text = 'обновление опросом'; }

    if (!state.sseUp && state.retryAt && state.serverOk !== null) {
      var left = Math.max(0, Math.ceil((state.retryAt - Date.now()) / 1000));
      text += left ? ', поток через ' + left + ' с' : ', поток восстанавливается';
    }

    state.conn = kind;
    var box = $('#conn');
    box.className = 'conn' + (kind === 'idle' ? '' : ' conn--' + kind);
    setText('#conn-text', text);
    box.setAttribute('title', state.lastUpdate ? 'Последнее обновление: ' + tf.format(state.lastUpdate) : 'Данных ещё не было');
  }

  function startSse() {
    if (state.sseRunning) return;
    state.sseRunning = true;
    state.sseAttempt = 0;
    sseConnect();
  }

  function stopSse() {
    state.sseRunning = false;
    state.sseUp = false;
    if (state.sseCtrl) { try { state.sseCtrl.abort(); } catch (e) { /* already gone */ } }
    state.sseCtrl = null;
  }

  function sseConnect() {
    if (!state.sseRunning) return;
    var ctrl = new AbortController();
    state.sseCtrl = ctrl;

    // EventSource cannot carry X-API-Key, so the stream is read from fetch()
    rawFetch('api/events', { headers: { Accept: 'text/event-stream' }, signal: ctrl.signal })
      .then(function (res) {
        if (res.status === 401 || res.status === 403) { onAuthLost(); return null; }
        if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
        state.sseOpenedAt = Date.now();
        state.sseUp = true;
        state.serverOk = true;
        state.retryAt = 0;
        updateConn();
        return pump(res.body.getReader());
      })
      .then(function () { if (state.sseRunning) scheduleReconnect(); })
      .catch(function () { if (state.sseRunning) scheduleReconnect(); });
  }

  function pump(reader) {
    var decoder = new TextDecoder();
    var buffer = '';
    function step() {
      return reader.read().then(function (r) {
        if (r.done) return;
        buffer += decoder.decode(r.value, { stream: true });
        var m;
        while ((m = /\r\n\r\n|\n\n|\r\r/.exec(buffer)) !== null) {
          var frame = buffer.slice(0, m.index);
          buffer = buffer.slice(m.index + m[0].length);
          handleFrame(frame);
        }
        return step();
      });
    }
    return step();
  }

  function handleFrame(frame) {
    var event = 'message', data = [];
    frame.split(/\r\n|\n|\r/).forEach(function (line) {
      if (!line || line.charAt(0) === ':') return;             // comment / heartbeat
      var i = line.indexOf(':');
      var field = i === -1 ? line : line.slice(0, i);
      var value = i === -1 ? '' : line.slice(i + 1).replace(/^ /, '');
      if (field === 'event') event = value;
      else if (field === 'data') data.push(value);
    });
    state.sseUp = true;
    state.serverOk = true;
    updateConn();
    // per the SSE spec a frame with an empty data buffer dispatches nothing:
    // that covers ": ping" comments and any other keepalive
    if (!data.length) return;
    if (event === 'ping' || event === 'keepalive' || event === 'heartbeat') return;
    scheduleRefresh();
  }

  function scheduleRefresh() {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(function () { refreshAll({ quiet: true }); }, 250);
  }

  function scheduleReconnect() {
    state.sseCtrl = null;
    // only a stream that actually held resets the backoff; a flapping endpoint keeps escalating
    if (state.sseOpenedAt && Date.now() - state.sseOpenedAt > 30000) state.sseAttempt = 0;
    state.sseOpenedAt = 0;
    var idx = Math.min(state.sseAttempt, SSE_BACKOFF_MS.length - 1);
    var base = SSE_BACKOFF_MS[idx];
    var delay = Math.round(base * (0.8 + Math.random() * 0.4));   // jitter, so N clients do not sync up
    state.sseAttempt++;
    state.retryAt = Date.now() + delay;
    state.sseUp = false;
    updateConn();
    setTimeout(function () {
      if (!state.sseRunning) return;
      updateConn();
      sseConnect();
    }, delay);
  }

  // ---------------------------------------------------------------- toasts

  function toast(kind, message) {
    var iconId = kind === 'good' ? 'i-check' : kind === 'warning' ? 'i-warn' : 'i-warn';
    var node = el('div', { class: 'toast toast--' + kind, role: 'status' }, [icon(iconId), el('span', { text: message })]);
    $('#toasts').appendChild(node);
    setTimeout(function () {
      node.style.transition = 'opacity .2s ease';
      node.style.opacity = '0';
      setTimeout(function () { node.remove(); }, 250);
    }, kind === 'critical' ? 8000 : 4000);
  }

  // ---------------------------------------------------------------- wiring

  function bind() {
    $('#gate-form').addEventListener('submit', function (ev) {
      ev.preventDefault();
      var key = $('#gate-key').value;
      if (!key) return;
      var btn = $('#gate-submit');
      btn.disabled = true;
      state.apiKey = key;
      api('api/status')
        .then(function () {
          storeKey(key, $('#gate-remember').checked);
          $('#gate-key').value = '';
          hideGate();
          start();
        })
        .catch(function (e) {
          state.apiKey = null;
          $('#gate').hidden = false;
          $('#app').hidden = true;
          var err = $('#gate-error');
          err.hidden = false;
          err.textContent = (e.status === 401 || e.status === 403) ? 'Ключ не подошёл.' : 'Сервер не ответил: ' + e.message;
        })
        .then(function () { btn.disabled = false; });
    });

    $('#btn-logout').addEventListener('click', function () {
      forgetKey();
      stopSse();
      stopPolling();
      state.status = null; state.settings = null; state.clients = [];
      state.samples.clear(); state.rates.clear();
      showGate('');
    });

    $('#btn-refresh').addEventListener('click', function () { refreshAll({}); });

    $('#spokes-range').addEventListener('input', function () { state.formTouched = true; onFormInput(); });
    $('#spokes-minus').addEventListener('click', function () { nudgeSpokes(-1); });
    $('#spokes-plus').addEventListener('click', function () { nudgeSpokes(1); });
    $('#set-host').addEventListener('input', function () { state.formTouched = true; renderDiff(); });
    $('#set-mtu').addEventListener('input', function () { state.formTouched = true; renderDiff(); });

    $('#settings-form').addEventListener('submit', applySettings);
    $('#settings-reset').addEventListener('click', function () {
      if (state.settings) fillForm(state.settings);
      clear($('#settings-notices'));
    });

    $('#client-form').addEventListener('submit', createClient);

    $('#token-copy').addEventListener('click', function () { copyFrom('#token-value', 'Токен'); });
    $('#uci-copy').addEventListener('click', function () { copyFrom('#token-uci', 'Команда'); });
    $('#token-bundle').addEventListener('click', function () {
      if (state.tokenClient) downloadBundle(state.tokenClient, null);
    });
    $('#token-close').addEventListener('click', function () { $('#token-dialog').close(); });
    $('#token-dialog').addEventListener('close', clearTokenDialog);
    $('#token-dialog').addEventListener('cancel', function (ev) { ev.preventDefault(); });   // no accidental Esc on a one-time secret

    $('#confirm-cancel').addEventListener('click', function () { $('#confirm-dialog').close(); });
    $('#confirm-ok').addEventListener('click', function () {
      var fn = state.confirmResolve;
      state.confirmResolve = null;
      $('#confirm-dialog').close();
      if (fn) fn(true);
    });
    $('#confirm-dialog').addEventListener('close', function () { state.confirmResolve = null; });

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && !$('#app').hidden) refreshStatus({ quiet: true }).catch(function () {});
    });

    window.addEventListener('online', function () {
      if ($('#app').hidden) return;
      state.sseAttempt = 0;
      refreshAll({ quiet: true });
    });

    state.tickTimer = setInterval(updateConn, 1000);
  }

  function nudgeSpokes(delta) {
    var range = $('#spokes-range');
    var next = Math.min(MAX_SPOKES, Math.max(1, Number(range.value) + delta));
    if (next === Number(range.value)) return;
    range.value = String(next);
    state.formTouched = true;
    onFormInput();
  }

  function start(quiet) {
    hideGate();
    onFormInput();
    updateConn();
    refreshAll({ quiet: !!quiet }).then(function () {
      startSse();
      startPolling();
    });
  }

  function boot() {
    bind();
    state.apiKey = loadStoredKey();
    var hadKey = !!state.apiKey;
    api('api/status')
      .then(function () { start(false); })
      .catch(function (e) {
        if (e.status === 401 || e.status === 403) {
          forgetKey();
          showGate(hadKey ? 'Сохранённый ключ больше не подходит.' : '');
          return;
        }
        // server unreachable: open the app anyway, the indicator and retries do the rest
        state.serverOk = false;
        updateConn();
        toast('critical', 'Сервер не ответил: ' + e.message);
        start(true);
      });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

})();
