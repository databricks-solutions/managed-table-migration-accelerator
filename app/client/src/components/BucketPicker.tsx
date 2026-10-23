import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@databricks/appkit-ui/react';
import { bucketOptions, type BucketPlanRowLike } from '../lib/bucketOptions.pure';

/** Sentinel for the clear option — Radix Select forbids an empty-string item value. */
const ANY = '__any__';

/**
 * BucketPicker — pick from the fixed bucket vocabulary instead of typing it. Emits the bare
 * bucket string (or '' for "any bucket", which drops the bucket selector). Annotated from the
 * current plan: each option shows its wave count, and a bucket with no waves in a loaded plan
 * is disabled so the operator can't scope a convert to an empty bucket. Controlled by value.
 */
export function BucketPicker({
  value,
  onSelect,
  planRows = [],
  id,
  placeholder = 'Any bucket',
  disabled = false,
  disabledHint,
}: {
  /** Currently-selected bucket (controlled). Empty string means no bucket scope. */
  value: string;
  onSelect: (bucket: string) => void;
  /** wave_plan_status rows, used to annotate + disable buckets. Empty = plan not loaded yet. */
  planRows?: BucketPlanRowLike[];
  id?: string;
  placeholder?: string;
  /** Force-disable the whole picker (e.g. a wave is chosen); `disabledHint` is the tooltip. */
  disabled?: boolean;
  disabledHint?: string;
}) {
  const options = bucketOptions(planRows);

  return (
    <Select
      value={value || undefined}
      onValueChange={(v) => onSelect(v === ANY ? '' : v)}
      disabled={disabled}
    >
      <SelectTrigger
        id={id}
        className="w-full"
        title={disabled && disabledHint ? disabledHint : undefined}
      >
        <SelectValue placeholder={disabled && disabledHint ? disabledHint : placeholder} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ANY}>Any bucket</SelectItem>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value} disabled={o.disabled}>
            <div className="flex flex-col">
              <span className="font-mono text-xs">
                {o.value}
                {o.disabled ? ' · not in plan' : ` · ${o.waveCount} wave${o.waveCount === 1 ? '' : 's'}`}
              </span>
              <span className="text-[11px] text-muted-foreground">{o.hint}</span>
            </div>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
