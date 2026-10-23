import { useState } from 'react';
import {
  Badge,
  Button,
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@databricks/appkit-ui/react';
import { Check, ChevronsUpDown } from 'lucide-react';
import { useMigrationQuery } from '../hooks/useMigrationQuery';
import { schemaOptions } from '../lib/schemaOptions.pure';

export type { SchemaOption } from '../lib/schemaOptions.pure';

/**
 * SchemaPicker — a searchable combobox over the plan's distinct `group_key` scopes (the exact
 * value `target_schema` matches in the engine). Emits the raw `group_key` string, so the
 * operator picks a real, resolvable scope instead of typing a catalog.schema that may not be in
 * the plan. Searchable by schema / catalog / "pilot". Shows a disabled trigger with a reason
 * while loading / on error / when the plan is empty.
 */
export function SchemaPicker({
  value,
  onSelect,
  id,
  placeholder = 'Select a schema…',
  disabled: forcedDisabled = false,
  disabledHint,
}: {
  /** Currently-selected group_key (controlled). */
  value: string;
  onSelect: (groupKey: string) => void;
  id?: string;
  placeholder?: string;
  /** Force-disable (e.g. a wave is chosen); shows `disabledHint` as the reason. */
  disabled?: boolean;
  disabledHint?: string;
}) {
  const [open, setOpen] = useState(false);
  const q = useMigrationQuery('wave_plan_status', {});
  const options = schemaOptions(q.data ?? []);

  const disabled = forcedDisabled || q.loading || q.error !== null || options.length === 0;
  const shownPlaceholder = q.loading
    ? 'Loading schemas…'
    : q.error !== null
      ? 'Couldn’t load schemas'
      : options.length === 0
        ? 'No schemas in the plan'
        : forcedDisabled && disabledHint
          ? disabledHint
          : placeholder;

  const selected = options.find((o) => o.value === value) ?? null;

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
          className="w-full justify-between font-normal"
        >
          <span className={selected ? undefined : 'text-muted-foreground'}>
            {selected ? selected.label : value || shownPlaceholder}
          </span>
          <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
        <Command>
          <CommandInput placeholder="Search by schema, catalog, pilot…" />
          <CommandList>
            <CommandEmpty>No matching schema.</CommandEmpty>
            {options.map((o) => (
              <CommandItem
                key={o.value}
                value={o.value}
                keywords={o.keywords}
                onSelect={() => {
                  onSelect(o.value === value ? '' : o.value);
                  setOpen(false);
                }}
              >
                <Check
                  className={`mr-2 size-4 shrink-0 ${o.value === value ? 'opacity-100' : 'opacity-0'}`}
                />
                <div className="flex min-w-0 flex-1 flex-col">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm">{o.label}</span>
                    {o.spansManySchemas ? (
                      <Badge variant="outline" className="shrink-0 text-[10px]">
                        {o.schemaCount} schemas
                      </Badge>
                    ) : null}
                    <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                      {o.waveCount} wave{o.waveCount === 1 ? '' : 's'} · {o.tableCount} table
                      {o.tableCount === 1 ? '' : 's'}
                    </span>
                  </div>
                  <span className="truncate font-mono text-[10px] text-muted-foreground">
                    {o.value}
                  </span>
                </div>
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
