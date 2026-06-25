/*
 * LTC → LV1 web remote — vanilla JS SPA.
 *
 * Single source of truth: window.state, hydrated from GET /api/state on
 * load and kept fresh by the SSE stream at /api/events. Every render
 * reads from window.state; every action POSTs to the server, then waits
 * for the resulting event(s) to flow back through SSE.
 */

(() => {

  // ────────────────────────────────────────────────────────────────────
  // Tiny helpers
  // ────────────────────────────────────────────────────────────────────

  const $  = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const el = (tag, attrs = {}, children = []) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') e.className = v;
      else if (k === 'dataset') Object.assign(e.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
      else if (v === false || v == null) continue;
      else e.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (c == null) continue;
      e.append(c.nodeType ? c : document.createTextNode(c));
    }
    return e;
  };

  const fetchJSON = async (url, opts = {}) => {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    const body = opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body;
    const r = await fetch(url, Object.assign({}, opts, { headers, body }));
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (_) {}
      throw new Error(msg);
    }
    if (r.status === 204) return null;
    return r.json();
  };

  const post = (url, body) => fetchJSON(url, { method: 'POST', body });
  const patch = (url, body) => fetchJSON(url, { method: 'PATCH', body });
  const del   = (url)       => fetchJSON(url, { method: 'DELETE' });

  // ────────────────────────────────────────────────────────────────────
  // Global state
  // ────────────────────────────────────────────────────────────────────

  window.state = {
    running: false,
    signal: null,
    current_tc: null,
    fps: null,
    lv1: { connected: false, registered: false, host: null, port: null, last_error: null },
    lv1_current_scene: null,
    lv1_current_scene_name: null,
    scenes: [],
    cues: [],
    current_file: null,
    dirty: false,
    last_fire: null,
    discovery: { scanning: false, results: [] },
    settings: {},
    recent_files: [],
  };

  // UI-only state
  const ui = {
    activeTab: 'cues',
    expandedCueId: null,
    assignFromCueId: null,    // when set, next scene tap assigns rather than recalls
    audioDevices: [],
    selectedDeviceIdx: null,
    channels: [],
    longPressTimer: null,
  };

  // ────────────────────────────────────────────────────────────────────
  // SSE — keep state in sync with the desktop app
  // ────────────────────────────────────────────────────────────────────

  let sse = null;

  function connectSSE() {
    if (sse) try { sse.close(); } catch (_) {}
    sse = new EventSource('/api/events');

    sse.addEventListener('snapshot', (e) => {
      Object.assign(window.state, JSON.parse(e.data));
      renderAll();
      markConn(true);
    });

    sse.addEventListener('tc', (e) => {
      const p = JSON.parse(e.data);
      window.state.current_tc = p.tc;
      window.state.fps = p.fps;
      window.state.signal = p.signal;
      renderTc();
    });

    sse.addEventListener('running', (e) => {
      const p = JSON.parse(e.data);
      window.state.running = !!p.running;
      window.state.signal = p.signal;
      renderTransport();
      renderTc();
    });

    sse.addEventListener('lv1_state', (e) => {
      const p = JSON.parse(e.data);
      window.state.lv1 = p;
      renderLv1Status();
      renderTransport();
    });

    sse.addEventListener('lv1_catalog', (e) => {
      const p = JSON.parse(e.data);
      window.state.scenes = p.scenes || [];
      renderScenes();
      renderCueScenePicker();
    });

    sse.addEventListener('lv1_current', (e) => {
      const p = JSON.parse(e.data);
      window.state.lv1_current_scene = p.index;
      window.state.lv1_current_scene_name = p.name;
      renderCurrentScene();
      renderScenes();
    });

    sse.addEventListener('cues', (e) => {
      const p = JSON.parse(e.data);
      window.state.cues = p.cues || [];
      renderCues();
    });

    sse.addEventListener('cue_fired', (e) => {
      const p = JSON.parse(e.data);
      window.state.last_fire = p;
      renderLastFire();
    });

    sse.addEventListener('last_fire', (e) => {
      window.state.last_fire = JSON.parse(e.data);
      renderLastFire();
    });

    sse.addEventListener('dirty', (e) => {
      const p = JSON.parse(e.data);
      window.state.dirty = !!p.dirty;
      window.state.current_file = p.file;
      renderFileBadge();
    });

    sse.addEventListener('discovery', (e) => {
      window.state.discovery = JSON.parse(e.data);
      renderDiscovered();
    });

    sse.addEventListener('status', (e) => {
      const p = JSON.parse(e.data);
      setStatus(p.text, p.warn);
    });

    sse.addEventListener('settings', (e) => {
      window.state.settings = JSON.parse(e.data);
      renderSettings();
    });

    sse.addEventListener('recent', (e) => {
      const p = JSON.parse(e.data);
      window.state.recent_files = p.recent_files || [];
      renderRecent();
    });

    // Debounce offline indicator — EventSource fires onerror on every transient
    // glitch, including the gap between a normal disconnect and the auto-retry.
    // Showing the red dot for those would flicker constantly on a flaky link.
    let offlineTimer = null;
    sse.onerror = () => {
      if (offlineTimer) return;
      offlineTimer = setTimeout(() => { offlineTimer = null; markConn(false); }, 1500);
    };
    sse.onopen = () => {
      if (offlineTimer) { clearTimeout(offlineTimer); offlineTimer = null; }
      markConn(true);
    };
  }

  // ────────────────────────────────────────────────────────────────────
  // Render layer — each renderXxx() touches only its own DOM region.
  // ────────────────────────────────────────────────────────────────────

  function renderAll() {
    renderTc();
    renderTransport();
    renderLv1Status();
    renderCurrentScene();
    renderLastFire();
    renderCues();
    renderScenes();
    renderDiscovered();
    renderFileBadge();
    renderSettings();
    renderCueScenePicker();
    renderRecent();
    $('#version').textContent = window.state.version ? `v${window.state.version}` : 'v—';
  }

  function renderRecent() {
    const sel = $('#recent-select');
    if (!sel) return;
    const recent = window.state.recent_files || [];
    sel.innerHTML = '';
    sel.append(el('option', { value: '' }, recent.length ? 'Recent ▾' : 'Recent (empty)'));
    recent.forEach((r) => {
      const label = r.exists ? r.name : `${r.name} (missing)`;
      const opt = el('option', { value: r.path, title: r.path }, label);
      if (!r.exists) opt.disabled = true;
      sel.append(opt);
    });
    if (recent.length) {
      sel.append(el('option', { value: '__clear__' }, '— Clear recent —'));
    }
    sel.value = '';
  }

  function renderTc() {
    const s = window.state;
    const tc = s.current_tc || '00:00:00:00';
    const tcEl = $('#tc');
    tcEl.textContent = tc;
    const live = s.signal === 'LOCKED';
    tcEl.classList.toggle('live', live);

    const st = $('#ltc-status');
    st.className = 'ltc-status';
    if (!s.running) { st.textContent = '● Stopped'; }
    else if (s.signal === 'LOCKED')       { st.textContent = '● LTC OK'; st.classList.add('ok'); }
    else if (s.signal === 'AUDIO_NOT_LTC'){ st.textContent = '● Audio present but no LTC'; st.classList.add('warn'); }
    else if (s.signal === 'NO_SIGNAL')    { st.textContent = '● No signal'; st.classList.add('err'); }
    else                                  { st.textContent = '● Waiting for LTC signal…'; st.classList.add('warn'); }

    $('#fps').textContent = s.fps ? `${(+s.fps).toFixed(2).replace(/\.?0+$/, '')} fps` : '-- fps';
  }

  function renderTransport() {
    const run = $('#btn-run');
    run.textContent = window.state.running ? '■  STOP' : '▶  START';
    run.classList.toggle('go',   !window.state.running);
    run.classList.toggle('stop',  window.state.running);

    const lv1 = $('#btn-lv1');
    if (window.state.lv1.registered) {
      lv1.textContent = '● ONLINE';
      lv1.classList.add('go'); lv1.classList.remove('stop', 'warn');
    } else if (window.state.lv1.connected) {
      lv1.textContent = '● connecting…';
      lv1.classList.add('warn'); lv1.classList.remove('go', 'stop');
    } else {
      lv1.textContent = '● OFFLINE';
      lv1.classList.add('stop'); lv1.classList.remove('go', 'warn');
    }
  }

  function renderLv1Status() {
    const s = window.state.lv1 || {};
    const body = $('#lv1-status');
    body.classList.remove('ok', 'warn', 'err');
    if (s.registered) { body.textContent = `Connected — ${s.host}:${s.port}`; body.classList.add('ok'); }
    else if (s.connected) { body.textContent = `Handshaking… (${s.host}:${s.port})`; body.classList.add('warn'); }
    else { body.textContent = `Disconnected${s.last_error ? ' — ' + s.last_error : ''}`; if (s.last_error) body.classList.add('err'); }
  }

  function renderCurrentScene() {
    const body = $('#lv1-current');
    const idx = window.state.lv1_current_scene;
    if (idx == null) { body.textContent = '—'; body.classList.remove('ok'); }
    else {
      body.textContent = `[${idx}] ${window.state.lv1_current_scene_name || '(unknown)'}`;
      body.classList.add('ok');
    }
  }

  function renderLastFire() {
    const lf = window.state.last_fire;
    const body = $('#last-fire');
    if (!lf) { body.textContent = '—'; body.classList.remove('ok'); return; }
    body.textContent = `[${lf.scene_index}] ${lf.scene_name || ''}\ntarget ${lf.target_tc}  fired @ ${lf.fired_tc}`;
    body.classList.add('ok');
  }

  function renderFileBadge() {
    const name = window.state.current_file
      ? window.state.current_file.split(/[\\/]/).pop()
      : '(unsaved)';
    const fn = $('#file-name');
    fn.textContent = name;
    fn.classList.toggle('dirty', !!window.state.dirty);
  }

  function renderCues() {
    const list = $('#cues-list');
    list.innerHTML = '';
    const cues = window.state.cues || [];
    if (cues.length === 0) {
      list.append(el('div', { class: 'empty' }, 'No cues yet — tap “+ Add” above or use Quick Add + at a live TC.'));
      return;
    }
    cues.forEach((c, i) => list.append(renderCue(c, i + 1)));
  }

  function renderCue(c, order) {
    const classes = ['cue'];
    if (!c.enabled)              classes.push('disabled');
    if (c.fired)                 classes.push('fired');
    if (ui.expandedCueId === c.id) classes.push('expanded');

    const sceneText = (c.scene_name && c.scene_index != null)
      ? `[${c.scene_index}] ${c.scene_name}`
      : (c.scene_name || (c.scene_index != null ? `[${c.scene_index}]` : ''));

    const node = el('div', {
      class: classes.join(' '),
      dataset: { cueId: c.id },
      onclick: () => toggleExpanded(c.id),
    }, [
      el('div', { class: 'order' }, String(order)),
      el('div', { class: 'tc' }, c.timecode),
      el('div', { class: 'label' }, c.label || '(no label)'),
      el('div', { class: 'scene' }, sceneText || '(no scene)'),
      el('div', { class: 'badge-row' }, [
        el('span', { class: `badge status-${c.scene_status}` }, c.scene_status),
        el('span', { class: 'badge' }, c.enabled ? '●' : '○'),
      ]),
      el('div', { class: 'cue-actions', onclick: (e) => e.stopPropagation() }, [
        el('button', { class: 'btn go', onclick: () => fireCue(c.id) }, '▶ Fire'),
        el('button', { class: 'btn', onclick: () => editCue(c.id) }, 'Edit'),
        el('button', { class: 'btn', onclick: () => moveCue(c.id, 'up') }, '▲ Up'),
        el('button', { class: 'btn', onclick: () => moveCue(c.id, 'down') }, '▼ Down'),
        el('button', { class: 'btn', onclick: () => toggleEnabled(c.id) }, c.enabled ? 'Disable' : 'Enable'),
        el('button', { class: 'btn', onclick: () => startAssign(c.id) }, '↔ Assign scene'),
        el('button', { class: 'btn danger', onclick: () => removeCue(c.id) }, 'Remove'),
      ]),
    ]);

    if (ui.assignFromCueId != null) node.classList.add('drop-target');
    return node;
  }

  function renderScenes() {
    const list = $('#scenes-list');
    list.innerHTML = '';
    const scenes = window.state.scenes || [];
    if (scenes.length === 0) {
      list.append(el('div', { class: 'empty' }, 'Connect to an LV1 to see its scene catalog.'));
      return;
    }
    const cur = window.state.lv1_current_scene;
    scenes.forEach((sc) => {
      const classes = ['scene-row'];
      if (sc.index === cur) classes.push('current');
      if (ui.assignFromCueId != null) classes.push('assign-mode');

      const row = el('div', {
        class: classes.join(' '),
        // Single-click only acts when we're in assign mode (one tap to
        // assign). Otherwise it's a no-op so the operator can't accidentally
        // recall a scene by brushing the list with a finger. Recalling
        // requires a deliberate double-click.
        onclick:    () => onSceneClick(sc.index, false),
        ondblclick: () => onSceneClick(sc.index, true),
      }, [
        el('span', { class: 'idx mono' }, `[${sc.index}]`),
        el('span', { class: 'name' }, sc.name),
      ]);
      list.append(row);
    });
  }

  function renderDiscovered() {
    const sel = $('#discovered-select');
    const cur = sel.value;
    sel.innerHTML = '';
    const opts = [{ value: '', text: '(pick one)' }];
    (window.state.discovery.results || []).forEach((r) => {
      opts.push({
        value: `${r.ip}:${r.port}`,
        text: `${r.host || 'unknown'} — ${r.ip}:${r.port}`,
      });
    });
    opts.forEach((o) => sel.append(el('option', { value: o.value }, o.text)));
    if (window.state.discovery.scanning) {
      sel.innerHTML = '';
      sel.append(el('option', {}, 'Discovering LV1s on the LAN…'));
    } else if (cur) {
      sel.value = cur;
    }
  }

  function renderSettings() {
    const s = window.state.settings || {};
    if ('lv1_host' in s) $('#lv1-host').value = s.lv1_host || '';
    if ('lv1_port' in s) $('#lv1-port').value = s.lv1_port || '';
    if ('tolerance_frames' in s) $('#tolerance').value = s.tolerance_frames ?? 1;
    if ('dry_run' in s) $('#dry-run').checked = !!s.dry_run;
    if ('block_size' in s) $('#block-select').value = String(s.block_size);
    if ('sample_rate' in s) $('#sr-select').value = String(s.sample_rate);
  }

  function renderCueScenePicker() {
    const sel = $('#cue-scene-picker');
    sel.innerHTML = '';
    sel.append(el('option', { value: '' }, '(custom — type below)'));
    (window.state.scenes || []).forEach((sc) => {
      sel.append(el('option', { value: String(sc.index) }, `[${sc.index}] ${sc.name}`));
    });
  }

  // ────────────────────────────────────────────────────────────────────
  // Tabs
  // ────────────────────────────────────────────────────────────────────

  $$('.tab').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));

  function switchTab(name) {
    ui.activeTab = name;
    $$('.tab').forEach((x) => x.classList.toggle('active', x.dataset.tab === name));
    $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.id === `tab-${name}`));
    // Lazy-load the audio device list only once. The user can hit the ↺
    // button in the Settings tab to force a re-scan.
    if (name === 'settings' && !ui.audioDevicesLoaded) loadAudioDevices();
  }

  // ────────────────────────────────────────────────────────────────────
  // Cue interactions
  // ────────────────────────────────────────────────────────────────────

  function toggleExpanded(id) {
    ui.expandedCueId = ui.expandedCueId === id ? null : id;
    renderCues();
  }
  function fireCue(id)     { post(`/api/cues/${id}/fire`).catch(setError); }
  function moveCue(id, d)  { post(`/api/cues/${id}/move`, { direction: d }).catch(setError); }
  function toggleEnabled(id) { post(`/api/cues/${id}/toggle`).catch(setError); }
  function removeCue(id) {
    if (!confirm('Remove this cue?')) return;
    del(`/api/cues/${id}`).catch(setError);
  }

  function startAssign(cueId) {
    ui.assignFromCueId = cueId;
    switchTab('scenes');
    setStatus('Tap a scene to assign it. ESC or refresh to cancel.', false);
    renderScenes();
    renderCues();
  }

  function onSceneClick(idx, isDouble) {
    // In assign mode, a single tap is enough — the assign was already
    // explicitly armed via the cue's ↔ Assign button, so accidental taps
    // are not a concern here.
    if (ui.assignFromCueId != null) {
      const cueId = ui.assignFromCueId;
      ui.assignFromCueId = null;
      post(`/api/cues/${cueId}/assign`, { scene_index: idx })
        .then(() => {
          setStatus(`Assigned scene [${idx}]`);
          // Take the operator back to the cues tab so the new association
          // is visible without an extra tap.
          switchTab('cues');
        })
        .catch(setError);
      renderScenes();
      renderCues();
      return;
    }
    // Outside assign mode, require a double-click before sending a recall.
    // Single taps must be inert because the scene list is a fat tap target
    // and an accidental brush would otherwise jam the wrong snapshot on
    // the live mix.
    if (!isDouble) return;
    if (!window.state.lv1.registered) {
      setStatus('Connect to the LV1 first', true);
      return;
    }
    post('/api/lv1/recall', { index: idx }).catch(setError);
  }

  // ────────────────────────────────────────────────────────────────────
  // Cue dialog (Add / Edit)
  // ────────────────────────────────────────────────────────────────────

  const cueModal = {
    open(cue) {
      this.editingId = cue ? cue.id : null;
      $('#cue-modal-title').textContent = cue ? 'Edit cue' : 'Add cue';
      $('#cue-tc').value          = cue?.timecode || (window.state.current_tc || '00:00:00:00');
      $('#cue-label').value       = cue?.label || '';
      $('#cue-scene-name').value  = cue?.scene_name || '';
      $('#cue-scene-index').value = cue?.scene_index ?? '';
      $('#cue-enabled').checked   = cue ? !!cue.enabled : true;
      renderCueScenePicker();
      // If editing an existing cue whose scene IS in the catalog, preset picker.
      if (cue && cue.scene_name) {
        const match = (window.state.scenes || []).find((s) => s.name === cue.scene_name);
        $('#cue-scene-picker').value = match ? String(match.index) : '';
      } else {
        $('#cue-scene-picker').value = '';
      }
      $('#modal-backdrop').classList.remove('hidden');
    },
    close() { $('#modal-backdrop').classList.add('hidden'); },
    async save() {
      const payload = {
        timecode:    $('#cue-tc').value.trim() || '00:00:00:00',
        label:       $('#cue-label').value.trim(),
        scene_name:  $('#cue-scene-name').value.trim(),
        scene_index: $('#cue-scene-index').value === '' ? null : parseInt($('#cue-scene-index').value, 10),
        enabled:     $('#cue-enabled').checked,
      };
      try {
        if (this.editingId == null) await post('/api/cues', payload);
        else await patch(`/api/cues/${this.editingId}`, payload);
        this.close();
      } catch (exc) { setError(exc); }
    },
  };

  function editCue(id) {
    const cue = window.state.cues.find((c) => c.id === id);
    if (cue) cueModal.open(cue);
  }

  $('#cue-scene-picker').addEventListener('change', (e) => {
    const v = e.target.value;
    if (v === '') return;
    const sc = window.state.scenes.find((s) => String(s.index) === v);
    if (!sc) return;
    $('#cue-scene-name').value  = sc.name;
    $('#cue-scene-index').value = sc.index;
  });

  $('#cue-modal-cancel').addEventListener('click', () => cueModal.close());
  $('#cue-modal-save').addEventListener('click',   () => cueModal.save());
  $('#modal-backdrop').addEventListener('click',   (e) => {
    if (e.target === $('#modal-backdrop')) cueModal.close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      cueModal.close();
      ui.assignFromCueId = null;
      renderScenes(); renderCues();
    }
  });

  // ────────────────────────────────────────────────────────────────────
  // Toolbar
  // ────────────────────────────────────────────────────────────────────

  $('#btn-add').addEventListener('click',        () => cueModal.open(null));
  $('#btn-tap').addEventListener('click',        () => post('/api/cues/tap').catch(setError));
  $('#btn-reset').addEventListener('click',      () => post('/api/cues/reset').catch(setError));
  $('#btn-revalidate').addEventListener('click', async () => {
    try {
      const r = await post('/api/cues/revalidate');
      if (r.issues && r.issues.length) {
        alert(`${r.issues.length} cue(s) with issues:\n\n` +
          r.issues.map((v) => `  • #${v.cue_id} '${v.cue_label}' (${v.status})${v.suggestion ? ' ← did you mean ' + v.suggestion + '?' : ''}`).join('\n'));
      } else {
        setStatus('All cues resolved OK');
      }
    } catch (exc) { setError(exc); }
  });
  $('#btn-new').addEventListener('click', async () => {
    if (window.state.dirty && !confirm('Discard unsaved changes?')) return;
    post('/api/cues/new').catch(setError);
  });
  $('#btn-save').addEventListener('click', async () => {
    // Already-named file → just resave to the same path.
    if (window.state.current_file) {
      try { await post('/api/cues/save'); }
      catch (exc) { setError(exc); }
      return;
    }
    // New file → ask only for a name, the server drops it into the
    // default LTCtoLV1 projects folder on the host.
    let defDir = '';
    try {
      const r = await fetchJSON('/api/cues/default_dir');
      defDir = r.path || '';
    } catch (_) { /* fall through with no hint */ }
    const promptMsg = defDir
      ? `Save as (file name only — will be saved to:\n${defDir})`
      : 'Save as (file name only):';
    const name = prompt(promptMsg);
    if (!name || !name.trim()) return;
    try { await post('/api/cues/save', { name: name.trim() }); }
    catch (exc) { setError(exc); }
  });
  $('#btn-download').addEventListener('click', () => {
    window.location.href = '/api/cues/download';
  });
  $('#recent-select').addEventListener('change', async (e) => {
    const val = e.target.value;
    e.target.value = '';
    if (!val) return;
    if (val === '__clear__') {
      try { await fetchJSON('/api/cues/recent', { method: 'DELETE' }); }
      catch (exc) { setError(exc); }
      return;
    }
    if (window.state.dirty && !confirm('Discard unsaved changes?')) return;
    try { await post('/api/cues/open', { path: val }); }
    catch (exc) { setError(exc); }
  });

  $('#upload-input').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      await post('/api/cues/upload', JSON.parse(text));
      setStatus(`Loaded ${file.name}`);
    } catch (exc) { setError(exc); }
    e.target.value = '';
  });

  // ────────────────────────────────────────────────────────────────────
  // Transport / LV1
  // ────────────────────────────────────────────────────────────────────

  $('#btn-run').addEventListener('click', async () => {
    if (window.state.running) { post('/api/run/stop').catch(setError); return; }
    if (!ui.audioDevicesLoaded) await loadAudioDevices();
    if (ui.selectedDeviceIdx == null) {
      setStatus('Pick an audio device in Settings first', true);
      switchTab('settings');
      return;
    }
    const dev = ui.audioDevices.find((d) => d.index === ui.selectedDeviceIdx);
    if (!dev) { setStatus('Audio device gone — refresh', true); return; }
    const ch = parseInt($('#channel-select').value || '1', 10);
    const block = parseInt($('#block-select').value, 10);
    const srForce = $('#sr-force').checked;
    const sr = srForce ? parseInt($('#sr-select').value, 10) : (dev.default_samplerate || 48000);
    try {
      await post('/api/run/start', {
        device_index: dev.index,
        channel: ch,
        sample_rate: sr,
        block_size: block,
        device_label: dev.name,
      });
    } catch (exc) { setError(exc); }
  });

  $('#btn-lv1').addEventListener('click', async () => {
    if (window.state.lv1.registered || window.state.lv1.connected) {
      post('/api/lv1/disconnect').catch(setError);
      return;
    }
    const sel = $('#discovered-select').value;
    let host = $('#lv1-host').value.trim();
    let port = parseInt($('#lv1-port').value || '0', 10);
    if ((!host || !port) && sel) {
      const [h, p] = sel.split(':');
      host = h; port = parseInt(p, 10);
    }
    if (!host || !port) {
      switchTab('settings');
      setStatus('Pick or type a host:port, then Connect', true);
      return;
    }
    try { await post('/api/lv1/connect', { host, port }); }
    catch (exc) { setError(exc); }
  });

  $('#btn-discover').addEventListener('click', () => post('/api/lv1/discover').catch(setError));
  $('#btn-connect').addEventListener('click',  () => $('#btn-lv1').click());

  // Bring the host desktop UI to the foreground — works over fullscreen
  // apps on the host (e.g. the LV1 mix app) via the topmost-flicker trick
  // on Windows. Surfaces a friendly error if the desktop UI is not running.
  $('#btn-show-ui').addEventListener('click', async () => {
    try {
      await post('/api/window/show');
      setStatus('Opening LTCtoLV1 on the host…');
    } catch (exc) { setError(exc); }
  });

  // ────────────────────────────────────────────────────────────────────
  // Settings — audio + engine
  // ────────────────────────────────────────────────────────────────────

  async function loadAudioDevices() {
    try {
      const r = await fetchJSON('/api/audio/devices');
      ui.audioDevices = r.devices || [];
      ui.audioDevicesLoaded = true;
      const sel = $('#audio-select');
      sel.innerHTML = '';
      ui.audioDevices.forEach((d) => sel.append(el('option', { value: String(d.index) }, `${d.name}  (${d.hostapi})`)));
      // Restore previous selection by name prefix if possible
      const want = window.state.settings.audio_device;
      let matchedIdx = null;
      if (want) {
        const m = ui.audioDevices.find((d) => `${d.name}`.startsWith(want));
        if (m) matchedIdx = m.index;
      }
      if (matchedIdx == null && ui.audioDevices.length) matchedIdx = ui.audioDevices[0].index;
      if (matchedIdx != null) {
        sel.value = String(matchedIdx);
        ui.selectedDeviceIdx = matchedIdx;
        await loadChannels(matchedIdx);
      }
    } catch (exc) { setError(exc); }
  }

  async function loadChannels(idx) {
    try {
      const r = await fetchJSON(`/api/audio/channels?device_index=${encodeURIComponent(idx)}`);
      ui.channels = r.channels || [];
      const sel = $('#channel-select');
      sel.innerHTML = '';
      ui.channels.forEach((n, i) => sel.append(el('option', { value: String(i + 1) }, `${i + 1} — ${n}`)));
      // Restore previous channel selection
      const want = window.state.settings.audio_channel || 1;
      sel.value = String(Math.min(want, ui.channels.length) || 1);
    } catch (exc) { setError(exc); }
  }

  $('#audio-select').addEventListener('change', (e) => {
    ui.selectedDeviceIdx = parseInt(e.target.value, 10);
    loadChannels(ui.selectedDeviceIdx);
  });

  $('#btn-audio-refresh').addEventListener('click', async () => {
    try {
      await post('/api/audio/refresh');
      ui.audioDevicesLoaded = false;
      await loadAudioDevices();
    } catch (exc) { setError(exc); }
  });

  $('#sr-force').addEventListener('change', (e) => {
    $('#sr-select').disabled = !e.target.checked;
  });
  $('#sr-select').disabled = true;

  $('#tolerance').addEventListener('change', (e) => {
    const v = parseInt(e.target.value || '1', 10);
    patch('/api/settings', { tolerance_frames: v }).catch(setError);
  });
  $('#dry-run').addEventListener('change', (e) => {
    patch('/api/settings', { dry_run: !!e.target.checked }).catch(setError);
  });

  // ────────────────────────────────────────────────────────────────────
  // Status bar helpers
  // ────────────────────────────────────────────────────────────────────

  // Non-error status messages auto-fade after a few seconds so the bar doesn't
  // freeze on a stale message (e.g. "Recalled scene [3]") while the user is
  // doing something else. Errors stay until the next status update.
  let statusClearTimer = null;
  function setStatus(text, warn = false) {
    const s = $('#status-text');
    s.textContent = text || '';
    s.classList.toggle('warn', !!warn);
    if (statusClearTimer) { clearTimeout(statusClearTimer); statusClearTimer = null; }
    if (text && !warn) {
      statusClearTimer = setTimeout(() => {
        statusClearTimer = null;
        const cur = $('#status-text');
        if (cur && cur.textContent === text) cur.textContent = '';
      }, 5000);
    }
  }
  function setError(exc) {
    console.error(exc);
    setStatus(String(exc.message || exc), true);
  }
  function markConn(live) {
    $('#conn-indicator').classList.toggle('live', live);
    $('#conn-indicator').title = live ? 'Live event stream connected' : 'Disconnected — retrying';
  }

  // ────────────────────────────────────────────────────────────────────
  // Bootstrap
  // ────────────────────────────────────────────────────────────────────

  (async () => {
    try {
      window.state = await fetchJSON('/api/state');
      renderAll();
    } catch (exc) { setError(exc); }
    connectSSE();
  })();

})();
