// Auto-generated parameter form from ParamSpec[] (scanner, risk config, strategy params).
import { memo, useEffect, useMemo, useState } from "react";
import type { ParamSpec } from "../../lib/types";
import { Switch, Tip } from "./States";

export type ParamValues = Record<string, unknown>;

export function defaultsOf(schema: ParamSpec[]): ParamValues {
  const out: ParamValues = {};
  for (const p of schema) out[p.name] = p.default;
  return out;
}

/** Only the entries of `values` that differ from `base`. */
export function diffParams(values: ParamValues, base: ParamValues): ParamValues {
  const out: ParamValues = {};
  for (const [k, v] of Object.entries(values)) {
    if (JSON.stringify(v) !== JSON.stringify(base[k])) out[k] = v;
  }
  return out;
}

function decimalsFor(step: number): number {
  if (!Number.isFinite(step) || step <= 0) return 4;
  const s = String(step);
  const i = s.indexOf(".");
  return i < 0 ? 0 : Math.min(6, s.length - i - 1);
}

export function NumberInput({
  value,
  onChange,
  min,
  max,
  step,
  integer,
  disabled,
  className = "input input-sm input-num",
  ariaLabel,
  placeholder,
}: {
  value: number | null | undefined;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  integer?: boolean;
  disabled?: boolean;
  className?: string;
  ariaLabel?: string;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState(value === null || value === undefined ? "" : String(value));
  const [focused, setFocused] = useState(false);
  useEffect(() => {
    if (!focused) setDraft(value === null || value === undefined || !Number.isFinite(value) ? "" : String(value));
  }, [value, focused]);
  const commit = (s: string) => {
    let n = Number(s);
    if (s.trim() === "" || !Number.isFinite(n)) return;
    if (integer) n = Math.round(n);
    if (min !== undefined) n = Math.max(min, n);
    if (max !== undefined) n = Math.min(max, n);
    onChange(n);
  };
  return (
    <input
      className={className}
      type="text"
      inputMode={integer ? "numeric" : "decimal"}
      value={draft}
      disabled={disabled}
      aria-label={ariaLabel}
      placeholder={placeholder}
      step={step}
      onFocus={() => setFocused(true)}
      onChange={(e) => {
        setDraft(e.target.value);
        const n = Number(e.target.value);
        if (e.target.value.trim() !== "" && Number.isFinite(n) && (min === undefined || n >= min) && (max === undefined || n <= max)) {
          onChange(integer ? Math.round(n) : n);
        }
      }}
      onBlur={(e) => {
        setFocused(false);
        commit(e.target.value);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit((e.target as HTMLInputElement).value);
      }}
    />
  );
}

interface FieldProps {
  spec: ParamSpec;
  value: unknown;
  changed: boolean;
  disabled?: boolean;
  onChange: (name: string, v: unknown) => void;
}

const ParamField = memo(function ParamField({ spec, value, changed, disabled, onChange }: FieldProps) {
  const id = `param-${spec.name}`;
  const label = (
    <label className="param-label" htmlFor={id}>
      {changed && <span className="changed" title="Changed" />}
      <span className="ellipsis">{spec.label || spec.name}</span>
      {spec.help && <Tip text={spec.help} />}
    </label>
  );

  if (spec.type === "bool") {
    return (
      <div className="param-row">
        {label}
        <div />
        <div style={{ justifySelf: "end" }}>
          <Switch checked={Boolean(value)} disabled={disabled} onChange={(v) => onChange(spec.name, v)} />
        </div>
      </div>
    );
  }

  if (spec.type === "enum") {
    return (
      <div className="param-row">
        {label}
        <select id={id} className="select input-sm" style={{ gridColumn: "2 / 4" }} disabled={disabled} value={String(value ?? "")} onChange={(e) => onChange(spec.name, e.target.value)}>
          {(spec.options ?? []).map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      </div>
    );
  }

  if (spec.type === "str") {
    const v = Array.isArray(value) ? value.join(", ") : String(value ?? "");
    return (
      <div className="param-row">
        {label}
        <input
          id={id}
          className="input input-sm"
          style={{ gridColumn: "2 / 4" }}
          disabled={disabled}
          value={v}
          onChange={(e) => onChange(spec.name, Array.isArray(spec.default) ? e.target.value.split(",").map((s) => s.trim()).filter(Boolean) : e.target.value)}
        />
      </div>
    );
  }

  const integer = spec.type === "int";
  const num = typeof value === "number" ? value : Number(value);
  const hasRange = spec.min !== undefined && spec.max !== undefined && spec.max > spec.min;
  const step = spec.step ?? (integer ? 1 : hasRange ? Number(((spec.max! - spec.min!) / 100).toPrecision(2)) : 0.01);
  const dec = decimalsFor(step);
  return (
    <div className="param-row">
      {label}
      {hasRange ? (
        <input
          className="range"
          type="range"
          min={spec.min}
          max={spec.max}
          step={step}
          disabled={disabled}
          value={Number.isFinite(num) ? num : Number(spec.default)}
          aria-label={`${spec.label} slider`}
          onChange={(e) => {
            const n = Number(e.target.value);
            onChange(spec.name, integer ? Math.round(n) : Number(n.toFixed(dec)));
          }}
        />
      ) : (
        <div className="faint xsmall">{spec.min !== undefined ? `≥ ${spec.min}` : spec.max !== undefined ? `≤ ${spec.max}` : ""}</div>
      )}
      <span id={id}>
        <NumberInput value={Number.isFinite(num) ? num : null} onChange={(n) => onChange(spec.name, n)} min={spec.min} max={spec.max} step={step} integer={integer} disabled={disabled} ariaLabel={spec.label} />
      </span>
    </div>
  );
});

interface Props {
  schema: ParamSpec[];
  values: ParamValues;
  onChange: (name: string, value: unknown) => void;
  /** Values to compare against for the "changed" marker. */
  baseline?: ParamValues;
  disabled?: boolean;
  /** Show group headings (default true when >1 group). */
  grouped?: boolean;
  /** Lay groups out in responsive columns. */
  columns?: boolean;
}

export function ParamForm({ schema, values, onChange, baseline, disabled, grouped, columns }: Props) {
  const groups = useMemo(() => {
    const m = new Map<string, ParamSpec[]>();
    for (const p of schema) {
      const g = p.group || "General";
      const arr = m.get(g) ?? [];
      arr.push(p);
      m.set(g, arr);
    }
    return [...m.entries()];
  }, [schema]);
  const showGroups = grouped ?? groups.length > 1;
  if (!schema.length) return <div className="faint small">No parameters.</div>;
  return (
    <div className={columns ? "param-cols" : undefined}>
      {groups.map(([g, specs]) => (
        <div key={g} className="param-group">
          {showGroups && <div className="param-group-title">{g}</div>}
          {specs.map((s) => (
            <ParamField
              key={s.name}
              spec={s}
              value={values[s.name] ?? s.default}
              changed={baseline ? JSON.stringify(values[s.name]) !== JSON.stringify(baseline[s.name]) : false}
              disabled={disabled}
              onChange={onChange}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
