// columns.js — WorkspaceRail, MonitorCol, ConfigPanel.
// Three zones: narrow rail (220px) | hero monitor (1fr) | config panel (360px).
// No Command column. No chat. No ask route. The agent already has an orchestrator.

import htm from './vendor/htm.module.js';
import { GraphSVG, stateHex } from './dag.js';
import { FormFields } from './forms.js';

const html = htm.bind(React.createElement);
const { useState, useMemo, useCallback } = React;

const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'HALTED', 'CANCELLED', 'EXPIRED']);

// ── Inline icons (16px grid, currentColor, SVG only) ─────────────────────────
const IcoRefresh = () => html`<svg width="12" height="12" viewBox="0 0 12 12" fill="none">
  <path d="M10 6A4 4 0 1 1 6 2.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
  <polyline points="6,0 8,2.2 5.8,4" stroke="currentColor" strokeWidth="1.4"
    strokeLinecap="round" strokeLinejoin="round"/>
</svg>`;

const IcoLink = () => html`<svg width="12" height="12" viewBox="0 0 12 12" fill="none">
  <path d="M5 3H2a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V7"
    stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
  <path d="M8 1h3v3" stroke="currentColor" strokeWidth="1.3"
    strokeLinecap="round" strokeLinejoin="round"/>
  <line x1="5.5" y1="6.5" x2="11" y2="1" stroke="currentColor"
    strokeWidth="1.3" strokeLinecap="round"/>
</svg>`;

// ── Run state helpers ─────────────────────────────────────────────────────────
function stateDesc(rs) {
  if (rs === 'SUCCEEDED')         return 'Completed successfully';
  if (rs === 'FAILED')            return 'Failed — not a success';
  if (rs === 'HALTED')            return 'Halted — did not complete';
  if (rs === 'CANCELLED')         return 'Cancelled';
  if (rs === 'EXPIRED')           return 'Expired';
  if (rs === 'AWAITING_APPROVAL') return 'Paused — approval required';
  if (rs === 'RUNNING')           return 'In flight…';
  if (rs === 'PENDING')           return 'Queued…';
  return '';
}

function bannerCls(rs) {
  if (rs === 'SUCCEEDED') return 'run-banner succeeded';
  if (rs === 'FAILED' || rs === 'BLOCKED') return 'run-banner failed';
  if (rs === 'RUNNING' || rs === 'PENDING' || rs === 'READY') return 'run-banner running';
  if (rs === 'AWAITING_APPROVAL') return 'run-banner awaiting';
  return 'run-banner halted';
}

