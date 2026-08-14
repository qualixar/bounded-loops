// dag.js — DAG layout engine + interactive graph SVG component.
// No build step. Runs as an ES module in the browser.
// CSS custom properties do NOT work in SVG presentation attrs — use hex constants below.

import htm from './vendor/htm.module.js';
const html = htm.bind(React.createElement);
const { useMemo } = React;

// Node geometry constants (px)
const NW = 178;   // node width
const NH = 64;    // node height
const CG = 88;    // column gap
const RG = 16;    // row gap

// ── State → hex colour (SVG presentation attrs only; CSS vars don't work here) ──
const STATE_HEX = {
  PENDING:          '#495165',
  READY:            '#5b8bff',
  WORKING:          '#e0a44a',
  CHECKING:         '#9d70ff',
  PASSED:           '#2dd4a0',
  SUCCEEDED:        '#2dd4a0',  // ONLY this is success
  FAILED:           '#f05b7f',
  BLOCKED:          '#f02f60',
  HALTED:           '#f0944d',
  CANCELLED:        '#495165',
  EXPIRED:          '#495165',
  RUNNING:          '#4f7bff',
  SKIPPED:          '#2a3147',
  AWAITING_APPROVAL:'#9d70ff',
};
export const stateHex = s => STATE_HEX[s] || '#495165';

