import { useState } from 'react';
import {
  Badge,
  Button,
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@databricks/appkit-ui/react';
import { Check, ChevronsUpDown } from 'lucide-react';
import { useMigrationQuery } from '../hooks/useMigrationQuery';
import { groupWavesByBucket, waveOptionsFromRows, type WaveOption } from '../lib/waveOptions.pure';

export type { WaveOption } from '../lib/waveOptions.pure';

/**
 * WavePicker — a searchable combobox over wave_plan_status, grouped by bucket. Type any token
 * the operator already knows — wave label, wave id, bucket, plan state, or the catalog.schema —
 * and the list filters to matching waves (Command matches against each item's `keywords`). The
 * app passes the selected wave_id VERBATIM (whatever format the planner uses — composite or
 * bare), so there's no paste-the-id-exactly requirement and no typo on the destructive jobs.
 * onSelect returns the whole row, so callers get plan_run_id alongside wave_id (correlated —
 * never a mismatched pair). Shows a disabled trigger with a reason while loading / on error /
 * empty. Finished waves (DONE/ROLLED_BACK) stay pickable — rollback needs them.
 */
export function WavePicker({
  value,
  onSelect,
  id,
  placeholder = 'Select a wave…',
  disabled: forcedDisabled = false,
  disabledHint,
}: {
  /** Currently-selected wave_id (controlled). */
  value: string;
  onSelect: (option: WaveOption | null) => void;
  id?: string;
  placeholder?: string;
  /** Force-disable (e.g. the other scope is chosen); shows `disabledHint` as the reason. */
  disabled?: boolean;
  disabledHint?: string;
}) {
  const [open, setOpen] = useState(false);
  const q = useMigrationQuery('wave_plan_status', {});
  const options = waveOptionsFromRows(q.data ?? []);
  const groups = groupWavesByBucket(options);

  const disabled = forcedDisabled || q.loading || q.error !== null || options.length === 0;
  const shownPlaceholder = q.loading
    ? 'Loading waves…'
    : q.error !== null
      ? 'Couldn’t load waves'
      : options.length === 0
        ? 'No waves in the plan'
        : forcedDisabled && disabledHint
          ? disabledHint
          : placeholder;

  const selected = options.find((o) => o.wave_id === value) ?? null;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          disabled={disabled}
          title={forcedDisabled && disabledHint ? disabledHint : undefined}
          className="w-full justify-between font-normal"
        >
          <span className={selected ? undefined : 'text-muted-foreground'}>
            {selected
              ? `${selected.wave_label} · ${selected.bucket} · ${selected.wave_state}`
              : shownPlaceholder}
          </span>
          <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
        <Command>
          <CommandInput placeholder="Search by label, id, bucket, state, schema…" />
          <CommandList>
            <CommandEmpty>No matching wave.</CommandEmpty>
            {groups.map((group) => (
              <CommandGroup key={group.bucket} heading={group.bucket}>
                {group.options.map((o) => (
                  <CommandItem
                    key={o.wave_id}
                    value={o.wave_id}
                    keywords={o.keywords}
                    onSelect={() => {
                      onSelect(o.wave_id === value ? null : o);
                      setOpen(false);
                    }}
                  >
                    <Check
                      className={`mr-2 size-4 shrink-0 ${o.wave_id === value ? 'opacity-100' : 'opacity-0'}`}
                    />
                    <div className="flex min-w-0 flex-1 flex-col">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm">{o.wave_label}</span>
                        <Badge variant="outline" className="shrink-0 text-[10px]">
                          {o.wave_state}
                        </Badge>
                        <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                          {o.total_tables} table{o.total_tables === 1 ? '' : 's'}
                        </span>
                      </div>
                      <span className="truncate font-mono text-[10px] text-muted-foreground">
                        {o.wave_id}
                        {o.group_key ? ` · ${o.group_key}` : ''}
                      </span>
                    </div>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
