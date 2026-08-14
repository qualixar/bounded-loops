// Jarvis — bounded-loops graph designer and runner.
// React 18 (UMD) + htm 3.1.1. No build step. No CDN. No network at runtime.
// Token: read once from URL, never written to DOM, sent only in POST bodies.
import htm from './vendor/htm.module.js';

const {useState, useEffect, useRef, useCallback, useMemo} = React;
const html = htm.bind(React.createElement);

// Token is read once at module load, never stored in state (which React can render to DOM).
const _TOKEN = new URLSearchParams(location.search).get('token') ?? '';

async function api(route, extra = {}) {
  try {
    const r = await fetch(`/api/${route}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: _TOKEN, ...extra}),
    });
    return r.json();
  } catch(e) { return {ok: false, error: String(e)}; }
}

const TERMINAL = new Set(['SUCCEEDED','FAILED','HALTED','CANCELLED','EXPIRED']);
const SUCCESS  = 'SUCCEEDED';
// Node states that map to CSS --s-* variables
const STATE_CSS = {PENDING:'pending',READY:'ready',WORKING:'working',CHECKING:'checking',
  PASSED:'passed',FAILED:'failed',BLOCKED:'blocked',HALTED:'halted',CANCELLED:'cancelled',
  SKIPPED:'skipped',SUCCEEDED:'succeeded',RUNNING:'running'};
const STATE_HEX = {PENDING:'#4a5566',READY:'#5b8bff',WORKING:'#e0a44a',CHECKING:'#a688ff',
  PASSED:'#34d9a0',FAILED:'#ff6b88',BLOCKED:'#ff4466',HALTED:'#ff8c42',CANCELLED:'#4a5566',
  SKIPPED:'#3b4455',SUCCEEDED:'#34d9a0',RUNNING:'#5b8bff'};

function stateHex(s) { return STATE_HEX[s] || '#4a5566'; }

// ── DAG layout ───────────────────────────────────────────────────────────────
// Accepts nodes (array of {node_id}), edges (array of [from,to] or {from_node,to_node}),
// levels (from projection, array of arrays of node_ids). levels wins when present.
function layoutDAG(nodes, edges, levels) {
  if (!nodes || !nodes.length) return {pos:{}, W:240, H:100, NW:160, NH:58};
  const NW=160, NH=58, CG=80, RG=14;
  const pos = {};

  if (levels && levels.length > 0) {
    levels.forEach((lvl, col) => {
      lvl.forEach((nid, row) => { pos[nid] = {x: col*(NW+CG), y: row*(NH+RG)}; });
    });
    // place any nodes not in levels (shouldn't happen but guard anyway)
    let extra = 0;
    nodes.forEach(n => {
      const id = n.node_id ?? n.id;
      if (!pos[id]) { pos[id] = {x: (levels.length)*(NW+CG), y: extra*(NH+RG)}; extra++; }
    });
  } else {
    // BFS topological layering
    const id = n => n.node_id ?? n.id;
    const adjIn = {}, adjOut = {};
    nodes.forEach(n => { adjIn[id(n)]=[]; adjOut[id(n)]=[]; });
    (edges||[]).forEach(e => {
      const f = Array.isArray(e) ? e[0] : (e.from_node ?? e.from);
      const t = Array.isArray(e) ? e[1] : (e.to_node ?? e.to);
      if (adjOut[f] !== undefined) adjOut[f].push(t);
      if (adjIn[t] !== undefined) adjIn[t].push(f);
    });
    const depth = {};
    const q = nodes.filter(n => !adjIn[id(n)].length);
    q.forEach(n => { depth[id(n)] = 0; });
    let i = 0;
    while (i < q.length) {
      const n = q[i++], fid = id(n), d = depth[fid]+1;
      (adjOut[fid]||[]).forEach(tid => {
        if (depth[tid] === undefined || depth[tid] < d) depth[tid] = d;
        if (!q.find(x => id(x) === tid)) { const tn=nodes.find(x=>id(x)===tid); if(tn) q.push(tn); }
      });
    }
    nodes.forEach(n => { if (depth[id(n)] === undefined) depth[id(n)] = 0; });
    const cols = {};
    nodes.forEach(n => { const d=depth[id(n)]; if(!cols[d]) cols[d]=[]; cols[d].push(id(n)); });
    Object.entries(cols).forEach(([c,ids]) => ids.forEach((nid,r) => {
      pos[nid] = {x: parseInt(c)*(NW+CG), y: r*(NH+RG)};
    }));
  }

  const xs = Object.values(pos).map(p=>p.x), ys = Object.values(pos).map(p=>p.y);
  const W = Math.max(240, Math.max(...xs)+NW+16), H = Math.max(100, Math.max(...ys)+NH+16);
  return {pos, W, H, NW, NH};
}

// ── Graph SVG ────────────────────────────────────────────────────────────────
function GraphSVG({nodes, edges, levels, nodeStates, selectedId, onSelect}) {
  const {pos, W, H, NW, NH} = useMemo(() => layoutDAG(nodes, edges, levels), [nodes, edges, levels]);
  if (!nodes || !nodes.length) return html`<div class="dim" style="padding:20px">No nodes to display.</div>`;

  const edgeList = (edges||[]).map(e => {
    const f = Array.isArray(e) ? e[0] : (e.from_node ?? e.from);
    const t = Array.isArray(e) ? e[1] : (e.to_node ?? e.to);
    const when = Array.isArray(e) ? null : e.when;
    return {f, t, when};
  });

  return html`
    <div class="dag-wrap">
      <svg class="dag-svg" viewBox="0 0 ${W} ${H}" width=${W} height=${H} xmlns="http://www.w3.org/2000/svg">
        <defs>
          <marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L0,6 L8,3 z" fill="#2a3248"/>
          </marker>
        </defs>
        ${edgeList.map(({f,t,when}) => {
          const fp=pos[f], tp=pos[t];
          if (!fp || !tp) return null;
          const x1=fp.x+NW, y1=fp.y+NH/2, x2=tp.x, y2=tp.y+NH/2, cx=(x1+x2)/2;
          const tState = (nodeStates&&nodeStates[t])||'PENDING';
          const color = tState === 'PASSED' || tState === 'SUCCEEDED' ? stateHex(tState) : '#2a3248';
          return html`<g key="${f}-${t}">
            <path d="M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2},${y2}"
              fill="none" stroke=${color} stroke-width="1.5" marker-end="url(#arr)"/>
            ${when && html`<text x=${cx} y=${Math.min(y1,y2)-4} text-anchor="middle"
              font-size="9" fill="#4a5566">${when}</text>`}
          </g>`;
        })}
        ${nodes.map(n => {
          const nid = n.node_id ?? n.id;
          const p = pos[nid];
          if (!p) return null;
          const state = nodeStates?.[nid] || n.state || 'PENDING';
          const color = stateHex(state);
          const isSelected = nid === selectedId;
          const isPaused = n.pauses_for_a_human || n.kind === 'approval';
          return html`<g key=${nid} onClick=${()=>onSelect(nid)} style="cursor:pointer">
            <rect x=${p.x} y=${p.y} width=${NW} height=${NH} rx="5"
              fill="#111318" stroke=${isSelected ? '#4f7bff' : '#1e2433'}
              stroke-width=${isSelected ? 2 : 1}/>
            <rect x=${p.x} y=${p.y} width="3" height=${NH} rx="1.5"
              fill=${color}/>
            <text x=${p.x+12} y=${p.y+20} font-size="12" font-weight="600"
              fill=${isSelected ? '#4f7bff' : '#d8dde6'} font-family="ui-monospace,monospace">${nid}</text>
            <text x=${p.x+12} y=${p.y+36} font-size="10"
              fill="#6b7688" font-family="system-ui,sans-serif">${n.kind||''}</text>
            ${isPaused && html`<circle cx=${p.x+NW-10} cy=${p.y+10} r="5" fill="#a688ff" opacity="0.9"/>`}
            ${state && state!=='PENDING' && html`<text x=${p.x+NW-6} y=${p.y+NH-7}
              font-size="9" text-anchor="end" fill=${color}
              font-family="ui-monospace,monospace">${state}</text>`}
          </g>`;
        })}
      </svg>
    </div>`;
}

// ── Form field widgets ────────────────────────────────────────────────────────
function ChoiceSelect({field, value, onChange}) {
  const hasUnavail = field.choices.some(c => !c.available);
  const unavailItems = field.choices.filter(c => !c.available);
  return html`
    <div>
      <select className="form-select" value=${value||''} onChange=${e=>onChange(e.target.value)}>
        <option value="">— pick —</option>
        ${field.choices.map(c => html`
          <option key=${c.value} value=${c.value} disabled=${!c.available}>
            ${c.value}${!c.available ? ' (unavailable)' : ''}
          </option>`)}
      </select>
      ${hasUnavail && html`<div class="unavail-list">
        ${unavailItems.map(c => html`
          <div class="unavail-item" key=${c.value}>
            <b>${c.value}</b>: ${c.reason || 'not available on any host'}
          </div>`)}
      </div>`}
    </div>`;
}

function FieldInput({field, value, onChange, depth=0}) {
  if (field.kind === 'enum' || field.choices?.length) {
    return html`<${ChoiceSelect} field=${field} value=${value} onChange=${onChange}/>`;
  }
  if (field.kind === 'boolean') {
    return html`<label style="display:flex;align-items:center;gap:6px;font-size:12px">
      <input type="checkbox" checked=${!!value} onChange=${e=>onChange(e.target.checked)}/>
      ${value ? 'true' : 'false'}
    </label>`;
  }
  if (field.kind === 'integer') {
    return html`<input className="form-input" type="number" value=${value??''}
      min=${field.minimum??undefined} max=${field.maximum??undefined}
      onChange=${e=>onChange(Number(e.target.value)||0)}/>`;
  }
  if (field.kind === 'object' && field.fields?.length) {
    return html`<div class=${depth ? 'form-nested' : ''}>
      <${FormFields} fields=${field.fields} values=${value||{}} onChange=${(k,v)=>onChange({...(value||{}), [k]:v})} depth=${depth+1}/>
    </div>`;
  }
  return html`<input className="form-input" type="text" value=${value??''}
    placeholder=${field.description||''}
    onChange=${e=>onChange(e.target.value)}/>`;
}

function FormFields({fields, values, onChange, depth=0}) {
  if (!fields?.length) return null;
  return html`<div class="form-fields">
    ${fields.map(f => html`
      <div key=${f.name}>
        <div class="form-field-label">
          ${f.name}${f.required ? html`<span class="req"> *</span>` : ''}
          ${f.description ? html`<span style="font-weight:400;color:var(--ink-lo)">${' — '}${f.description}</span>` : null}
        </div>
        <${FieldInput} field=${f} value=${values?.[f.name]} depth=${depth}
          onChange=${v => onChange(f.name, v)}/>
      </div>`)}
  </div>`;
}

// ── Left column — Command ─────────────────────────────────────────────────────
function LeftCol({transcript, cmdInput, setCmdInput, onSearch, searchRes, onCompose,
                  manifest, setManifest, composeData, onLint, onPlan, onSave, graphs, onLoadGraph, loading}) {
  const endRef = useRef(null);
  useEffect(() => { endRef.current?.scrollIntoView({behavior:'smooth'}); }, [transcript]);

  const [saveName, setSaveName] = useState('');
  const [showSaveRow, setShowSaveRow] = useState(false);

  return html`
    <div class="col-hdr"><span>COMMAND</span></div>
    <div class="col-body" style="display:flex;flex-direction:column;gap:0">
      <div class="cmd-wrap">
        <div class="cmd-input-row">
          <input class="cmd-input" type="text" value=${cmdInput}
            placeholder="Describe your task…"
            onInput=${e => setCmdInput(e.target.value)}
            onKeyDown=${e => { if(e.key==='Enter' && !e.shiftKey) { e.preventDefault(); onSearch(); } }}/>
          <button class="btn primary" onClick=${onSearch} disabled=${loading.search}>
            ${loading.search ? html`<span class="spinner"/>` : 'Search'}
          </button>
        </div>
      </div>
      <div class="transcript">
        ${transcript.map((m,i) => html`
          <div key=${i} class=${'tx-msg '+m.role}>
            <div class="tx-role">${m.role === 'user' ? 'you' : m.role === 'err' ? 'error' : m.role}</div>
            ${m.text}
          </div>`)}
        ${searchRes && html`
          <div class="tx-msg jarvis">
            <div class="tx-role">jarvis — search results</div>
            ${searchRes.ranking_caveat && html`<div class="caveat">${searchRes.ranking_caveat}</div>`}
            <div class="search-results">
              ${(searchRes.candidates||[]).map(c => html`
                <div key=${c.name} class="search-item">
                  <div class="search-name">${c.name}</div>
                  <div class="search-meta">gate: ${c.gate_kind} · score: ${c.score} · ${c.keyless ? 'keyless' : 'needs key'}</div>
                  <div class="search-desc">${c.description}</div>
                </div>`)}
            </div>
            ${searchRes.candidates?.length > 0 && html`
              <button class="btn primary" style="margin-top:6px" onClick=${onCompose}
                disabled=${loading.compose}>
                ${loading.compose ? html`<span class="spinner"/>` : 'Compose graph from these'}
              </button>`}
          </div>`}
        ${manifest && html`
          <div class="tx-msg jarvis">
            <div class="tx-role">manifest</div>
            ${composeData?.gaps?.length > 0 && html`<div class="gap-row">
              ${composeData.gaps.map((g,i) => html`
                <div class="gap-item" key=${i}>
                  <b>${g.node_id}</b>: ${g.gap} — ${g.next_step}
                </div>`)}
            </div>`}
            <textarea class="manifest-area" value=${manifest}
              onInput=${e => setManifest(e.target.value)} rows="10"/>
            <div class="btn-row">
              <button class="btn sm" onClick=${onLint} disabled=${loading.lint}>
                ${loading.lint ? html`<span class="spinner"/>` : 'Lint'}
              </button>
              <button class="btn sm" onClick=${onPlan} disabled=${loading.plan}>
                ${loading.plan ? html`<span class="spinner"/>` : 'Plan'}
              </button>
              <button class="btn sm primary" onClick=${() => setShowSaveRow(v=>!v)}>Save…</button>
            </div>
            ${showSaveRow && html`<div class="cmd-input-row" style="margin-top:6px">
              <input class="cmd-input" type="text" value=${saveName}
                placeholder="graph-name" onInput=${e=>setSaveName(e.target.value)}/>
              <button class="btn sm primary" onClick=${() => { onSave(saveName); setShowSaveRow(false); setSaveName(''); }}>
                Save
              </button>
            </div>`}
          </div>`}
        <div ref=${endRef}/>
      </div>
      ${graphs.length > 0 && html`
        <hr class="divider"/>
        <div style="font-size:10px;color:var(--ink-dim);font-weight:600;text-transform:uppercase;letter-spacing:.06em;padding:4px 0">Saved graphs</div>
        <div class="graph-list">
          ${graphs.map(g => html`
            <div key=${g} class="graph-item" onClick=${() => onLoadGraph(g)}>
              <span class="graph-item-name">${g}</span>
              <span class="tag">load</span>
            </div>`)}
        </div>`}
    </div>`;
}

// ── Centre column — Graph ─────────────────────────────────────────────────────
function CentreCol({projection, planNodes, composeEdges, selectedNodeId, setSelectedNodeId,
                    runs, selectedRun, setSelectedRun, loading}) {
  // Prefer live projection data; fall back to plan nodes + compose edges
  const nodes = projection ? projection.nodes : (planNodes || []);
  const edges = projection ? projection.edges : (composeEdges || []);
  const levels = projection ? projection.levels : null;
  const nodeStates = useMemo(() => {
    if (!projection?.nodes) return {};
    return Object.fromEntries(projection.nodes.map(n => [n.node_id, n.state]));
  }, [projection]);

  const runState = projection?.run_state;
  const bannerClass = runState === SUCCESS ? 'run-banner succeeded'
    : runState === 'FAILED' ? 'run-banner failed'
    : runState === 'RUNNING' || runState === 'PENDING' ? 'run-banner running'
    : runState ? 'run-banner halted' : '';

  return html`
    <div class="col-hdr">
      <span>GRAPH</span>
      <div class="col-hdr-actions">
        ${loading.run ? html`<span class="spinner"/>` : null}
        ${runs.length > 0 && html`
          <select value=${selectedRun||''} onChange=${e=>setSelectedRun(e.target.value||null)}
            style="background:var(--surf2);color:var(--ink);border:1px solid var(--line2);border-radius:4px;padding:3px 6px;font-size:11px">
            <option value="">— no active run —</option>
            ${runs.map(r => html`<option key=${r} value=${r}>${r}</option>`)}
          </select>`}
      </div>
    </div>
    <div class="col-graph-body">
      ${runState && html`
        <div class=${bannerClass}>
          <span class="state-dot" style="background:${stateHex(runState)};width:9px;height:9px;border-radius:50%;display:inline-block"/>
          <span class="state-label" style="color:${stateHex(runState)}">${runState}</span>
          ${runState !== SUCCESS && TERMINAL.has(runState)
            ? html`<span class="dim"> — this run did not succeed</span>`
            : runState === 'RUNNING' || runState === 'PENDING'
            ? html`<span class="dim"> — in flight</span>` : null}
          ${projection?.run_id && html`<span style="font-family:var(--mono);font-size:10px;color:var(--ink-lo);margin-left:auto">${projection.run_id.slice(0,12)}…</span>`}
        </div>`}
      ${nodes.length > 0
        ? html`<${GraphSVG} nodes=${nodes} edges=${edges} levels=${levels}
                  nodeStates=${nodeStates} selectedId=${selectedNodeId}
                  onSelect=${setSelectedNodeId}/>`
        : html`<div class="dim" style="padding:30px 0;text-align:center">
            No graph yet. Compose one from the command column.
          </div>`}
    </div>`;
}

// ── Right column — Evidence ───────────────────────────────────────────────────
function RightCol({selectedNodeId, projection, planNodes, forms, caps,
                   selectedRun, onApprove, loading}) {
  const [formVals, setFormVals] = useState({});
  const [approvePreview, setApprovePreview] = useState(null);
  const [approving, setApproving] = useState(false);

  // Find node data from projection or plan
  const projNode = projection?.nodes?.find(n => n.node_id === selectedNodeId);
  const planNode = planNodes?.find(n => (n.node_id ?? n.id) === selectedNodeId);
  const nodeKind = projNode?.kind ?? planNode?.kind ?? '';
  const nodeFields = forms?.nodes?.[nodeKind];
  const state = projNode?.state || (planNode ? 'PENDING' : null);

  const needsApproval = (nodeKind === 'approval') &&
    projNode && !TERMINAL.has(projNode.state) && projNode.state !== 'PASSED';

  async function handleApprovePreview(decision) {
    setApproving(true);
    const r = await onApprove(decision, false);
    setApprovePreview(r);
    setApproving(false);
  }
  async function handleApproveConfirm(decision) {
    setApproving(true);
    await onApprove(decision, true);
    setApprovePreview(null);
    setApproving(false);
  }

  if (!selectedNodeId) return html`
    <div class="col-hdr"><span>EVIDENCE</span></div>
    <div class="col-body"><div class="dim" style="padding:20px 0">Select a node in the graph to inspect it.</div></div>`;

  // Look up isolation enforcement from capabilities
  const isoKey = projNode?.isolation ?? planNode?.isolation;
  const capIso = caps?.isolation?.[isoKey];

  const spend = projNode ? {
    tokens: projNode.spend_tokens ?? 0,
    cost: projNode.spend_cost_microunits ?? 0,
    complete: projNode.spend_complete ?? true,
  } : null;
  const maxAttempts = planNode?.max_attempts ?? projNode?.attempt ?? 1;

  return html`
    <div class="col-hdr"><span>EVIDENCE — ${selectedNodeId}</span></div>
    <div class="col-body">
      <div class="evidence">
        <div class="ev-section">
          <div class="ev-title">Node</div>
          <div class="ev-row"><span class="ev-key">id</span><span class="ev-val">${selectedNodeId}</span></div>
          <div class="ev-row"><span class="ev-key">kind</span><span class="ev-val">${nodeKind||'—'}</span></div>
          ${state && html`<div class="ev-row"><span class="ev-key">state</span>
            <span class="ev-val" style="color:${stateHex(state)}">${state}</span></div>`}
          ${projNode?.attempt !== undefined && html`<div class="ev-row">
            <span class="ev-key">attempt</span><span class="ev-val">${projNode.attempt} / ${maxAttempts}</span></div>`}
          ${(projNode?.required_effects?.length || planNode?.effects?.length) && html`<div class="ev-row">
            <span class="ev-key">effects</span>
            <span class="ev-val">${(projNode?.required_effects || planNode?.effects || []).join(', ')||'none'}</span>
          </div>`}
        </div>

        ${isoKey && html`<div class="ev-section">
          <div class="ev-title">Isolation — what was actually enforced here</div>
          <div class="ev-row"><span class="ev-key">tier</span><span class="ev-val">${isoKey}</span></div>
          ${capIso ? html`
            <div class="ev-row"><span class="ev-key">deliverable here</span>
              <span class="ev-val ${capIso.deliverable_here ? 'ok' : 'err'}">
                ${capIso.deliverable_here ? 'yes' : 'no'}</span></div>
            ${capIso.reason_if_not && html`<div class="ev-row">
              <span class="ev-key">reason</span><span class="ev-val warn">${capIso.reason_if_not}</span></div>`}
            ${capIso.controls_enforced_here?.length
              ? html`<div class="ev-row"><span class="ev-key">controls</span>
                  <span class="ev-val">${capIso.controls_enforced_here.join(', ')}</span></div>`
              : html`<div class="ev-row"><span class="ev-key">controls</span>
                  <span class="ev-val warn">none enforced on this host</span></div>`}
          ` : html`<div class="ev-row"><span class="ev-key">detail</span>
            <span class="ev-val warn">capability data not loaded</span></div>`}
        </div>`}

        ${spend && html`<div class="ev-section">
          <div class="ev-title">Spend ${!spend.complete ? '(lower bound — some attempts reported nothing)' : ''}</div>
          <div class="ev-row"><span class="ev-key">tokens</span><span class="ev-val">${spend.tokens.toLocaleString()}</span></div>
          <div class="ev-row"><span class="ev-key">cost</span>
            <span class="ev-val">${(spend.cost/1000000).toFixed(6)} USD</span></div>
        </div>`}

        ${projNode?.artifact_digests?.length > 0 && html`<div class="ev-section">
          <div class="ev-title">Artifacts</div>
          ${projNode.artifact_digests.map((d,i) => html`
            <div class="ev-row" key=${i}><span class="ev-key">${i+1}</span>
              <span class="ev-val mono" style="font-size:10px">${d.slice(0,20)}…</span></div>`)}
        </div>`}

        ${needsApproval && selectedRun && html`<div class="ev-section">
          <div class="ev-title">Approval required</div>
          <div class="approve-panel">
            ${approvePreview
              ? html`<div>
                  <div class="approve-preview">${approvePreview.would}</div>
                  <div class="dim" style="font-size:10px;margin:4px 0">${approvePreview.hint}</div>
                  <div class="approve-btns">
                    <button class="btn ok" onClick=${()=>handleApproveConfirm('approved')} disabled=${approving}>
                      Confirm Approve
                    </button>
                    <button class="btn danger" onClick=${()=>handleApproveConfirm('rejected')} disabled=${approving}>
                      Confirm Reject
                    </button>
                    <button class="btn sm" onClick=${()=>setApprovePreview(null)}>Cancel</button>
                  </div>
                </div>`
              : html`<div class="approve-btns">
                  <button class="btn ok" onClick=${()=>handleApprovePreview('approved')} disabled=${approving}>
                    ${approving ? html`<span class="spinner"/>` : 'Approve'}
                  </button>
                  <button class="btn danger" onClick=${()=>handleApprovePreview('rejected')} disabled=${approving}>
                    Reject
                  </button>
                </div>`}
          </div>
        </div>`}

        ${nodeFields?.length > 0 && html`<div class="ev-section">
          <div class="ev-title">Node configuration (${nodeKind})</div>
          <${FormFields} fields=${nodeFields} values=${formVals}
            onChange=${(k,v) => setFormVals(prev => ({...prev, [k]:v}))}/>
        </div>`}
      </div>
    </div>`;
}

// ── App root ──────────────────────────────────────────────────────────────────
function App() {
  const [ws,     setWs]     = useState(null);
  const [caps,   setCaps]   = useState(null);
  const [forms,  setForms]  = useState(null);
  const [runs,   setRuns]   = useState([]);
  const [graphs, setGraphs] = useState([]);
  const [selectedRun, setSelectedRun] = useState(null);
  const [projection, setProjection] = useState(null);
  const [planNodes, setPlanNodes] = useState([]);
  const [composeEdges, setComposeEdges] = useState([]);
  const [composeData, setComposeData] = useState(null);
  const [manifest, setManifest] = useState('');
  const [cmdInput, setCmdInput] = useState('');
  const [transcript, setTranscript] = useState([]);
  const [searchRes, setSearchRes] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [loading, setLoading] = useState({});
  const esSrc = useRef(null);

  const setLoad = (k, v) => setLoading(p => ({...p, [k]: v}));
  const addMsg  = useCallback((role, text) => setTranscript(t => [...t, {role, text}]), []);

  // Initial boot: workspace, capabilities, forms, runs
  useEffect(() => {
    Promise.all([api('workspace'), api('capabilities'), api('forms'), api('runs')]).then(
      ([w, c, f, r]) => {
        if (w.ok) { setWs(w); setGraphs(w.graphs||[]); }
        if (c.ok) setCaps(c.capabilities);
        if (f.ok) setForms(f.forms);
        if (r.ok) setRuns(r.runs||[]);
      }
    );
  }, []);

  // SSE: connect when a run is selected, disconnect on change/unmount
  useEffect(() => {
    if (esSrc.current) { esSrc.current.close(); esSrc.current = null; }
    if (!selectedRun) { setProjection(null); return; }

    // Fetch initial state immediately
    api('run', {run: selectedRun}).then(r => { if (r.ok) setProjection(r.projection); });

    const url = `/events?token=${encodeURIComponent(_TOKEN)}&run=${encodeURIComponent(selectedRun)}`;
    const es = new EventSource(url);
    esSrc.current = es;

    es.onmessage = (ev) => {
      try {
        const snap = JSON.parse(ev.data);
        setProjection(snap);
        if (snap.run_state && TERMINAL.has(snap.run_state)) {
          // Terminal state — stream will close itself; nothing for us to do but read last snapshot
          es.close();
          esSrc.current = null;
        }
      } catch {}
    };
    es.onerror = () => {
      // Closed stream is normal completion, not an error — don't show an error banner
      es.close();
      esSrc.current = null;
    };
    return () => { es.close(); esSrc.current = null; };
  }, [selectedRun]);

  async function onSearch() {
    const q = cmdInput.trim();
    if (!q) return;
    addMsg('user', q);
    setLoad('search', true);
    setSearchRes(null);
    const r = await api('search', {task_description: q});
    setLoad('search', false);
    if (!r.ok) { addMsg('err', r.error || 'Search failed'); return; }
    setSearchRes(r);
    addMsg('jarvis', `Found ${r.candidates?.length||0} candidate${r.candidates?.length===1?'':'s'}. ${r.no_match_means||''}`);
  }

  async function onCompose() {
    if (!searchRes?.candidates?.length) return;
    setLoad('compose', true);
    const nodes = searchRes.candidates.map(c => ({id: c.name, kind: c.gate_kind || 'loop'}));
    const r = await api('compose', {graph_id: cmdInput.replace(/\s+/g,'_').toLowerCase()||'jarvis_graph', nodes});
    setLoad('compose', false);
    if (!r.ok) { addMsg('err', r.refusal?.message || r.error || 'Compose failed'); return; }
    setManifest(r.manifest || '');
    setComposeEdges(r.edges || []);
    setComposeData(r);
    addMsg('jarvis', `Manifest composed. ${r.gaps?.length ? `${r.gaps.length} gaps to fill — see below.` : 'No gaps detected.'} ${r.defaults_applied ? 'Defaults were applied.' : ''}`);
    // Auto-plan
    const p = await api('plan', {manifest: r.manifest});
    if (p.ok) { setPlanNodes(p.nodes||[]); addMsg('jarvis', `Plan compiled: ${p.nodes?.length} node(s). pauses_at: ${JSON.stringify(p.pauses_at||[])}`); }
  }

  async function onLint() {
    if (!manifest) return;
    setLoad('lint', true);
    const r = await api('lint', {manifest});
    setLoad('lint', false);
    if (r.ok) addMsg('jarvis', `Lint OK — graph_id: ${r.graph_id}, ${r.nodes?.length} nodes, ${r.edges} edge(s).`);
    else addMsg('err', r.refusal?.message || r.error || 'Lint failed');
  }

  async function onPlan() {
    if (!manifest) return;
    setLoad('plan', true);
    const r = await api('plan', {manifest});
    setLoad('plan', false);
    if (r.ok) {
      setPlanNodes(r.nodes||[]);
      addMsg('jarvis', `Plan: ${r.nodes?.length} node(s). Pauses at: ${JSON.stringify(r.pauses_at||[])}.`);
    } else addMsg('err', r.refusal?.message || r.error || 'Plan failed');
  }

  async function onSave(name) {
    if (!name || !manifest) { addMsg('err', 'Provide a name and a manifest first.'); return; }
    const r = await api('graph.save', {name, manifest});
    if (r.ok) {
      addMsg('jarvis', `Saved as "${r.name}" at ${r.path}`);
      setGraphs(g => [...new Set([...g, name])].sort());
    } else addMsg('err', r.refusal?.message || r.error || 'Save failed');
  }

  async function onLoadGraph(name) {
    const r = await api('graph.read', {name});
    if (!r.ok) { addMsg('err', r.error || 'Could not read graph'); return; }
    setManifest(r.manifest);
    addMsg('jarvis', `Loaded graph "${name}".`);
    const p = await api('plan', {manifest: r.manifest});
    if (p.ok) setPlanNodes(p.nodes||[]);
  }

  async function onApprove(decision, confirm) {
    if (!selectedRun || !selectedNodeId) return null;
    const r = await api('approve', {run: selectedRun, node_id: selectedNodeId, decision, confirm});
    if (!r.ok) { addMsg('err', r.refusal?.message || r.error || 'Approve call failed'); return null; }
    if (confirm && r.projection) setProjection(r.projection);
    return r;
  }

  return html`<div id="app" style="display:flex;flex-direction:column;height:100%">
    <div class="hdr">
      <span class="hdr-title">JAR<span>VIS</span></span>
      <span class="hdr-sep">|</span>
      <span class="hdr-ws">${ws?.project_root || ws?.root || '…'}</span>
      ${ws?.reason && html`<span class="hdr-why">${ws.origin}: ${ws.reason}</span>`}
      <span class="hdr-spacer"/>
      <span class="hdr-label">Run:</span>
      <select value=${selectedRun||''} onChange=${e=>setSelectedRun(e.target.value||null)}
        style="background:var(--surf2);color:var(--ink);border:1px solid var(--line2);border-radius:4px;padding:4px 8px;font-size:12px">
        <option value="">— none —</option>
        ${runs.map(r => html`<option key=${r} value=${r}>${r}</option>`)}
      </select>
    </div>
    <div class="honesty-banner">
      <b>Note:</b> Jarvis is not a language model. It searches the shipped catalog and assembles
      graphs from it. Model work happens inside the graph nodes, driven by your own CLI or key.
    </div>
    <div class="cols">
      <${LeftCol} transcript=${transcript} cmdInput=${cmdInput} setCmdInput=${setCmdInput}
        onSearch=${onSearch} searchRes=${searchRes} onCompose=${onCompose}
        manifest=${manifest} setManifest=${setManifest} composeData=${composeData}
        onLint=${onLint} onPlan=${onPlan} onSave=${onSave}
        graphs=${graphs} onLoadGraph=${onLoadGraph} loading=${loading}/>
      <${CentreCol} projection=${projection} planNodes=${planNodes} composeEdges=${composeEdges}
        selectedNodeId=${selectedNodeId} setSelectedNodeId=${setSelectedNodeId}
        runs=${runs} selectedRun=${selectedRun} setSelectedRun=${setSelectedRun} loading=${loading}/>
      <${RightCol} selectedNodeId=${selectedNodeId} projection=${projection} planNodes=${planNodes}
        forms=${forms} caps=${caps} selectedRun=${selectedRun} onApprove=${onApprove} loading=${loading}/>
    </div>
  </div>`;
}

ReactDOM.createRoot(document.getElementById('root')).render(html`<${App}/>`);