// ── layoutDAG ──────────────────────────────────────────────────────────────────
// Accepts optional server-provided `levels` (array of arrays of node IDs).
// Falls back to BFS topological layering when levels is absent or empty.
export function layoutDAG(nodes, edges, levels) {
  if (!nodes || !nodes.length) return { pos: {}, W: 240, H: 100 };

  const pos = {};
  const nid = n => n.node_id ?? n.id;

  if (levels && levels.length > 0) {
    levels.forEach((lvl, col) =>
      lvl.forEach((id, row) => { pos[id] = { x: col * (NW + CG), y: row * (NH + RG) }; })
    );
    // Place any unlayered nodes after the last column
    let extra = 0;
    nodes.forEach(n => {
      if (!pos[nid(n)]) { pos[nid(n)] = { x: levels.length * (NW + CG), y: extra++ * (NH + RG) }; }
    });
  } else {
    // BFS topological depth assignment
    const adjIn  = {};
    const adjOut = {};
    nodes.forEach(n => { adjIn[nid(n)] = []; adjOut[nid(n)] = []; });
    (edges || []).forEach(e => {
      const f = Array.isArray(e) ? e[0] : (e.from_node ?? e.from);
      const t = Array.isArray(e) ? e[1] : (e.to_node   ?? e.to);
      if (adjOut[f]) adjOut[f].push(t);
      if (adjIn[t])  adjIn[t].push(f);
    });
    const depth = {};
    const queue = nodes.filter(n => !adjIn[nid(n)].length);
    queue.forEach(n => { depth[nid(n)] = 0; });
    for (let i = 0; i < queue.length; i++) {
      const id = nid(queue[i]), d = depth[id] + 1;
      (adjOut[id] || []).forEach(tid => {
        if (depth[tid] === undefined || depth[tid] < d) depth[tid] = d;
        if (!queue.find(x => nid(x) === tid)) {
          const tn = nodes.find(x => nid(x) === tid);
          if (tn) queue.push(tn);
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
      ids.forEach((id, r) => { pos[id] = { x: parseInt(c) * (NW + CG), y: r * (NH + RG) }; })
    );
  }

  const xs = Object.values(pos).map(p => p.x);
  const ys = Object.values(pos).map(p => p.y);
  return {
    pos,
    W: Math.max(NW + 32, Math.max(...xs) + NW + 24),
    H: Math.max(NH + 32, Math.max(...ys) + NH + 24),
  };
}

// ── GraphSVG ───────────────────────────────────────────────────────────────────
// nodeStates: { [nodeId]: stateString } — from live projection overlay
// selectedId: string | null — current selected node
// onSelect: (nodeId) => void
export function GraphSVG({ nodes, edges, levels, nodeStates, selectedId, onSelect }) {
  const { pos, W, H } = useMemo(
    () => layoutDAG(nodes, edges, levels),
    [nodes, edges, levels]
  );

  if (!nodes || !nodes.length) return null;

  const nid = n => n.node_id ?? n.id;
  const edgeList = (edges || []).map(e => ({
    f:    Array.isArray(e) ? e[0] : (e.from_node ?? e.from),
    t:    Array.isArray(e) ? e[1] : (e.to_node   ?? e.to),
    when: Array.isArray(e) ? null  : (e.when ?? null),
  }));

  return html`
    <div className="dag-wrap" role="figure"
         aria-label=${`Workflow graph — ${nodes.length} nodes`}>
      <svg className="dag-svg" width=${W} height=${H} viewBox=${'0 0 ' + W + ' ' + H}
           xmlns="http://www.w3.org/2000/svg">
        <defs>
          <marker id="arr"      markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
            <path d="M0,1 L6,3.5 L0,6 Z" fill="#2c3856"/>
          </marker>
          <marker id="arr-ok"   markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
            <path d="M0,1 L6,3.5 L0,6 Z" fill="#2dd4a0"/>
          </marker>
          <marker id="arr-fail" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
            <path d="M0,1 L6,3.5 L0,6 Z" fill="#f05b7f"/>
          </marker>
        </defs>

        ${edgeList.map(({ f, t, when }) => {
          const fp = pos[f], tp = pos[t];
          if (!fp || !tp) return null;
          const x1 = fp.x + NW, y1 = fp.y + NH / 2;
          const x2 = tp.x,      y2 = tp.y + NH / 2;
          const cx = (x1 + x2) / 2;
          const ts    = nodeStates?.[t];
          const isOk  = ts === 'SUCCEEDED' || ts === 'PASSED';
          const isBad = ts === 'FAILED' || ts === 'BLOCKED';
          const stroke = isOk ? '#2dd4a0' : isBad ? '#f05b7f' : '#2c3856';
          const marker = isOk ? 'url(#arr-ok)' : isBad ? 'url(#arr-fail)' : 'url(#arr)';
          return html`<g key=${f + '>' + t}>
            <path
              d=${'M' + x1 + ',' + y1 + ' C' + cx + ',' + y1 + ' ' + cx + ',' + y2 + ' ' + x2 + ',' + y2}
              fill="none" stroke=${stroke} strokeWidth="1.5" markerEnd=${marker} opacity="0.65"/>
            ${when && when !== 'always' ? html`<text
              x=${cx} y=${(Math.min(y1, y2) - 5)}
              textAnchor="middle" fontSize="9"
              fontFamily="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
              fill="#495165">${when}</text>` : null}
          </g>`;
        })}

        ${nodes.map(n => {
          const id     = nid(n), p = pos[id];
          if (!p) return null;
          const state   = nodeStates?.[id] || n.state || 'PENDING';
          const color   = stateHex(state);
          const isSel   = id === selectedId;
          const isLive  = state === 'RUNNING'  || state === 'WORKING' || state === 'CHECKING';
          const isAwait = state === 'AWAITING_APPROVAL';
          const isSkip  = state === 'SKIPPED';
          const border  = isSel   ? '#4f7bff'
                        : isAwait ? '#9d70ff'
                        : isLive  ? color
                        : '#1f2840';
          const bw      = isSel ? 2 : (isLive || isAwait) ? 1.5 : 1;

          return html`<g key=${id}
            role="button"
            aria-label=${'Node ' + id + ': ' + (n.kind || 'node') + ', state ' + state}
            aria-pressed=${isSel}
            tabIndex="0"
            style=${{ cursor: 'pointer', opacity: isSkip ? 0.36 : 1 }}
            onClick=${() => onSelect(isSel ? null : id)}
            onKeyDown=${e => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onSelect(isSel ? null : id);
              }
            }}>

            ${isLive ? html`<rect
              x=${p.x - 1} y=${p.y - 1} width=${NW + 2} height=${NH + 2} rx="6"
              fill="none" stroke=${color} strokeWidth="1" opacity="0.32"
              strokeDasharray="4 3">
              <animate attributeName="stroke-dashoffset"
                from="0" to="14" dur="0.9s" repeatCount="indefinite"/>
            </rect>` : null}

            <rect x=${p.x} y=${p.y} width=${NW} height=${NH} rx="5"
              fill="#141820" stroke=${border} strokeWidth=${bw}/>

            ${/* left state strip */ ''}
            <rect x=${p.x} y=${p.y} width="4" height=${NH} rx="2" fill=${color}/>

            ${/* node id label */ ''}
            <text x=${p.x + 12} y=${p.y + 22}
              fontSize="12" fontWeight="600"
              fontFamily="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
              fill=${isSel ? '#4f7bff' : '#dde1ea'}>
              ${id.length > 19 ? id.slice(0, 17) + '…' : id}
            </text>

            ${/* node kind */ ''}
            <text x=${p.x + 12} y=${p.y + 39}
              fontSize="10" fontFamily="-apple-system,BlinkMacSystemFont,system-ui,sans-serif"
              fill="#495165">${n.kind || ''}</text>

            ${/* approval indicator — pulsing circle */ ''}
            ${isAwait ? html`<g>
              <circle cx=${p.x + NW - 12} cy=${p.y + 14} r="6" fill="#9d70ff" opacity="0.15"/>
              <circle cx=${p.x + NW - 12} cy=${p.y + 14} r="4.5" fill="#9d70ff"/>
              <text x=${p.x + NW - 12} y=${p.y + 18}
                textAnchor="middle" fontSize="7.5" fontWeight="700" fill="#fff">!</text>
            </g>` : null}

            ${/* state label bottom-right */ ''}
            ${state && state !== 'PENDING' ? html`<text
              x=${p.x + NW - 8} y=${p.y + NH - 9}
              textAnchor="end" fontSize="8"
              fontFamily="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
              fill=${color}>${state}</text>` : null}
          </g>`;
        })}
      </svg>
    </div>`;
}