// ── WorkspaceRail ─────────────────────────────────────────────────────────────
// Left rail: runs list, saved graphs, orchestrator connection status (informational only).
export function WorkspaceRail({
  runs, selectedRun, setSelectedRun, selectedRunState,
  graphs, onLoadGraph,
  agents, lastPoll, onRefresh, loading,
}) {
  return html`
    <div className="col">
      <div className="col-hdr">
        <div className="col-hdr-label"><span>Workspace</span></div>
        <div className="col-hdr-actions">
          <button className="btn btn-ghost btn-sm" onClick=${onRefresh}
            disabled=${loading.poll} title="Refresh workspace">
            ${loading.poll ? html`<span className="spinner"/>` : html`<${IcoRefresh}/>`}
          </button>
        </div>
      </div>

      <div className="rail-body">

        ${agents.length > 0 ? html`
          <div className="rail-section">
            <div className="rail-section-hdr">Orchestrators</div>
            ${agents.map(a => html`
              <div className="conn-item" key=${a.id || a.binary}>
                <span className=${'conn-dot ' + (a.available !== false ? 'ok' : 'muted')}/>
                <span className="conn-label">${a.binary || a.name || a.id}</span>
                ${a.prompt_via ? html`
                  <span className="conn-detail">${a.prompt_via}</span>` : null}
              </div>`)}
          </div>
          <div className="rail-divider"/>` : null}

        <div className="rail-section">
          <div className="rail-section-hdr">Runs</div>
          ${runs.length === 0 ? html`
            <div className="conn-item" style=${{ color: 'var(--ink-3)', fontSize: 'var(--t-xs)', lineHeight: '1.6' }}>
              No runs yet.
              Your agent will write new runs automatically.
            </div>` : runs.map(r => {
              const active   = r === selectedRun;
              const rs       = active ? selectedRunState : null;
              const dotColor = rs ? stateHex(rs) : '#2a3147';
              const isLive   = rs === 'RUNNING' || rs === 'PENDING';
              return html`
                <div key=${r}
                  className=${'run-item' + (active ? ' active' : '')}
                  onClick=${() => setSelectedRun(active ? null : r)}
                  role="button" aria-pressed=${active} tabIndex="0"
                  onKeyDown=${e => { if (e.key === 'Enter') setSelectedRun(active ? null : r); }}>
                  <span className=${'run-item-dot' + (isLive ? ' pulsing' : '')}
                    style=${{ background: dotColor }}/>
                  <span className="run-item-name">${r}</span>
                  ${rs ? html`<span className="run-item-badge" style=${{ color: dotColor }}>${rs}</span>` : null}
                </div>`;})}
        </div>

        ${graphs.length > 0 ? html`
          <div className="rail-divider"/>
          <div className="rail-section">
            <div className="rail-section-hdr">Saved graphs</div>
            ${graphs.map(g => html`
              <div key=${g} className="graph-item" onClick=${() => onLoadGraph(g)}
                role="button" tabIndex="0"
                onKeyDown=${e => { if (e.key === 'Enter') onLoadGraph(g); }}>
                <span className="graph-item-name">${g}</span>
                <span className="graph-item-tag">load</span>
              </div>`)}
          </div>` : null}

        ${lastPoll ? html`
          <div className="poll-hint">
            synced ${Math.round((Date.now() - lastPoll) / 1000)}s ago
          </div>` : null}
      </div>
    </div>`;
}

