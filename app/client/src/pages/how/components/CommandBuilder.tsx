import { useState } from 'react';
import { AlertTriangle, TriangleAlert } from 'lucide-react';
import { cn } from '@databricks/appkit-ui/react';
import {
  PHASE_LABEL,
  PHASE_ORDER,
  PRESETS,
  STAGES,
  TARGETS,
  buildCommand,
  defaultState,
  getStage,
  type BuilderState,
  type Issue,
  type Stage,
  type StageField,
} from '../directPath.pure';
import { CommandBlock } from './CommandBlock';

/**
 * CommandBuilder — the module centerpiece. Pick a stage + fill its selectors/gates and it
 * assembles the exact `databricks bundle run …` command, live-validated against the jobs.md
 * selector rules (via the pure `buildCommand`). It teaches the command; it never runs it.
 */
export function CommandBuilder() {
  const [state, setState] = useState<BuilderState>(() => ({
    bundleKey: 'job_04_convert',
    target: 'dev',
    profile: '',
    params: { target_schema: 'main.sales', confirm_compaction_paused: 'true' },
  }));

  const stage = getStage(state.bundleKey) as Stage;
  const { command, issues } = buildCommand(state);
  const errors = issues.filter((i) => i.level === 'error');
  const warnings = issues.filter((i) => i.level === 'warning');

  const selectStage = (bundleKey: string) => setState(defaultState(bundleKey));
  const setParam = (key: string, value: string) =>
    setState((s) => ({ ...s, params: { ...s.params, [key]: value } }));

  return (
    <div className="flex flex-col gap-4">
      {/* Presets */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Start from</span>
        {PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => setState(p.state)}
            title={p.description}
            className="how-press rounded-lg border border-border bg-background px-3 py-1.5 text-[11px] font-medium text-foreground hover:border-[var(--brand)]"
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Stage picker, grouped by lifecycle phase */}
      <div className="flex flex-col gap-2 rounded-lg border border-border bg-background p-3">
        <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Stage</span>
        <div className="flex flex-col gap-2">
          {PHASE_ORDER.map((phase) => {
            const inPhase = STAGES.filter((s) => s.phase === phase);
            if (inPhase.length === 0) return null;
            return (
              <div key={phase} className="flex flex-wrap items-center gap-1.5">
                <span className="w-20 shrink-0 text-[10px] font-medium text-muted-foreground">{PHASE_LABEL[phase]}</span>
                {inPhase.map((s) => {
                  const on = s.bundleKey === state.bundleKey;
                  return (
                    <button
                      key={s.bundleKey}
                      type="button"
                      onClick={() => selectStage(s.bundleKey)}
                      aria-pressed={on}
                      className={cn(
                        'how-press flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-medium',
                        on
                          ? 'border-[var(--brand)] bg-[var(--brand)]/10 text-[var(--brand)]'
                          : 'border-border bg-card text-muted-foreground hover:text-foreground',
                      )}
                    >
                      {s.label}
                      {s.mutates ? <span className="size-1.5 rounded-full bg-[var(--brand)]" aria-hidden title="Can mutate" /> : null}
                    </button>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>

      {/* Selected stage: purpose + runtime/note */}
      <div className="rounded-lg border border-border bg-card p-3">
        <div className="flex flex-wrap items-center gap-2">
          <code className="font-mono text-[12px] font-semibold text-foreground">{stage.bundleKey}</code>
          {stage.mutates ? (
            <span className="rounded-full bg-[var(--brand)]/10 px-2 py-0.5 text-[10px] font-semibold text-[var(--brand)]">Mutates</span>
          ) : (
            <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-semibold text-muted-foreground">Read-only</span>
          )}
          {stage.runtime ? <span className="text-[10px] text-muted-foreground">{stage.runtime}</span> : null}
        </div>
        <p className="mt-1 text-[12px] text-muted-foreground">{stage.purpose}</p>
        {stage.note ? <p className="mt-1 text-[11px] text-[var(--info-foreground)]">{stage.note}</p> : null}
      </div>

      {/* Target + profile */}
      <div className="flex flex-wrap items-end gap-4">
        <div className="flex items-center gap-2">
          <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Target</span>
          <div className="flex items-center gap-0.5 rounded-lg border border-border bg-background p-0.5" role="group" aria-label="Target">
            {TARGETS.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setState((s) => ({ ...s, target: t }))}
                aria-pressed={state.target === t}
                className={cn(
                  'how-press rounded-md px-2.5 py-1 text-[11px] font-medium',
                  state.target === t ? 'bg-[var(--brand)] text-[var(--fg-on-dark)]' : 'text-muted-foreground',
                )}
              >
                {t}
              </button>
            ))}
          </div>
        </div>
        <label className="flex flex-col gap-1">
          <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Profile (optional)</span>
          <input
            type="text"
            value={state.profile}
            onChange={(e) => setState((s) => ({ ...s, profile: e.target.value }))}
            placeholder="e.g. my-profile"
            className="w-56 rounded-md border border-border bg-background px-2.5 py-1.5 text-[12px] text-foreground placeholder:text-muted-foreground/60"
          />
        </label>
      </div>

      {/* Dynamic stage fields */}
      {stage.fields.length > 0 ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {stage.fields.map((f) => (
            <Field key={f.key} field={f} value={state.params[f.key] ?? ''} onChange={(v) => setParam(f.key, v)} />
          ))}
        </div>
      ) : (
        <p className="text-[11px] text-muted-foreground">No run-now parameters — this stage takes none.</p>
      )}

      {/* Output */}
      <div className="flex flex-col gap-2">
        <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Command</span>
        <CommandBlock command={command} />
        {issues.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {errors.map((i) => (
              <IssueRow key={i.message} issue={i} />
            ))}
            {warnings.map((i) => (
              <IssueRow key={i.message} issue={i} />
            ))}
          </ul>
        ) : (
          <p className="text-[11px] text-[var(--success-foreground)]">Valid — ready to run in a terminal with the Databricks CLI.</p>
        )}
      </div>
    </div>
  );
}

function Field({ field, value, onChange }: { field: StageField; value: string; onChange: (v: string) => void }) {
  if (field.kind === 'bool') {
    const on = value === 'true';
    return (
      <button
        type="button"
        role="switch"
        aria-checked={on}
        onClick={() => onChange(on ? 'false' : 'true')}
        className={cn(
          'how-press flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-left',
          on ? 'border-[var(--brand)] bg-[var(--brand)]/10' : 'border-border bg-background',
        )}
      >
        <span className="flex flex-col">
          <span className="text-[12px] font-medium text-foreground">{field.label}</span>
          {field.help ? <span className="text-[10px] text-muted-foreground">{field.help}</span> : null}
        </span>
        <span
          className={cn('flex h-4 w-7 items-center rounded-full p-0.5', on ? 'bg-[var(--brand)]' : 'bg-muted-foreground/30')}
          aria-hidden
        >
          <span className={cn('size-3 rounded-full bg-white transition-transform', on && 'translate-x-3')} />
        </span>
      </button>
    );
  }

  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] font-medium text-foreground">{field.label}</span>
      {field.kind === 'enum' ? (
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="rounded-md border border-border bg-background px-2.5 py-1.5 text-[12px] text-foreground"
        >
          {(field.options ?? []).map((o) => (
            <option key={o} value={o}>
              {o === '' ? '(none)' : o}
            </option>
          ))}
        </select>
      ) : (
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          className="rounded-md border border-border bg-background px-2.5 py-1.5 text-[12px] text-foreground placeholder:text-muted-foreground/60"
        />
      )}
      {field.help && field.kind !== 'enum' ? <span className="text-[10px] text-muted-foreground">{field.help}</span> : null}
    </label>
  );
}

function IssueRow({ issue }: { issue: Issue }) {
  const isError = issue.level === 'error';
  const Icon = isError ? TriangleAlert : AlertTriangle;
  return (
    <li
      className={cn(
        'flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-[11px]',
        isError
          ? 'border-[var(--destructive-border)] bg-[var(--destructive-bg)] text-[var(--destructive-foreground)]'
          : 'border-[var(--info-border)] bg-[var(--info-bg)] text-[var(--info-foreground)]',
      )}
    >
      <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      {issue.message}
    </li>
  );
}
