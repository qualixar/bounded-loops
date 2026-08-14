// Jarvis — bounded-loops graph designer and runner.
// React 18 (UMD) + htm 3.1.1. No build step. No CDN. No network at runtime.
// Token: read once from URL, never written to DOM, sent only in POST bodies.
import htm from './vendor/htm.module.js';

const {useState, useEffect, useRef, useCallback, useMemo} = React;
const html = htm.bind(React.createElement);

// Token is read once at module load — never stored in React state (which can render to DOM).
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
const STATE_HEX = {
  PENDING:'#4a5566', READY:'#5b8bff', WORKING:'#e0a44a', CHECKING:'#a688ff',
  PASSED:'#34d9a0', FAILED:'#ff6b88', BLOCKED:'#ff4466', HALTED:'#ff8c42',
  CANCELLED:'#4a5566', SKIPPED:'#3b4455', SUCCEEDED:'#34d9a0', RUNNING:'#5b8bff',
};
function stateHex(s) { return STATE_HEX[s] || '#4a5566'; }

// ── DAG layout ─────────────────────────────────────────────────────────────
// nodes: array of {node_id} | {id}
// edges: array of [from,to] tuples or {from_node,to_node} objects
// levels: from projection — array of arrays of node_id strings (preferred)
function layoutDAG(nodes, edges, levels) {
  if (!nodes || !nodes.length) return {pos:{}, W:240, H:100, NW:160, NH:58};
  const NW=160, NH=58, CG=80, RG=14;
  const pos = {};
  const nid = n => n.node_id ?? n.id;

  if (levels && levels.length > 0) {
    levels.forEach((lvl, col) => {
      lvl.forEach((id, row) => { pos[id] = {x: col*(NW+CG), y: row*(NH+RG)}; });
    });
    let extra = 0;
    nodes.forEach(n => {
      if (!pos[nid(n)]) { pos[nid(n)] = {x: levels.length*(NW+CG), y: extra*(NH+RG)}; extra++; }
    });
  } else {
    // BFS topological layering
    const adjIn = {}, adjOut = {};
    nodes.forEach(n => { adjIn[nid(n)]=[]; adjOut[nid(n)]=[]; });
    (edges||[]).forEach(e => {
      const f = Array.isArray(e) ? e[0] : (e.from_node ?? e.from);
      const t = Array.isArray(e) ? e[1] : (e.to_node ?? e.to);
      if (adjOut[f] !== undefined) adjOut[f].push(t);
      if (adjIn[t] !== undefined) adjIn[t].push(f);
    });
    const depth = {}, q = nodes.filter(n => !adjIn[nid(n)].length);
    q.forEach(n => { depth[nid(n)] = 0; });
    let i = 0;
    while (i < q.length) {
      const n = q[i++], id = nid(n), d = depth[id]+1;
      (adjOut[id]||[]).forEach(tid => {
        if (depth[tid] === undefined || depth[tid] < d) depth[tid] = d;
        if (!q.find(x => nid(x) === tid)) {
          const tn = nodes.find(x => nid(x) === tid);
          if (tn) q.push(tn);
        }
      });
    }
    nodes.forEach(n => { if (depth[nid(n)] === undefined) depth[nid(n)] = 0; });
    const cols = {};
    nodes.forEach(n => {
      const d = depth[nid(n)];
      if (!cols[d]) cols[d] = [];
      cols[d].push(nid(n));
    });
    Object.entries(cols).forEach(([c, ids]) =>
      ids.forEach((id, r) => { pos[id] = {x: parseInt(c)*(NW+CG), y: r*(NH+RG)}; })
    );
  }

  const xs = Object.values(pos).map(p=>p.x), ys = Object.values(pos).map(p=>p.y);
  const W = Math.max(240, Math.max(...xs)+NW+16);
  const H = Math.max(100, Math.max(...ys)+NH+16);
  return {pos, W, H, NW, NH};
}

