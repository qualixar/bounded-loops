// forms.js — Schema-driven form fields.
// ChoiceSelect: choices with available:false are NEVER hidden — always shown disabled with reason.
// FieldInput:   routes to the right control per field.kind.
// FormFields:   renders a list of fields with labels, hints, and nested support.

import htm from './vendor/htm.module.js';
const html = htm.bind(React.createElement);

// Choices with available:false MUST render disabled with their reason visible.
// They must never be hidden or silently dropped.
export function ChoiceSelect({ field, value, onChange }) {
  const unavail = (field.choices || []).filter(c => c.available === false);
  return html`<div>
    <select className="select" value=${value || ''}
      onChange=${e => onChange(e.target.value)}>
      <option value="">— select —</option>
      ${(field.choices || []).map(c => html`
        <option key=${c.value} value=${c.value} disabled=${c.available === false}>
          ${c.label || c.value}${c.available === false ? ' (unavailable)' : ''}
        </option>`)}
    </select>
    ${unavail.length > 0 ? html`
      <div className="unavail-list">
        ${unavail.map(c => html`
          <div className="unavail-item" key=${c.value}>
            <strong>${c.label || c.value}</strong>: ${c.reason || 'not available on this host'}
          </div>`)}
      </div>` : null}
  </div>`;
}

export function FieldInput({ field, value, onChange, depth = 0 }) {
  if (field.kind === 'enum' || (field.choices && field.choices.length > 0)) {
    return html`<${ChoiceSelect} field=${field} value=${value} onChange=${onChange}/>`;
  }

  if (field.kind === 'boolean') {
    return html`<label className="form-check-label">
      <input type="checkbox" checked=${!!value}
        onChange=${e => onChange(e.target.checked)}/>
      ${value ? 'enabled' : 'disabled'}
    </label>`;
  }

  if (field.kind === 'integer' || field.kind === 'number') {
    return html`<input className="input" type="number" value=${value ?? ''}
      min=${field.minimum ?? undefined} max=${field.maximum ?? undefined}
      onChange=${e => onChange(Number(e.target.value) || 0)}/>`;
  }

  if (field.kind === 'object' && field.fields && field.fields.length > 0) {
    return html`<div className=${depth > 0 ? 'form-nested' : ''}>
      <${FormFields} fields=${field.fields} values=${value || {}}
        onChange=${(k, v) => onChange({ ...(value || {}), [k]: v })}
        depth=${depth + 1}/>
    </div>`;
  }

  if (field.kind === 'text' || (field.max_length && field.max_length > 200)) {
    return html`<textarea className="textarea"
      value=${value ?? ''} placeholder=${field.description || ''}
      onChange=${e => onChange(e.target.value)}/>`;
  }

  return html`<input className="input" type="text" value=${value ?? ''}
    placeholder=${field.description || ''}
    onChange=${e => onChange(e.target.value)}/>`;
}

export function FormFields({ fields, values, onChange, depth = 0 }) {
  if (!fields || !fields.length) return null;
  return html`<div className="form-fields">
    ${fields.map(f => html`
      <div className="form-field" key=${f.name}>
        <div className="form-field-label">
          <span>${f.label || f.name}</span>
          ${f.required ? html`<span className="form-field-req" aria-label="required">*</span>` : null}
        </div>
        ${f.description ? html`
          <div className="form-field-hint">${f.description}</div>` : null}
        <${FieldInput}
          field=${f}
          value=${values?.[f.name]}
          depth=${depth}
          onChange=${v => onChange(f.name, v)}/>
      </div>`)}
  </div>`;
}
