import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import {
  Button,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@databricks/appkit-ui/react';
import { ChevronDown, ChevronLeft, ChevronRight } from 'lucide-react';
import { CopyIconButton } from './CopyableName';

export interface Column<T> {
  /** Stable key for the column (also used as React key). */
  key: string;
  header: ReactNode;
  /** Cell renderer. Receives the row. */
  render: (row: T) => ReactNode;
  /** Optional className on the <td>/<th> (e.g. alignment, width, mono). */
  className?: string;
  /** Opt-in: the identifier a copy button next to the cell copies. Null/blank renders no button. */
  copyValue?: (row: T) => string | null | undefined;
}

/**
 * PagedTable — the estate is designed for ~12,800 tables and built against ~300, so EVERY
 * grid caps rows and paginates now rather than rendering the whole result. Pagination is
 * client-side over the already-fetched rows; a header line reports the true total so a
 * capped view never reads as "that's all there is". Built on the Table primitives (not the
 * DataTable component, which fetches by queryKey and bypasses useMigrationQuery).
 *
 * OPT-IN features (all default off, so a consumer that sets none renders unchanged):
 * - `renderExpanded`: a per-row detail row that opens on a leading chevron toggle. Used to
 *   move wide free text (e.g. an action's notes) out of the grid without losing it.
 * - `renderCard`: a stacked label:value card shown BELOW `md`, with the table hidden there —
 *   so a wide grid becomes scannable on narrow screens instead of a sideways scroll.
 * - `Column.copyValue`: a copy button after the cell content for an identifier column (the
 *   cell's own `render` output, truncation, and `title` are untouched).
 * - scroll-shadow: always on. Left/right gradient overlays make any residual horizontal
 *   scroll discoverable (the nested `overflow-x-auto` was previously near-invisible).
 */
export function PagedTable<T>({
  rows,
  columns,
  pageSize = 25,
  getRowKey,
  caption,
  renderExpanded,
  renderCard,
}: {
  rows: T[];
  columns: Column<T>[];
  pageSize?: number;
  /** Stable key per row; falls back to the row index. */
  getRowKey?: (row: T, index: number) => string;
  /** Optional line above the table, e.g. "933 blocked tables". */
  caption?: ReactNode;
  /** Opt-in: render a per-row detail row toggled by a leading chevron. */
  renderExpanded?: (row: T) => ReactNode;
  /** Opt-in: render a stacked card per row, shown below the `md` breakpoint. */
  renderCard?: (row: T) => ReactNode;
}) {
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const clamped = Math.min(page, pageCount - 1);
  const start = clamped * pageSize;
  const visible = rows.slice(start, start + pageSize);

  const rowKey = useCallback(
    (row: T, index: number) => (getRowKey ? getRowKey(row, index) : String(index)),
    [getRowKey],
  );

  const toggle = useCallback((key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const colSpanTotal = columns.length + (renderExpanded ? 1 : 0);

  const tableBlock = (
    <ScrollShadow>
      <Table>
        <TableHeader>
          <TableRow>
            {renderExpanded ? <TableHead className="w-8" aria-hidden /> : null}
            {columns.map((c) => (
              <TableHead key={c.key} className={c.className}>
                {c.header}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {visible.map((row, i) => {
            const key = rowKey(row, start + i);
            const isOpen = expanded.has(key);
            return (
              <Fragment key={key}>
                <TableRow>
                  {renderExpanded ? (
                    <TableCell className="w-8 align-top">
                      <button
                        type="button"
                        aria-expanded={isOpen}
                        aria-label={isOpen ? 'Collapse row' : 'Expand row'}
                        onClick={() => toggle(key)}
                        className="flex size-6 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                      >
                        {isOpen ? (
                          <ChevronDown className="size-4" aria-hidden />
                        ) : (
                          <ChevronRight className="size-4" aria-hidden />
                        )}
                      </button>
                    </TableCell>
                  ) : null}
                  {columns.map((c) => {
                    const copy = c.copyValue?.(row)?.trim();
                    return (
                      <TableCell key={c.key} className={c.className}>
                        {copy ? (
                          <span className="inline-flex min-w-0 max-w-full items-center gap-1">
                            {c.render(row)}
                            <CopyIconButton value={copy} />
                          </span>
                        ) : (
                          c.render(row)
                        )}
                      </TableCell>
                    );
                  })}
                </TableRow>
                {renderExpanded && isOpen ? (
                  <TableRow className="hover:bg-transparent">
                    <TableCell colSpan={colSpanTotal} className="bg-muted/30">
                      {renderExpanded(row)}
                    </TableCell>
                  </TableRow>
                ) : null}
              </Fragment>
            );
          })}
        </TableBody>
      </Table>
    </ScrollShadow>
  );

  return (
    <div className="flex flex-col gap-3">
      {caption ? <div className="text-sm text-muted-foreground">{caption}</div> : null}

      {renderCard ? (
        <>
          <div className="hidden md:block">{tableBlock}</div>
          <div className="flex flex-col gap-3 md:hidden">
            {visible.map((row, i) => (
              <div key={rowKey(row, start + i)}>{renderCard(row)}</div>
            ))}
          </div>
        </>
      ) : (
        tableBlock
      )}

      {rows.length > pageSize ? (
        <div className="flex items-center justify-between gap-3 text-sm text-muted-foreground">
          <span>
            Showing {start + 1}–{Math.min(start + pageSize, rows.length)} of {rows.length}
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={clamped === 0}
              onClick={() => setPage(clamped - 1)}
            >
              <ChevronLeft className="size-4" aria-hidden />
              Prev
            </Button>
            <span className="tabular-nums">
              {clamped + 1} / {pageCount}
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={clamped >= pageCount - 1}
              onClick={() => setPage(clamped + 1)}
            >
              Next
              <ChevronRight className="size-4" aria-hidden />
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * ScrollShadow — wraps the horizontally-scrollable table region and fades in a left/right
 * edge gradient whenever there is off-screen content in that direction, so the (otherwise
 * near-invisible) nested scroll is discoverable. Pure presentation; no layout shift.
 */
function ScrollShadow({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const maxScroll = el.scrollWidth - el.clientWidth;
    setEdges({
      left: el.scrollLeft > 1,
      right: maxScroll > 1 && el.scrollLeft < maxScroll - 1,
    });
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    measure();
    el.addEventListener('scroll', measure, { passive: true });
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => {
      el.removeEventListener('scroll', measure);
      ro.disconnect();
    };
  }, [measure]);

  return (
    <div className="relative">
      <div ref={ref} className="overflow-x-auto rounded-lg border border-border">
        {children}
      </div>
      {edges.left ? (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-y-0 left-0 w-6 rounded-l-lg bg-gradient-to-r from-black/10 to-transparent dark:from-black/40"
        />
      ) : null}
      {edges.right ? (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-y-0 right-0 w-6 rounded-r-lg bg-gradient-to-l from-black/10 to-transparent dark:from-black/40"
        />
      ) : null}
    </div>
  );
}