// ── MonitorCol ────────────────────────────────────────────────────────────────
// Hero centre column. Live DAG + run state banner + execute-confirm modal.
export function MonitorCol({
  projection, planNodes, graphEdges,
  selectedNodeId, setSelectedNodeId,
  selectedRun, loading,
  executePreview, onExecuteConfirm, onExecuteCancel,
}) {
  const nodes  = projection?.nodes ?? planNodes ?? [];
  const edges  = projection?.edges ?? graphEdges ?? [];
  const levels = projection?.levels ?? null;

  const nodeStates = useMemo(() => {
    if (!projection?.nodes) return {};
    return Object.fromEntries(projection.nodes.map(n => [n.node_id, n.state]));
  }, [projection]);

  const rs     = projection?.run_state;
  const isLive = rs === 'RUNNING' || rs === 'PENDING';

  return html`
    <div className="col">
      <div className="col-hdr">
        <div className="col-hdr-label">
          <span>Monitor</span>
          ${selectedRun ? html`
            <span className="mono dim" style=${{ fontSize: 'var(--t-xs)' }}>${selectedRun}</span>` : null}
        </div>
        <div className="col-hdr-actions">
          ${loading.run ? html`<span className="spinner"/>` : null}
        </div>
      </div>

      <div className="col-graph-body">

        ${rs ? html`
          <div className=${bannerCls(rs)}>
            <span className=${'run-state-dot' + (isLive ? ' pulsing' : '')}
              style=${{ background: stateHex(rs) }}/>
            <span className="run-state-label" style=${{ color: stateHex(rs) }}>${rs}</span>
            <span className="run-state-desc">${stateDesc(rs)}</span>
            ${rs !== 'SUCCEEDED' && TERMINAL.has(rs) ? html`
              <span className="tag err" style=${{ marginLeft: 'auto' }}>not succeeded</span>` : null}
            ${projection?.run_id ? html`
              <span className="run-state-id">${projection.run_id.slice(0, 12)}…</span>` : null}
          </div>` : null}

        ${nodes.length > 0
          ? html`<${GraphSVG}
              nodes=${nodes} edges=${edges} levels=${levels}
              nodeStates=${nodeStates}
              selectedId=${selectedNodeId}
              onSelect=${setSelectedNodeId}/>`
          : html`<div className="monitor-empty">
              <svg className="monitor-empty-icon" width="36" height="36" viewBox="0 0 16 16" fill="none">
                <rect x="1" y="1" width="5" height="5" rx="1"
                  stroke="currentColor" strokeWidth="1.2"/>
                <rect x="10" y="1" width="5" height="5" rx="1"
                  stroke="currentColor" strokeWidth="1.2"/>
                <rect x="5.5" y="10" width="5" height="5" rx="1"
                  stroke="currentColor" strokeWidth="1.2"/>
                <line x1="6" y1="3.5" x2="10" y2="3.5"
                  stroke="currentColor" strokeWidth="1.2"/>
                <line x1="8" y1="3.5" x2="8" y2="10"
                  stroke="currentColor" strokeWidth="1.2"/>
              </svg>
              <div className="monitor-empty-title">No graph loaded</div>
              <div className="monitor-empty-desc">
                Select a run from the workspace rail to watch it live,
                or load a saved graph and click Plan to preview the DAG.
                New runs appear automatically as your agent works.
              </div>
            </div>`}
      </div>

      ${executePreview ? html`
        <div className="modal-overlay" onClick=${onExecuteCancel}>
          <div className="modal" onClick=${e => e.stopPropagation()}
            role="dialog" aria-modal="true" aria-label="Execution preview">
            <div className="modal-title">Review before running</div>

            ${executePreview.effects?.length > 0 ? html`
              <div className="modal-section">
                <div className="modal-section-label">
                  Effects (${executePreview.effects.length})
                </div>
                ${executePreview.effects.map((ef, i) => html`
                  <div className="modal-item" key=${i}>${ef}</div>`)}
              </div>` : null}

            ${executePreview.ceilings && Object.keys(executePreview.ceilings).length > 0 ? html`
              <div className="modal-section">
                <div className="modal-section-label">Ceilings</div>
                ${Object.entries(executePreview.ceilings).map(([k, v]) => html`
                  <div className="modal-item" key=${k}>${k}: ${JSON.stringify(v)}</div>`)}
              </div>` : null}

            ${executePreview.pauses_at?.length > 0 ? html`
              <div className="modal-section">
                <div className="modal-section-label">Pauses for approval at</div>
                <div style=${{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                  ${executePreview.pauses_at.map(n => html`
                    <span className="tag info" key=${n}>${n}</span>`)}
                </div>
              </div>` : null}

            ${executePreview.run_dir_preview ? html`
              <div className="modal-section">
                <div className="modal-section-label">Run directory</div>
                <div className="modal-item">${executePreview.run_dir_preview}</div>
              </div>` : null}

            <div className="modal-btns">
              <button className="btn" onClick=${onExecuteCancel}>Cancel</button>
              <button className="btn btn-primary" onClick=${onExecuteConfirm}>
                Execute
              </button>
            </div>
          </div>
        </div>` : null}
    </div>`;
}

