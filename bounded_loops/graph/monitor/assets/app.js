// app.js — bounded-loops monitoring console. Main entry point.
// React 18.3.1 UMD (window.React, window.ReactDOM) + htm 3.1.1.
// No build step. No CDN. No network at runtime beyond the local server.

import htm from './vendor/htm.module.js';
import { WorkspaceRail, MonitorCol, ConfigPanel } from './columns.js';
import { KPalette } from './palette.js';

const html = htm.bind(React.createElement);
const { useState, useEffect, useRef, useCallback } = React;

// ── Product display name — change THIS ONE CONSTANT when name is decided ──────
const APP_DISPLAY_NAME = 'bounded·loops';   // interpunct u+00B7

// ── Token — server injects window.__BL_TOKEN__; URL param is the fallback ────
// NEVER stored in React state. NEVER logged. NEVER put in a link.
const _TOKEN = window.__BL_TOKEN__ ?? new URLSearchParams(location.search).get('token') ?? '';

// ── Terminal states ───────────────────────────────────────────────────────────
const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'HALTED', 'CANCELLED', 'EXPIRED']);

// ── API helper ────────────────────────────────────────────────────────────────
async function api(route, extra = {}) {
  try {
    const r = await fetch('/api/' + route, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: _TOKEN, ...extra }),
    });
    return r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// ── Palette icon helper ───────────────────────────────────────────────────────
const mkIco = paths => html`<svg width="14" height="14" viewBox="0 0 16 16" fill="none">${paths}</svg>`;
const ICO = {
  lint:    mkIco(html`<circle cx="8" cy="8" r="5.5" stroke="currentColor" strokeWidth="1.3"/><line x1="8" y1="5.5" x2="8" y2="8.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/><circle cx="8" cy="10.5" r=".8" fill="currentColor"/>`),
  plan:    mkIco(html`<rect x="2" y="3" width="12" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.3"/><line x1="5" y1="6.5" x2="11" y2="6.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/><line x1="5" y1="9.5" x2="9" y2="9.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>`),
  run:     mkIco(html`<path d="M5 3.5l7 4.5-7 4.5V3.5z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>`),
  save:    mkIco(html`<rect x="2.5" y="1.5" width="11" height="13" rx="1" stroke="currentColor" strokeWidth="1.3"/><rect x="5" y="1.5" width="6" height="4.5" stroke="currentColor" strokeWidth="1.3"/><rect x="4.5" y="8.5" width="7" height="4" rx=".5" stroke="currentColor" strokeWidth="1.3"/>`),
  approve: mkIco(html`<path d="M3 8l4 4 6-6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>`),
  handoff: mkIco(html`<path d="M9 2h5v5M14 2L7 9" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/><rect x="1.5" y="8" width="6" height="6" rx="1" stroke="currentColor" strokeWidth="1.3"/>`),
};