// ── Graph SVG ───────────────────────────────────────────────────────────────
function GraphSVG({nodes, edges, levels, nodeStates, selectedId, onSelect}) {
  const {pos, W, H, NW, NH} = useMemo(
    () => layoutDAG(nodes, edges, levels), [nodes, edges, levels]
  );
  if (!nodes || !nodes.length) {
    return html`<div className="dim" style=${{padding:'20px'}}>No nodes to display.</div>`;
  }

  const nid = n => n.node_id ?? n.id;
  const edgeList = (edges||[]).map(e => ({
    f: Array.isArray(e) ? e[0] : (e.from_node ?? e.from),
    t: Array.isArray(e) ? e[1] : (e.to_node ?? e.to),
    when: Array.isArray(e) ? null : e.when,
  }));

  return html`
    <div className="dag-wrap">
      <svg className="dag-svg" viewBox="0 0 ${W} ${H}" width=${W} height=${H}
           xmlns="http://www.w3.org/2000/svg">
        <defs>
          <marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L0,6 L8,3 z" fill="#2a3248"/>
          </marker>
        </defs>
        ${edgeList.map(({f,t,when}) => {
          const fp=pos[f], tp=pos[t];
          if (!fp || !tp) return null;
          const x1=fp.x+NW, y1=fp.y+NH/2, x2=tp.x, y2=tp.y+NH/2, cx=(x1+x2)/2;
          const tState = nodeStates?.[t] || 'PENDING';
          const stroke = (tState==='PASSED'||tState==='SUCCEEDED') ? stateHex(tState) : '#2a3248';
          return html`<g key="${f}-${t}">
            <path d="M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2},${y2}"
              fill="none" stroke=${stroke} strokeWidth="1.5" markerEnd="url(#arr)"/>
            ${when ? html`<text x=${cx} y=${Math.min(y1,y2)-4} textAnchor="middle"
              fontSize="9" fill="#4a5566">${when}</text>` : null}
          </g>`;
        })}
        ${nodes.map(n => {
          const id = nid(n), p = pos[id];
          if (!p) return null;
          const state = nodeStates?.[id] || n.state || 'PENDING';
          const color = stateHex(state);
          const isSel = id === selectedId;
          const isPaused = n.pauses_for_a_human || n.kind === 'approval';
          return html`<g key=${id} onClick=${()=>onSelect(id)} style=${{cursor:'pointer'}}>
            <rect x=${p.x} y=${p.y} width=${NW} height=${NH} rx="5"
              fill="#111318" stroke=${isSel ? '#4f7bff' : '#1e2433'}
              strokeWidth=${isSel ? 2 : 1}/>
            <rect x=${p.x} y=${p.y} width="3" height=${NH} rx="1.5" fill=${color}/>
            <text x=${p.x+12} y=${p.y+20} fontSize="12" fontWeight="600"
              fill=${isSel ? '#4f7bff' : '#d8dde6'}
              fontFamily="ui-monospace,monospace">${id}</text>
            <text x=${p.x+12} y=${p.y+37} fontSize="10" fill="#6b7688"
              fontFamily="system-ui,sans-serif">${n.kind||''}</text>
            ${isPaused ? html`<circle cx=${p.x+NW-10} cy=${p.y+10} r="5"
              fill="#a688ff" opacity="0.9"/>` : null}
            ${state && state!=='PENDING' ? html`<text x=${p.x+NW-6} y=${p.y+NH-7}
              fontSize="9" textAnchor="end" fill=${color}
              fontFamily="ui-monospace,monospace">${state}</text>` : null}
          </g>`;
        })}
      </svg>
    </div>`;
}