// ── ConfigPanel ───────────────────────────────────────────────────────────────
// Right panel: node details, isolation/spend evidence, forms, approval flow,
// and the persistent handoff affordance ("continue in your agent").
export function ConfigPanel({
  selectedNodeId, projection, planNodes, forms, caps,
  selectedRun, onApprove,
  graphName, onHandoff, handoffResult, handoffLoading, handoffError,
}) {
  const [formVals,      setFormVals]      = useState({});
  const [approvePreview, setApprovePreview] = useState(null);
  const [approving,     setApproving]     = useState(false);
  const [copied,        setCopied]        = useState(false);

  const projNode  = projection?.nodes?.find(n => n.node_id === selectedNodeId);
  const planNode  = planNodes?.find(n => (n.node_id ?? n.id) === selectedNodeId);
  const nodeKind  = projNode?.kind ?? planNode?.kind ?? '';
  const nodeState = projNode?.state ?? (planNode ? 'PENDING' : null);
  const nodeFields = forms?.nodes?.[nodeKind] ?? null;

  const needsApproval = nodeKind === 'approval' && projNode &&
    projNode.state === 'AWAITING_APPROVAL';

  const isoKey = projNode?.isolation ?? planNode?.isolation;
  const capIso = caps?.isolation?.[isoKey];

  const handleApprove = useCallback(async (decision, confirm) => {
    setApproving(true);
    const r = await onApprove(decision, confirm);
    setApproving(false);
    if (!confirm && r) setApprovePreview({ ...r, decision });
    if (confirm)  setApprovePreview(null);
    return r;
  }, [onApprove]);

  async function copyHandoff() {
    const text = handoffResult?.command;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2200);
    } catch {}
  }

  return html`
    <div className="col">
      <div className="col-hdr">
        <div className="col-hdr-label">
          <span>Configure</span>
          ${selectedNodeId ? html`
            <span className="mono dim" style=${{ fontSize: 'var(--t-xs)' }}>
              ${selectedNodeId}
            </span>` : null}
        </div>
      </div>

      <div className="col-body">

        ${!selectedNodeId ? html`
          <div className="panel-empty">
            Select a node in the graph to inspect and configure it.
          </div>` : html`

          <div className="evidence">

            <div className="ev-section">
              <div className="ev-section-title">Node</div>
              <div className="ev-row">
                <span className="ev-key">id</span>
                <span className="ev-val">${selectedNodeId}</span>
              </div>
              ${nodeKind ? html`
                <div className="ev-row">
                  <span className="ev-key">kind</span>
                  <span className="ev-val">${nodeKind}</span>
                </div>` : null}
              ${nodeState ? html`
                <div className="ev-row">
                  <span className="ev-key">state</span>
                  <span className="ev-val" style=${{ color: stateHex(nodeState) }}>
                    ${nodeState}
                    ${nodeState !== 'SUCCEEDED' && TERMINAL.has(nodeState)
                      ? html` <span style=${{ color: 'var(--danger)', fontSize: '9px' }}>✗</span>`
                      : null}
                  </span>
                </div>` : null}
              ${projNode?.attempt !== undefined ? html`
                <div className="ev-row">
                  <span className="ev-key">attempt</span>
                  <span className="ev-val">
                    ${projNode.attempt} / ${planNode?.max_attempts ?? projNode.max_attempts ?? 1}
                  </span>
                </div>` : null}
              ${(projNode?.required_effects || planNode?.effects || []).length > 0 ? html`
                <div className="ev-row">
                  <span className="ev-key">effects</span>
                  <span className="ev-val">
                    ${(projNode?.required_effects || planNode?.effects || []).join(', ')}
                  </span>
                </div>` : null}
            </div>

            ${isoKey ? html`
              <div className="ev-section">
                <div className="ev-section-title">Isolation — ${isoKey}</div>
                ${capIso ? html`
                  <div className="ev-row">
                    <span className="ev-key">deliverable here</span>
                    <span className=${'ev-val ' + (capIso.deliverable_here ? 'ok' : 'err')}>
                      ${capIso.deliverable_here ? 'yes' : 'no'}
                    </span>
                  </div>
                  ${capIso.reason_if_not ? html`
                    <div className="ev-row">
                      <span className="ev-key">why not</span>
                      <span className="ev-val warn">${capIso.reason_if_not}</span>
                    </div>` : null}
                  <div className="ev-row">
                    <span className="ev-key">controls</span>
                    <span className="ev-val">
                      ${capIso.controls_enforced_here?.length
                        ? capIso.controls_enforced_here.join(', ')
                        : html`<span class="warn">none enforced</span>`}
                    </span>
                  </div>` : html`
                  <div className="ev-row">
                    <span className="ev-key">status</span>
                    <span className="ev-val warn">capability data not loaded</span>
                  </div>`}
              </div>` : null}

            ${projNode && (projNode.spend_tokens !== undefined) ? html`
              <div className="ev-section">
                <div className="ev-section-title">
                  Spend
                  ${!projNode.spend_complete ? html`
                    <span className="tag warn" style=${{ marginLeft: '4px' }}>partial</span>` : null}
                </div>
                <div className="ev-row">
                  <span className="ev-key">tokens</span>
                  <span className="ev-val">
                    ${(projNode.spend_tokens || 0).toLocaleString()}
                  </span>
                </div>
                <div className="ev-row">
                  <span className="ev-key">cost</span>
                  <span className="ev-val">
                    $${((projNode.spend_cost_microunits || 0) / 1_000_000).toFixed(6)}
                  </span>
                </div>
              </div>` : null}

            ${projNode?.artifact_digests?.length > 0 ? html`
              <div className="ev-section">
                <div className="ev-section-title">Artifacts (${projNode.artifact_digests.length})</div>
                ${projNode.artifact_digests.slice(0, 5).map((d, i) => html`
                  <div className="ev-row" key=${i}>
                    <span className="ev-key">${i + 1}</span>
                    <span className="ev-val">${d.slice(0, 18)}…</span>
                  </div>`)}
              </div>` : null}

            ${needsApproval && selectedRun ? html`
              <div className="ev-section" style=${{
                borderColor: 'rgba(157,112,255,.35)',
                background: 'var(--st-awaiting-bg)',
              }}>
                <div className="ev-section-title" style=${{ color: 'var(--st-awaiting)' }}>
                  Approval required
                </div>
                <div className="approve-panel">
                  ${approvePreview ? html`
                    <div className="approve-preview">${approvePreview.would}</div>
                    ${approvePreview.hint ? html`
                      <div className="approve-hint">${approvePreview.hint}</div>` : null}
                    <div className="approve-btns">
                      <button className="btn btn-ok"
                        onClick=${() => handleApprove(approvePreview.decision, true)}
                        disabled=${approving}>
                        ${approving ? html`<span className="spinner"/>` : 'Confirm'}
                      </button>
                      <button className="btn btn-sm"
                        onClick=${() => setApprovePreview(null)}>
                        Back
                      </button>
                    </div>` : html`
                    <div className="approve-btns">
                      <button className="btn btn-ok"
                        onClick=${() => handleApprove('approved', false)}
                        disabled=${approving}>
                        ${approving ? html`<span className="spinner"/>` : 'Approve'}
                      </button>
                      <button className="btn btn-danger"
                        onClick=${() => handleApprove('rejected', false)}
                        disabled=${approving}>
                        Reject
                      </button>
                    </div>`}
                </div>
              </div>` : null}

            ${nodeFields?.length > 0 ? html`
              <div className="ev-section">
                <div className="ev-section-title">Configuration — ${nodeKind}</div>
                <${FormFields}
                  fields=${nodeFields}
                  values=${formVals}
                  onChange=${(k, v) => setFormVals(p => ({ ...p, [k]: v }))}/>
              </div>` : null}

          </div>`}

        ${/* Handoff — always visible, key path back to the agent's orchestrator */ html`
          <div className="handoff-section">
            <div className="handoff-title">
              <${IcoLink}/>
              Continue in your agent
            </div>
            <div className="handoff-desc">
              To change graph structure, run this command in your orchestrator
              (Claude Code, Codex, etc.).
            </div>

            <div className="handoff-btns">
              <button className="btn btn-sm" onClick=${onHandoff}
                disabled=${!graphName || handoffLoading}>
                ${handoffLoading
                  ? html`<span className="spinner"/>`
                  : handoffResult ? 'Refresh' : 'Get command'}
              </button>
              ${handoffResult ? html`
                <button className="btn btn-sm" onClick=${copyHandoff}>
                  ${copied ? 'Copied' : 'Copy'}
                </button>` : null}
              ${!graphName ? html`
                <span style=${{ fontSize: 'var(--t-xs)', color: 'var(--ink-3)' }}>
                  enter a graph name first
                </span>` : null}
            </div>

            ${handoffResult ? html`
              <div className="handoff-code">${handoffResult.command}</div>
              ${handoffResult.mcp_tool ? html`
                <div className="handoff-code">${handoffResult.mcp_tool}</div>` : null}` : null}

            ${handoffError ? html`
              <div className="handoff-error">${handoffError}</div>` : null}
          </div>`}
      </div>
    </div>`;
}
