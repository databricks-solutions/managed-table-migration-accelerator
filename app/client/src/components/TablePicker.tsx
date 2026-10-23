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
import type { BadgeTone, TableOption } from './maintenance.pure';

/** Tone → color classes, reusing the app's semantic CSS vars (same palette as the run pills). */
const TONE_CLASS: Record<BadgeTone, string> = {
  ready: 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]',
  expiring: 'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]',
  waiting: 'border-[var(--info-border)] bg-[var(--info-bg)] text-[var(--info-foreground)]',
  muted: 'border-border bg-muted text-muted-foreground',
};

/**
 * TablePicker — a searchable combobox over a supplied list of tables. Presentational: it takes
 * options in and reports a selection out, so it stays dumb and reusable for both the cleanup and
 * rollback candidate lists (each mapped by `maintenance.pure`). Mirrors WavePicker: pick, don't
 * type — the exact `full_name` can never be mistyped, and the search matches `keywords`
 * (catalog / schema / table). Shows a disabled trigger with a reason while loading / on error /
 * empty, or when the parent forces it off (e.g. the rollback table↔wave XOR).
 */
export function TablePicker({
  value,
  onSelect,
  options,
  loading,
  error,
  id,
  placeholder = 'Search tables…',
  emptyLabel = 'No tables',
  disabled = false,
  disabledHint,
}: {
  /** Currently-selected full_name (controlled). */
  value: string;
  onSelect: (option: TableOption | null) => void;
  options: TableOption[];
  loading: boolean;
  error: boolean;
  id?: string;
  placeholder?: string;
  /** Trigger label when the list loads fine but is empty. */
  emptyLabel?: string;
  /** Force-disable (e.g. the other scope is chosen); shows `disabledHint` as the reason. */
  disabled?: boolean;
  disabledHint?: string;
}) {
  const [open, setOpen] = useState(false);

  const isEmpty = !loading && !error && options.length === 0;
  const effectiveDisabled = disabled || loading || error || isEmpty;
  const shownPlaceholder = loading
    ? 'Loading tables…'
    : error
      ? 'Couldn’t load tables'
      : isEmpty
        ? emptyLabel
        : disabled && disabledHint
          ? disabledHint
          : placeholder;

  const selected = options.find((o) => o.full_name === value) ?? null;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          disabled={effectiveDisabled}
          title={disabled && disabledHint ? disabledHint : undefined}
          className="w-full justify-between font-normal"
        >
          <span className={selected ? 'truncate font-mono text-sm' : 'truncate text-muted-foreground'}>
            {selected ? selected.full_name : shownPlaceholder}
          </span>
          <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
        <Command>
          <CommandInput placeholder="Search by table, catalog, schema…" />
          <CommandList>
            <CommandEmpty>No matching table.</CommandEmpty>
            <CommandGroup>
              {options.map((o) => (
                <CommandItem
                  key={o.full_name}
                  value={o.full_name}
                  keywords={o.keywords}
                  onSelect={() => {
                    onSelect(o.full_name === value ? null : o);
                    setOpen(false);
                  }}
                >
                  <Check
                    className={`mr-2 size-4 shrink-0 ${o.full_name === value ? 'opacity-100' : 'opacity-0'}`}
                  />
                  <div className="flex min-w-0 flex-1 flex-col">
                    <span className="truncate font-mono text-[12px]">{o.full_name}</span>
                    <span className="truncate text-[11px] text-muted-foreground">{o.secondary}</span>
                  </div>
                  {o.badge ? (
                    <Badge
                      variant="outline"
                      className={`ml-2 shrink-0 font-normal ${TONE_CLASS[o.badge.tone]}`}
                    >
                      {o.badge.label}
                    </Badge>
                  ) : null}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