// ── App root ──────────────────────────────────────────────────────────────────
function App() {
  // Workspace / server meta
  const [ws,       setWs]       = useState(null);
  const [caps,     setCaps]     = useState(null);
  const [forms,    setForms]    = useState(null);
  const [runs,     setRuns]     = useState([]);
  const [graphs,   setGraphs]   = useState([]);
  const [agents,   setAgents]   = useState([]);
  const [lastPoll, setLastPoll] = useState(null);

  // Graph editor state
  const [selectedRun,    setSelectedRun]    = useState(null);
  const [projection,     setProjection]     = useState(null);
  const [planNodes,      setPlanNodes]      = useState([]);
  const [graphEdges,     setGraphEdges]     = useState([]);
  const [manifest,       setManifest]       = useState('');
  const [graphName,      setGraphName]      = useState('');
  const [selectedNodeId, setSelectedNodeId] = useState(null);

  // Execute flow
  const [executePreview, setExecutePreview] = useState(null);
  const [executing,      setExecuting]      = useState(false);
  const [executeError,   setExecuteError]   = useState('');

  // Handoff
  const [handoffResult,  setHandoffResult]  = useState(null);
  const [handoffLoading, setHandoffLoading] = useState(false);
  const [handoffError,   setHandoffError]   = useState('');

  // Palette
  const [paletteOpen, setPaletteOpen] = useState(false);

  // Loading flags (keyed: lint | plan | poll | run)
  const [loading, setLoading] = useState({});
  const setLoad = useCallback((k, v) => setLoading(p => ({ ...p, [k]: v })), []);

  const esRef = useRef(null);

  // ── Boot ────────────────────────────────────────────────────────────────────
  useEffect(() => {
    (async () => {
      const [w, c, f, r, a] = await Promise.all([
        api('workspace'), api('capabilities'), api('forms'), api('runs'), api('agents'),
      ]);
      if (w.ok) { setWs(w); setGraphs(w.graphs || []); }
      if (c.ok) setCaps(c.capabilities);
      if (f.ok) setForms(f.forms);
      if (r.ok) setRuns(r.runs || []);
      // agents is informational only — never gates any feature
      if (a.ok) setAgents(a.admitted || []);
      setLastPoll(Date.now());
    })();
    const id = setInterval(poll, 10000);
    return () => clearInterval(id);
  }, []);

  // ── Polling: runs + workspace + agents every 10s ─────────────────────────────
  const poll = useCallback(async () => {
    setLoad('poll', true);
    const [r, w, a] = await Promise.all([api('runs'), api('workspace'), api('agents')]);
    if (r.ok) setRuns(r.runs || []);
    if (w.ok) setGraphs(w.graphs || []);
    if (a.ok) setAgents(a.admitted || []);
    setLastPoll(Date.now());
    setLoad('poll', false);
  }, []);

  // ── SSE: live run projection ─────────────────────────────────────────────────
  useEffect(() => {
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
    if (!selectedRun) { setProjection(null); return; }

    setLoad('run', true);
    api('run', { run: selectedRun }).then(r => {
      if (r.ok && r.projection) setProjection(r.projection);
      setLoad('run', false);
    });

    const qs = 'token=' + encodeURIComponent(_TOKEN) + '&run=' + encodeURIComponent(selectedRun);
    const es = new EventSource('/events?' + qs);
    esRef.current = es;

    es.onmessage = ev => {
      try {
        const snap = JSON.parse(ev.data);
        setProjection(snap);
        if (snap.run_state && TERMINAL.has(snap.run_state)) {
          es.close(); esRef.current = null;
        }
      } catch {}
    };
    es.onerror = () => { es.close(); esRef.current = null; };
    return () => { es.close(); esRef.current = null; };
  }, [selectedRun]);

  // ── Global keyboard shortcuts ────────────────────────────────────────────────
  useEffect(() => {
    function onKey(e) {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key === 'k') { e.preventDefault(); setPaletteOpen(p => !p); return; }
      if (e.key === 'Escape')   { setPaletteOpen(false); setExecutePreview(null); return; }
      if (mod && e.key === 'i') { e.preventDefault(); onLint(); return; }
      if (mod && e.key === 'p') { e.preventDefault(); onPlan(); return; }
      if (mod && e.key === 's') { e.preventDefault(); onSave(); return; }
      if (mod && e.key === 'r') { e.preventDefault(); onExecutePreview(); }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [manifest, graphName]);

  // ── Actions ──────────────────────────────────────────────────────────────────
  async function onLint() {
    if (!manifest) return;
    setLoad('lint', true);
    const r = await api('lint', { manifest });
    setLoad('lint', false);
    if (!r.ok) console.error('[lint]', r.refusal?.message || r.error);
  }

  async function onPlan() {
    if (!manifest) return;
    setLoad('plan', true);
    const r = await api('plan', { manifest });
    setLoad('plan', false);
    if (r.ok) { setPlanNodes(r.nodes || []); setGraphEdges(r.edges || []); }
  }

  async function onSave() {
    const name = graphName.trim();
    if (!name || !manifest) return;
    const r = await api('graph.save', { name, manifest });
    if (r.ok) setGraphs(g => [...new Set([...g, name])].sort());
  }

  async function onLoadGraph(name) {
    const r = await api('graph.read', { name });
    if (!r.ok) return;
    setManifest(r.manifest || '');
    setGraphName(name);
    setSelectedNodeId(null);   // clear stale node evidence from previous graph
    setHandoffResult(null);
    setHandoffError('');
    const p = await api('plan', { manifest: r.manifest });
    if (p.ok) { setPlanNodes(p.nodes || []); setGraphEdges(p.edges || []); }
  }

  async function onExecutePreview() {
    if (!manifest) return;
    setExecuteError('');
    setExecuting(true);
    const r = await api('execute', { manifest, name: graphName || undefined, confirm: false });
    setExecuting(false);
    if (r.ok) setExecutePreview(r);
    else setExecuteError(r.error || r.refusal?.message || 'Execute unavailable');
  }

  async function onExecuteConfirm() {
    setExecuting(true);
    const r = await api('execute', { manifest, name: graphName || undefined, confirm: true });
    setExecuting(false);
    setExecutePreview(null);
    if (r.ok && r.run) {
      setRuns(p => [...new Set([...p, r.run])].sort());
      setSelectedRun(r.run);
    }
  }

  async function onApprove(decision, confirm) {
    if (!selectedRun || !selectedNodeId) return null;
    const r = await api('approve', {
      run: selectedRun, node_id: selectedNodeId, decision, confirm,
    });
    if (!r.ok) return null;
    if (confirm && r.projection) setProjection(r.projection);
    return r;
  }

  async function onHandoff() {
    const name = graphName.trim();
    if (!name) return;
    setHandoffLoading(true);
    setHandoffError('');
    setHandoffResult(null);
    const r = await api('handoff', { name });
    setHandoffLoading(false);
    if (r.ok) setHandoffResult(r);
    else setHandoffError(r.error || r.refusal?.message || 'Handoff unavailable');
  }

  // ── ⌘K command list — operations only, no chat ───────────────────────────────
  const hasManifest = !!manifest;

  // Whether the run currently being WATCHED is live. Used for live-view affordances only.
  //
  // It deliberately does NOT gate Run any more. It used to, and that was wrong: the run in the
  // viewer and the manifest in the editor are different objects. Selecting any live run —
  // which is the normal thing to do in a monitor — disabled Run for a graph that had nothing
  // to do with it, so the button was permanently dead for exactly the users watching their
  // agent work. Starting a second run of a graph is legitimate anyway; the engine gives each
  // run its own id and directory. Accidental starts are prevented by the preview step, which
  // creates nothing until confirmed, not by grey-ing out the control.
  const isLiveRun   = projection?.run_state === 'RUNNING' || projection?.run_state === 'PENDING';
  const canExecute  = hasManifest && !executing;

  const paletteCommands = [
    { id: 'lint',    label: 'Lint graph',                shortcut: '⌘I', icon: ICO.lint,    disabled: !hasManifest,                         action: onLint },
    { id: 'plan',    label: 'Plan graph',                shortcut: '⌘P', icon: ICO.plan,    disabled: !hasManifest,                         action: onPlan },
    { id: 'run',     label: 'Execute graph',             shortcut: '⌘R', icon: ICO.run,     disabled: !canExecute,                          action: onExecutePreview },
    { id: 'save',    label: 'Save graph',                shortcut: '⌘S', icon: ICO.save,    disabled: !hasManifest || !graphName.trim(),      action: onSave },
    { id: 'approve', label: 'Approve selected node',     shortcut: 'A',  icon: ICO.approve, disabled: !selectedNodeId,                        action: () => onApprove('approved', false) },
    { id: 'handoff', label: 'Get agent handoff command', shortcut: '⌘H', icon: ICO.handoff, disabled: !graphName.trim(),                      action: onHandoff },
  ];

  // Split display name on interpunct for typographic accent
  const nameParts = APP_DISPLAY_NAME.split('·');

  return html`
    <div style=${{ display: 'flex', flexDirection: 'column', height: '100%' }}>

      <header className="hdr">
        <span className="hdr-name">
          ${nameParts[0]}<span className="hdr-name-accent">·</span>${nameParts[1] || ''}
        </span>
        <span className="hdr-sep">/</span>
        <span className="hdr-crumb" title=${ws?.project_root || ''}>
          ${ws?.project_root || ws?.root || '…'}
        </span>
        ${hasManifest ? html`
          <span className="hdr-sep">/</span>
          <input className="hdr-graph-input"
            placeholder="graph name" value=${graphName}
            aria-label="Graph name"
            onInput=${e => setGraphName(e.target.value)}/>` : null}
        <div className="hdr-spacer"/>
        <div className="hdr-actions">
          ${executeError ? html`
            <span style=${{ fontSize: 'var(--t-xs)', color: 'var(--danger)' }}>
              ${executeError}
            </span>` : null}
          ${loading.lint ? html`<span className="spinner"/>` : null}
          <button className="btn btn-ghost btn-sm" onClick=${onLint}
            disabled=${!hasManifest} title="Lint (⌘I)">Lint</button>
          <button className="btn btn-ghost btn-sm" onClick=${onPlan}
            disabled=${!hasManifest} title="Plan (⌘P)">Plan</button>
          <button className="btn btn-primary btn-sm" onClick=${onSave}
            disabled=${!hasManifest || !graphName.trim()} title="Save (⌘S)">
            <svg width="11" height="11" viewBox="0 0 14 14" fill="none">
              <rect x="1.5" y="1.5" width="11" height="11" rx="1" stroke="currentColor" strokeWidth="1.3"/>
              <rect x="4" y="1.5" width="6" height="4" stroke="currentColor" strokeWidth="1.3"/>
              <rect x="3.5" y="7" width="7" height="4" rx=".5" stroke="currentColor" strokeWidth="1.3"/>
            </svg>
            Save
          </button>
          <button className="btn btn-sm" onClick=${onExecutePreview}
            disabled=${!canExecute} title="Execute (⌘R)">
            ${executing ? html`<span className="spinner"/>` : html`
              <svg width="9" height="9" viewBox="0 0 10 10" fill="none">
                <path d="M2 1.5l6 3.5-6 3.5V1.5z" fill="currentColor"/>
              </svg>
              Run`}
          </button>
          <button className="btn btn-ghost btn-sm" title="Command palette (⌘K)"
            onClick=${() => setPaletteOpen(true)}>
            <span className="mono" style=${{ fontSize: '10px' }}>⌘K</span>
          </button>
        </div>
      </header>

      <div className="cols">
        <${WorkspaceRail}
          runs=${runs}
          selectedRun=${selectedRun} setSelectedRun=${setSelectedRun}
          selectedRunState=${projection?.run_state}
          graphs=${graphs} onLoadGraph=${onLoadGraph}
          agents=${agents} lastPoll=${lastPoll}
          onRefresh=${poll} loading=${loading}
        />
        <${MonitorCol}
          projection=${projection} planNodes=${planNodes} graphEdges=${graphEdges}
          selectedNodeId=${selectedNodeId} setSelectedNodeId=${setSelectedNodeId}
          selectedRun=${selectedRun} loading=${loading}
          executePreview=${executePreview}
          onExecuteConfirm=${onExecuteConfirm}
          onExecuteCancel=${() => setExecutePreview(null)}
        />
        <${ConfigPanel}
          selectedNodeId=${selectedNodeId}
          projection=${projection} planNodes=${planNodes}
          forms=${forms} caps=${caps}
          selectedRun=${selectedRun} onApprove=${onApprove}
          graphName=${graphName}
          onHandoff=${onHandoff}
          handoffResult=${handoffResult}
          handoffLoading=${handoffLoading}
          handoffError=${handoffError}
        />
      </div>

      ${paletteOpen ? html`
        <${KPalette}
          commands=${paletteCommands}
          onClose=${() => setPaletteOpen(false)}/>` : null}
    </div>`;
}

ReactDOM.createRoot(document.getElementById('root')).render(html`<${App}/>`);