// ── Form field widgets ─────────────────────────────────────────────────────
// Choices with available:false are rendered disabled with the reason visible —
// never silently dropped. The compiler refuses them; showing them lets users
// understand why an option from the docs does not work.
function ChoiceSelect({field, value, onChange}) {
  const unavailItems = (field.choices||[]).filter(c => !c.available);
  return html`
    <div>
      <select className="form-select" value=${value||''} onChange=${e=>onChange(e.target.value)}>
        <option value="">— pick —</option>
        ${(field.choices||[]).map(c => html`
          <option key=${c.value} value=${c.value} disabled=${!c.available}>
            ${c.value}${!c.available ? ' (unavailable)' : ''}
          </option>`)}
      </select>
      ${unavailItems.length > 0 ? html`
        <div className="unavail-list">
          ${unavailItems.map(c => html`
            <div className="unavail-item" key=${c.value}>
              <b>${c.value}</b>: ${c.reason || 'not available on any host'}
            </div>`)}
        </div>` : null}
    </div>`;
}

function FieldInput({field, value, onChange, depth=0}) {
  if (field.kind === 'enum' || field.choices?.length) {
    return html`<${ChoiceSelect} field=${field} value=${value} onChange=${onChange}/>`;
  }
  if (field.kind === 'boolean') {
    return html`<label className="check-label">
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
    return html`<div className=${depth ? 'form-nested' : ''}>
      <${FormFields} fields=${field.fields} values=${value||{}}
        onChange=${(k,v) => onChange({...(value||{}), [k]:v})} depth=${depth+1}/>
    </div>`;
  }
  return html`<input className="form-input" type="text" value=${value??''}
    placeholder=${field.description||''} onChange=${e=>onChange(e.target.value)}/>`;
}

function FormFields({fields, values, onChange, depth=0}) {
  if (!fields?.length) return null;
  return html`<div className="form-fields">
    ${fields.map(f => html`
      <div key=${f.name}>
        <div className="form-field-label">
          ${f.name}${f.required ? html`<span className="req"> *</span>` : null}
          ${f.description ? html`<span className="field-hint">${' — '}${f.description}</span>` : null}
        </div>
        <${FieldInput} field=${f} value=${values?.[f.name]} depth=${depth}
          onChange=${v => onChange(f.name, v)}/>
      </div>`)}
  </div>`;
}

// ── Left column — Command ──────────────────────────────────────────────────
function LeftCol({transcript, cmdInput, setCmdInput, onSearch, searchRes, onCompose,
                  manifest, setManifest, composeData, onLint, onPlan, onSave,
                  graphs, onLoadGraph, loading}) {
  const endRef = useRef(null);
  useEffect(() => { endRef.current?.scrollIntoView({behavior:'smooth'}); }, [transcript]);
  const [saveName, setSaveName] = useState('');
  const [showSaveRow, setShowSaveRow] = useState(false);

  return html`
    <div className="col">
    <div className="col-hdr"><span>COMMAND</span></div>
    <div className="cmd-wrap-outer">
      <div className="cmd-input-row">
        <input className="cmd-input" type="text" value=${cmdInput}
          placeholder="Describe your task…"
          onInput=${e => setCmdInput(e.target.value)}
          onKeyDown=${e => { if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();onSearch();} }}/>
        <button className="btn primary" onClick=${onSearch} disabled=${loading.search}>
          ${loading.search ? html`<span className="spinner"/>` : 'Search'}
        </button>
      </div>
    </div>
    <div className="col-body left-inner">
      <div className="transcript">
        ${transcript.map((m,i) => html`
          <div key=${i} className=${'tx-msg '+m.role}>
            <div className="tx-role">${m.role==='user'?'you':m.role==='err'?'error':m.role}</div>
            ${m.text}
          </div>`)}
        ${searchRes ? html`
          <div className="tx-msg jarvis">
            <div className="tx-role">jarvis — search results</div>
            ${searchRes.ranking_caveat ? html`
              <div className="caveat">${searchRes.ranking_caveat}</div>` : null}
            <div className="search-results">
              ${(searchRes.candidates||[]).map(c => html`
                <div key=${c.name} className="search-item">
                  <div className="search-name">${c.name}</div>
                  <div className="search-meta">
                    gate: ${c.gate_kind} · score: ${c.score?.toFixed(2)} · ${c.keyless ? 'keyless' : 'needs key'}
                  </div>
                  <div className="search-desc">${c.description}</div>
                </div>`)}
            </div>
            ${searchRes.candidates?.length > 0 ? html`
              <button className="btn primary btn-compose" onClick=${onCompose}
                disabled=${loading.compose}>
                ${loading.compose ? html`<span className="spinner"/>` : 'Compose graph from these'}
              </button>` : null}
          </div>` : null}
        ${manifest ? html`
          <div className="tx-msg jarvis">
            <div className="tx-role">manifest</div>
            ${composeData?.gaps?.length > 0 ? html`
              <div className="gap-row">
                ${composeData.gaps.map((g,i) => html`
                  <div className="gap-item" key=${i}>
                    <b>${g.node_id}</b>: ${g.gap} — ${g.next_step}
                  </div>`)}
              </div>` : null}
            <textarea className="manifest-area" value=${manifest}
              onInput=${e => setManifest(e.target.value)} rows="10"/>
            <div className="btn-row">
              <button className="btn sm" onClick=${onLint} disabled=${loading.lint}>
                ${loading.lint ? html`<span className="spinner"/>` : 'Lint'}
              </button>
              <button className="btn sm" onClick=${onPlan} disabled=${loading.plan}>
                ${loading.plan ? html`<span className="spinner"/>` : 'Plan'}
              </button>
              <button className="btn sm primary" onClick=${()=>setShowSaveRow(v=>!v)}>Save…</button>
            </div>
            ${showSaveRow ? html`
              <div className="cmd-input-row save-row">
                <input className="cmd-input" type="text" value=${saveName}
                  placeholder="graph-name" onInput=${e=>setSaveName(e.target.value)}/>
                <button className="btn sm primary"
                  onClick=${()=>{onSave(saveName);setShowSaveRow(false);setSaveName('');}}>
                  Save
                </button>
              </div>` : null}
          </div>` : null}
        <div ref=${endRef}/>
      </div>
      ${graphs.length > 0 ? html`
        <hr className="divider"/>
        <div className="graph-section-hdr" style=${{padding:'0 0 4px 0'}}>Saved graphs</div>
        <div className="graph-list">
          ${graphs.map(g => html`
            <div key=${g} className="graph-item" onClick=${()=>onLoadGraph(g)}>
              <span className="graph-item-name">${g}</span>
              <span className="tag">load</span>
            </div>`)}
        </div>` : null}
    </div>
    </div>`;
}

// ── Centre column — Graph ──────────────────────────────────────────────────
function CentreCol({projection, planNodes, composeEdges, selectedNodeId, setSelectedNodeId,
                    runs, selectedRun, setSelectedRun, loading}) {
  const nodes = projection ? projection.nodes : (planNodes || []);
  const edges = projection ? projection.edges : (composeEdges || []);
  const levels = projection?.levels || null;
  const nodeStates = useMemo(() => {
    if (!projection?.nodes) return {};
    return Object.fromEntries(projection.nodes.map(n => [n.node_id, n.state]));
  }, [projection]);

  const rs = projection?.run_state;
  const bannerCls = rs === SUCCESS ? 'run-banner succeeded'
    : rs === 'FAILED' ? 'run-banner failed'
    : (rs === 'RUNNING' || rs === 'PENDING') ? 'run-banner running'
    : rs ? 'run-banner halted' : '';

  return html`
    <div className="col">
    <div className="col-hdr">
      <span>GRAPH</span>
      <div className="col-hdr-actions">
        ${loading.run ? html`<span className="spinner"/>` : null}
        ${runs.length > 0 ? html`
          <select className="col-hdr-select" value=${selectedRun||''}
            onChange=${e=>setSelectedRun(e.target.value||null)}>
            <option value="">— no active run —</option>
            ${runs.map(r => html`<option key=${r} value=${r}>${r}</option>`)}
          </select>` : null}
      </div>
    </div>
    <div className="col-graph-body">
      ${rs ? html`
        <div className=${bannerCls}>
          <span className="state-dot" style=${{background:stateHex(rs)}}/>
          <span className="state-label" style=${{color:stateHex(rs)}}>${rs}</span>
          ${rs !== SUCCESS && TERMINAL.has(rs)
            ? html`<span className="dim"> — this run did not succeed</span>` : null}
          ${rs === 'RUNNING' || rs === 'PENDING'
            ? html`<span className="dim"> — in flight</span>` : null}
          ${projection?.run_id ? html`
            <span className="tiny-mono">${projection.run_id.slice(0,12)}…</span>` : null}
        </div>` : null}
      ${nodes.length > 0
        ? html`<${GraphSVG} nodes=${nodes} edges=${edges} levels=${levels}
                  nodeStates=${nodeStates} selectedId=${selectedNodeId}
                  onSelect=${setSelectedNodeId}/>`
        : html`<div className="dim center-msg">
            No graph yet. Compose one from the command column.
          </div>`}
    </div>
    </div>`;
}

// ── Right column — Evidence ────────────────────────────────────────────────
function RightCol({selectedNodeId, projection, planNodes, forms, caps,
                   selectedRun, onApprove, loading}) {
  const [formVals, setFormVals] = useState({});
  const [approvePreview, setApprovePreview] = useState(null);
  const [approving, setApproving] = useState(false);

  const projNode = projection?.nodes?.find(n => n.node_id === selectedNodeId);
  const planNode = planNodes?.find(n => (n.node_id ?? n.id) === selectedNodeId);
  const nodeKind = projNode?.kind ?? planNode?.kind ?? '';
  const nodeFields = forms?.nodes?.[nodeKind];
  const state = projNode?.state || (planNode ? 'PENDING' : null);

  // Approval node needs human action only when it's not yet terminal
  const needsApproval = nodeKind === 'approval' && projNode &&
    !TERMINAL.has(projNode.state) && projNode.state !== 'PASSED';

  async function doPreview(decision) {
    setApproving(true);
    const r = await onApprove(decision, false);
    if (r) setApprovePreview({...r, decision});
    setApproving(false);
  }
  async function doConfirm() {
    if (!approvePreview) return;
    setApproving(true);
    await onApprove(approvePreview.decision, true);
    setApprovePreview(null);
    setApproving(false);
  }

  if (!selectedNodeId) return html`
    <div className="col">
    <div className="col-hdr"><span>EVIDENCE</span></div>
    <div className="col-body">
      <div className="dim" style=${{padding:'20px 0'}}>
        Select a node in the graph to inspect it.
      </div>
    </div>
    </div>`;

  const isoKey = projNode?.isolation ?? planNode?.isolation;
  const capIso = caps?.isolation?.[isoKey];
  const spend = projNode ? {
    tokens: projNode.spend_tokens ?? 0,
    cost: projNode.spend_cost_microunits ?? 0,
    complete: projNode.spend_complete ?? true,
  } : null;
  const maxAttempts = planNode?.max_attempts ?? 1;

  return html`
    <div className="col">
    <div className="col-hdr">
      <span>EVIDENCE — ${selectedNodeId}</span>
    </div>
    <div className="col-body">
      <div className="evidence">

        <div className="ev-section">
          <div className="ev-title">Node</div>
          <div className="ev-row">
            <span className="ev-key">id</span>
            <span className="ev-val">${selectedNodeId}</span>
          </div>
          <div className="ev-row">
            <span className="ev-key">kind</span>
            <span className="ev-val">${nodeKind||'—'}</span>
          </div>
          ${state ? html`
            <div className="ev-row">
              <span className="ev-key">state</span>
              <span className="ev-val" style=${{color:stateHex(state)}}>${state}</span>
            </div>` : null}
          ${projNode?.attempt !== undefined ? html`
            <div className="ev-row">
              <span className="ev-key">attempt</span>
              <span className="ev-val">${projNode.attempt} / ${maxAttempts}</span>
            </div>` : null}
          ${(projNode?.required_effects?.length || planNode?.effects?.length) ? html`
            <div className="ev-row">
              <span className="ev-key">effects</span>
              <span className="ev-val">
                ${(projNode?.required_effects || planNode?.effects || []).join(', ')||'none'}
              </span>
            </div>` : null}
        </div>

        ${isoKey ? html`
          <div className="ev-section">
            <div className="ev-title">Isolation — controls actually enforced here</div>
            <div className="ev-row">
              <span className="ev-key">tier</span>
              <span className="ev-val">${isoKey}</span>
            </div>
            ${capIso ? html`
              <div className="ev-row">
                <span className="ev-key">deliverable here</span>
                <span className=${'ev-val '+(capIso.deliverable_here?'ok':'err')}>
                  ${capIso.deliverable_here ? 'yes' : 'no'}
                </span>
              </div>
              ${capIso.reason_if_not ? html`
                <div className="ev-row">
                  <span className="ev-key">reason</span>
                  <span className="ev-val warn">${capIso.reason_if_not}</span>
                </div>` : null}
              <div className="ev-row">
                <span className="ev-key">controls</span>
                <span className="ev-val">
                  ${capIso.controls_enforced_here?.length
                    ? capIso.controls_enforced_here.join(', ')
                    : html`<span className="warn">none enforced on this host</span>`}
                </span>
              </div>` : html`
              <div className="ev-row">
                <span className="ev-key">detail</span>
                <span className="ev-val warn">capability data not loaded</span>
              </div>`}
          </div>` : null}

        ${spend ? html`
          <div className="ev-section">
            <div className="ev-title">
              Spend${!spend.complete ? ' (lower bound — some attempts reported nothing)' : ''}
            </div>
            <div className="ev-row">
              <span className="ev-key">tokens</span>
              <span className="ev-val">${spend.tokens.toLocaleString()}</span>
            </div>
            <div className="ev-row">
              <span className="ev-key">cost</span>
              <span className="ev-val">${(spend.cost/1000000).toFixed(6)} USD</span>
            </div>
          </div>` : null}

        ${projNode?.artifact_digests?.length > 0 ? html`
          <div className="ev-section">
            <div className="ev-title">Artifacts</div>
            ${projNode.artifact_digests.map((d,i) => html`
              <div className="ev-row" key=${i}>
                <span className="ev-key">${i+1}</span>
                <span className="ev-val artifact-val mono">${d.slice(0,22)}…</span>
              </div>`)}
          </div>` : null}

        ${needsApproval && selectedRun ? html`
          <div className="ev-section">
            <div className="ev-title">Approval required</div>
            <div className="approve-panel">
              ${approvePreview ? html`
                <div>
                  <div className="approve-preview">${approvePreview.would}</div>
                  <div className="dim approve-hint">${approvePreview.hint}</div>
                  <div className="approve-btns">
                    <button className="btn ok" onClick=${doConfirm} disabled=${approving}>
                      ${approving ? html`<span className="spinner"/>` : 'Confirm'}
                    </button>
                    <button className="btn sm" onClick=${()=>setApprovePreview(null)}>
                      Cancel
                    </button>
                  </div>
                </div>` : html`
                <div className="approve-btns">
                  <button className="btn ok" onClick=${()=>doPreview('approved')}
                    disabled=${approving}>
                    ${approving ? html`<span className="spinner"/>` : 'Approve'}
                  </button>
                  <button className="btn danger" onClick=${()=>doPreview('rejected')}
                    disabled=${approving}>Reject</button>
                </div>`}
            </div>
          </div>` : null}

        ${nodeFields?.length > 0 ? html`
          <div className="ev-section">
            <div className="ev-title">Node configuration (${nodeKind})</div>
            <${FormFields} fields=${nodeFields} values=${formVals}
              onChange=${(k,v) => setFormVals(prev => ({...prev, [k]:v}))}/>
          </div>` : null}
      </div>
    </div>
    </div>`;
}

// ── App root ────────────────────────────────────────────────────────────────
function App() {
  const [ws,           setWs]          = useState(null);
  const [caps,         setCaps]        = useState(null);
  const [forms,        setForms]       = useState(null);
  const [runs,         setRuns]        = useState([]);
  const [graphs,       setGraphs]      = useState([]);
  const [selectedRun,  setSelectedRun] = useState(null);
  const [projection,   setProjection]  = useState(null);
  const [planNodes,    setPlanNodes]   = useState([]);
  const [composeEdges, setComposeEdges]= useState([]);
  const [composeData,  setComposeData] = useState(null);
  const [manifest,     setManifest]    = useState('');
  const [cmdInput,     setCmdInput]    = useState('');
  const [transcript,   setTranscript]  = useState([]);
  const [searchRes,    setSearchRes]   = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [loading, setLoading] = useState({});
  const esSrc = useRef(null);

  const setLoad = useCallback((k, v) => setLoading(p => ({...p, [k]: v})), []);
  const addMsg  = useCallback((role, text) => setTranscript(t => [...t, {role, text}]), []);

  // Boot: workspace, capabilities, forms, runs list
  useEffect(() => {
    Promise.all([
      api('workspace'), api('capabilities'), api('forms'), api('runs'),
    ]).then(([w, c, f, r]) => {
      if (w.ok) { setWs(w); setGraphs(w.graphs||[]); }
      else addMsg('err', 'Workspace: '+w.error);
      if (c.ok) setCaps(c.capabilities);
      if (f.ok) setForms(f.forms);
      if (r.ok) setRuns(r.runs||[]);
    });
  }, []);

  // SSE: connect when a run is selected, disconnect on change/unmount.
  // Each SSE message is a full projection snapshot — replace state wholesale.
  // A closed stream is normal completion after a terminal state, not an error.
  useEffect(() => {
    if (esSrc.current) { esSrc.current.close(); esSrc.current = null; }
    if (!selectedRun) { setProjection(null); return; }

    // Fetch initial snapshot immediately (covers the no-stream case identically)
    api('run', {run: selectedRun}).then(r => { if (r.ok) setProjection(r.projection); });

    const qs = `token=${encodeURIComponent(_TOKEN)}&run=${encodeURIComponent(selectedRun)}`;
    const es = new EventSource(`/events?${qs}`);
    esSrc.current = es;

    es.onmessage = ev => {
      try {
        const snap = JSON.parse(ev.data);
        // snap is the raw ArenaProjection dict — run_state is the status key
        setProjection(snap);
        if (snap.run_state && TERMINAL.has(snap.run_state)) {
          es.close(); esSrc.current = null;
        }
      } catch {}
    };
    // Closed stream = terminal completion, not a failure — do not show error banner
    es.onerror = () => { es.close(); esSrc.current = null; };

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
    const n = r.candidates?.length || 0;
    addMsg('jarvis', `Found ${n} candidate${n===1?'':'s'}. ${r.no_match_means||''}`);
  }

  async function onCompose() {
    if (!searchRes?.candidates?.length) return;
    setLoad('compose', true);
    const slug = cmdInput.trim().replace(/\s+/g,'_').toLowerCase() || 'jarvis_graph';
    const nodes = searchRes.candidates.map(c => ({id: c.name, kind: c.gate_kind || 'loop'}));
    const r = await api('compose', {graph_id: slug, nodes});
    setLoad('compose', false);
    if (!r.ok) { addMsg('err', r.refusal?.message || r.error || 'Compose failed'); return; }
    setManifest(r.manifest || '');
    setComposeEdges(r.edges || []);
    setComposeData(r);
    const gapMsg = r.gaps?.length ? `${r.gaps.length} gap(s) to fill — see below.` : 'No gaps detected.';
    addMsg('jarvis', `Manifest composed. ${gapMsg}${r.defaults_applied ? ' Defaults applied.' : ''}`);
    // Auto-plan so the DAG shows immediately
    const p = await api('plan', {manifest: r.manifest});
    if (p.ok) {
      setPlanNodes(p.nodes||[]);
      addMsg('jarvis', `Plan compiled: ${p.nodes?.length} node(s). Pauses at: ${JSON.stringify(p.pauses_at||[])}.`);
    }
  }

  async function onLint() {
    if (!manifest) return;
    setLoad('lint', true);
    const r = await api('lint', {manifest});
    setLoad('lint', false);
    if (r.ok) addMsg('jarvis', `Lint OK — graph_id: ${r.graph_id}, ${r.nodes?.length} node(s), ${r.edges} edge(s).`);
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
    if (!name || !manifest) { addMsg('err', 'Need a name and a manifest.'); return; }
    const r = await api('graph.save', {name, manifest});
    if (r.ok) {
      addMsg('jarvis', `Saved as "${r.name}".`);
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

  // approve: confirm=false = preview, confirm=true = record decision (MUTATING)
  async function onApprove(decision, confirm) {
    if (!selectedRun || !selectedNodeId) return null;
    const r = await api('approve', {run: selectedRun, node_id: selectedNodeId, decision, confirm});
    if (!r.ok) { addMsg('err', r.refusal?.message || r.error || 'Approve call failed'); return null; }
    if (confirm && r.projection) setProjection(r.projection);
    return r;
  }

  return html`
    <div id="app" style=${{display:'flex',flexDirection:'column',height:'100%'}}>
      <div className="hdr">
        <span className="hdr-title">JAR<span>VIS</span></span>
        <span className="hdr-sep">|</span>
        <span className="hdr-ws">${ws?.project_root || ws?.root || '…'}</span>
        ${ws?.reason ? html`<span className="hdr-why">${ws.origin}: ${ws.reason}</span>` : null}
        <span className="hdr-spacer"/>
        <span className="hdr-label">Run:</span>
        <select className="hdr-select" value=${selectedRun||''}
          onChange=${e=>setSelectedRun(e.target.value||null)}>
          <option value="">— none —</option>
          ${runs.map(r => html`<option key=${r} value=${r}>${r}</option>`)}
        </select>
      </div>
      <div className="honesty-banner">
        <b>Note:</b> Jarvis is not a language model. It searches the shipped catalog
        and assembles graphs from it. Model work happens inside the nodes,
        driven by your own CLI or key.
      </div>
      <div className="cols">
        <${LeftCol}
          transcript=${transcript} cmdInput=${cmdInput} setCmdInput=${setCmdInput}
          onSearch=${onSearch} searchRes=${searchRes} onCompose=${onCompose}
          manifest=${manifest} setManifest=${setManifest} composeData=${composeData}
          onLint=${onLint} onPlan=${onPlan} onSave=${onSave}
          graphs=${graphs} onLoadGraph=${onLoadGraph} loading=${loading}/>
        <${CentreCol}
          projection=${projection} planNodes=${planNodes} composeEdges=${composeEdges}
          selectedNodeId=${selectedNodeId} setSelectedNodeId=${setSelectedNodeId}
          runs=${runs} selectedRun=${selectedRun} setSelectedRun=${setSelectedRun}
          loading=${loading}/>
        <${RightCol}
          selectedNodeId=${selectedNodeId} projection=${projection} planNodes=${planNodes}
          forms=${forms} caps=${caps} selectedRun=${selectedRun}
          onApprove=${onApprove} loading=${loading}/>
      </div>
    </div>`;
}

ReactDOM.createRoot(document.getElementById('root')).render(html`<${App}/>`);
