// palette.js — ⌘K command palette.
// Operations only: Run, Approve, Save, Lint, Plan, Handoff, jump to run.
// NOT a chat substitute. No ask route. No chat commands.
// Keyboard: ↑↓ navigate, Enter run, Escape close.

import htm from './vendor/htm.module.js';
const html = htm.bind(React.createElement);
const { useState, useEffect, useRef, useCallback } = React;

// ── Search icon ───────────────────────────────────────────────────────────────
const IcoSearch = () => html`<svg width="14" height="14" viewBox="0 0 16 16" fill="none">
  <circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" strokeWidth="1.4"/>
  <line x1="10" y1="10" x2="14" y2="14" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
</svg>`;

// ── KPalette ──────────────────────────────────────────────────────────────────
// commands: Array<{ id, label, icon, shortcut?, disabled?, action: () => void }>
// onClose: () => void
export function KPalette({ commands, onClose }) {
  const [query,     setQuery]     = useState('');
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef(null);
  const listRef  = useRef(null);

  // Grab focus immediately
  useEffect(() => { inputRef.current?.focus(); }, []);

  // Filter by query (case-insensitive substring match on label)
  const filtered = commands.filter(c =>
    !query || c.label.toLowerCase().includes(query.toLowerCase())
  );

  // Clamp active index when filtered list shrinks
  useEffect(() => {
    setActiveIdx(i => Math.min(i, Math.max(0, filtered.length - 1)));
  }, [filtered.length]);

  // Scroll active item into view
  useEffect(() => {
    const el = listRef.current?.querySelector('[data-active="true"]');
    el?.scrollIntoView({ block: 'nearest' });
  }, [activeIdx]);

  const execute = useCallback(cmd => {
    if (!cmd || cmd.disabled) return;
    onClose();
    cmd.action();
  }, [onClose]);

  function onKey(e) {
    if (e.key === 'Escape')    { e.preventDefault(); onClose(); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); setActiveIdx(i => Math.min(i + 1, filtered.length - 1)); return; }
    if (e.key === 'ArrowUp')   { e.preventDefault(); setActiveIdx(i => Math.max(i - 1, 0)); return; }
    if (e.key === 'Enter')     { e.preventDefault(); execute(filtered[activeIdx]); }
  }

  return html`
    <div className="palette-overlay"
      role="dialog" aria-modal="true" aria-label="Command palette"
      onClick=${onClose}>
      <div className="palette" onClick=${e => e.stopPropagation()} onKeyDown=${onKey}>

        <div className="palette-input-wrap">
          <span className="palette-icon" aria-hidden="true"><${IcoSearch}/></span>
          <input ref=${inputRef} className="palette-input"
            placeholder="Run a command…" value=${query}
            aria-label="Command search" aria-autocomplete="list"
            onInput=${e => { setQuery(e.target.value); setActiveIdx(0); }}/>
        </div>

        <div ref=${listRef} className="palette-list" role="listbox" aria-label="Commands">
          ${filtered.length === 0
            ? html`<div className="palette-empty">No commands match.</div>`
            : filtered.map((cmd, i) => html`
              <div key=${cmd.id}
                className=${'palette-item' + (i === activeIdx ? ' active' : '')}
                role="option" aria-selected=${i === activeIdx}
                aria-disabled=${!!cmd.disabled}
                data-active=${i === activeIdx ? 'true' : 'false'}
                style=${{ opacity: cmd.disabled ? .4 : 1,
                           cursor: cmd.disabled ? 'default' : 'pointer' }}
                onClick=${() => !cmd.disabled && execute(cmd)}>
                <span className="palette-item-icon" aria-hidden="true">${cmd.icon}</span>
                <span className="palette-item-label">${cmd.label}</span>
                ${cmd.shortcut ? html`
                  <span className="palette-item-shortcut">${cmd.shortcut}</span>` : null}
              </div>`)}
        </div>

        <div className="palette-footer" aria-hidden="true">
          <span className="btn-kbd">↑↓</span><span>navigate</span>
          <span className="btn-kbd">↵</span><span>run</span>
          <span className="btn-kbd">Esc</span><span>close</span>
        </div>
      </div>
    </div>`;
}
