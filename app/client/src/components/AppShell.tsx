import { Link, Outlet, useLocation } from 'react-router';
import { Boxes, ShieldCheck } from 'lucide-react';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from '@databricks/appkit-ui/react';
import { NAV_GROUPS, titleForPath } from './nav';
import { useCatalog } from '../context/CatalogProvider';
import { RunPill } from './RunPill';
import { RunFailureBanner } from './RunFailureBanner';
import { PageIntro } from './PageIntro';
import { RunSuccessBanner } from './RunSuccessBanner';
import { ReReadButton } from './ReReadButton';
import { GenieDock } from './GenieDock';
import { TourProvider } from '../tours/TourProvider';
import { TakeATourButton, TourAutoStart } from '../tours/TourTriggers';

/**
 * AppShell — the persistent chrome the design specifies: a navy control rail (appkit
 * Sidebar, themed via the --sidebar-* bridge in index.css), a top header carrying the
 * current route's title, and the routed page in the inset. Every route renders inside it.
 */
export function AppShell() {
  const { pathname } = useLocation();
  const title = titleForPath(pathname);

  const isActive = (path: string) =>
    pathname === path || (path === '/workstreams' && pathname.startsWith('/workstreams/'));

  return (
    <TourProvider>
      <SidebarProvider>
        <Sidebar collapsible="icon">
          <SidebarHeader className="gap-3 p-3">
            <div className="flex items-center gap-2.5">
              <div className="flex size-8 shrink-0 items-center justify-center rounded-md bg-[var(--brand)] text-[var(--fg-on-dark)]">
                <Boxes className="size-4" aria-hidden />
              </div>
              <div className="flex min-w-0 flex-col leading-tight group-data-[collapsible=icon]:hidden">
                <span className="truncate text-[13px] font-medium">Managed Table Migration</span>
                <span className="text-[9px] tracking-[0.1em] text-[var(--label-on-dark)]">CONTROL PLANE</span>
              </div>
            </div>
            <ConfigPill />
          </SidebarHeader>

          <SidebarContent data-tour="nav">
            {NAV_GROUPS.map((group) => (
              <SidebarGroup key={group.label}>
                <SidebarGroupLabel className="text-[9px] tracking-[0.12em] text-[var(--label-on-dark)]">
                  {group.label}
                </SidebarGroupLabel>
                <SidebarGroupContent>
                  <SidebarMenu>
                    {group.items.map((item) => (
                      <SidebarMenuItem key={item.path}>
                        <SidebarMenuButton asChild isActive={isActive(item.path)} tooltip={item.label}>
                          <Link to={item.path}>
                            <item.icon aria-hidden />
                            <span>{item.label}</span>
                          </Link>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    ))}
                  </SidebarMenu>
                </SidebarGroupContent>
              </SidebarGroup>
            ))}
          </SidebarContent>

          <SidebarFooter>
            <IdentityChip />
          </SidebarFooter>
          <SidebarRail />
        </Sidebar>

        {/* min-w-0: appkit's SidebarInset ships `w-full flex-1` with no min-width, so a flex item's
          default `min-width:auto` lets a wide table's min-content push the inset past the viewport
          and scroll the whole page sideways. min-w-0 lets it shrink to the available width.
          h-svh + overflow-hidden: pin the inset to the viewport so the inner <main> becomes the real
          vertical scroll container. Without a bounded height here, SidebarInset only had `min-h-svh`
          and grew with content, so `main`'s `overflow-y-auto` never engaged and the document body
          scrolled instead — which silently broke `position: sticky` inside pages (the scrollytelling
          graphic on How it works bound to a scroll container that never scrolled). */}
        <SidebarInset className="min-w-0 h-svh overflow-hidden">
          <header className="flex h-14 shrink-0 items-center gap-2 border-b border-border bg-card px-4 md:px-5">
            <SidebarTrigger className="-ml-1" />
            <h1 className="text-sm font-medium text-foreground">{title}</h1>
            {/* Right side: the active-run pill (what's running + deep link) and the RE-READ
              button (instant re-fetch of all data + how stale the manifest is). */}
            <div className="ml-auto flex items-center gap-2" data-tour="run-controls">
              <TakeATourButton />
              <RunPill />
              <ReReadButton />
            </div>
          </header>
          {/* overflow-x-hidden clips at the page boundary (fit-to-screen); wide tables scroll inside
            their own overflow-x-auto wrapper (PagedTable), so nothing here ever scrolls the page.
            min-h-0: with the parent inset now bounded (h-svh), this flex child must be allowed to
            shrink below its content height so `overflow-y-auto` actually scrolls here (the header
            and sidebar stay pinned) instead of letting the body scroll. */}
          <main className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden p-4 md:p-6">
            {/* Terminal job failures surface here so a failed readiness check / convert is visible
              on every route until dismissed (renders nothing when there are none). */}
            <PageIntro />
            <RunFailureBanner />
            <RunSuccessBanner />
            <Outlet />
            <TourAutoStart />
          </main>
        </SidebarInset>
        {/* Genie — a docked panel available on every route, not a nav item (control_plane §3.13).
          Read-only; front-of-house for the already-wired genie() plugin. */}
        <GenieDock />
      </SidebarProvider>
    </TourProvider>
  );
}

/** Sidebar-footer identity slot. Shows the real signed-in user (server-resolved from the
 *  Databricks Apps x-forwarded-* headers via /api/config), falling back to the neutral
 *  "Admin only" label when no identity is present (local dev / non-Apps runtime) — the app
 *  never invents a person. Non-interactive by design: this is context, not a nav target. */
function IdentityChip() {
  const { user } = useCatalog();
  return (
    <div className="flex items-center gap-2.5 px-1 py-1.5 text-[var(--fg-on-dark-2)]">
      <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-[var(--surface-deep-2)]">
        <ShieldCheck className="size-4" aria-hidden />
      </div>
      <div className="flex min-w-0 flex-col leading-tight group-data-[collapsible=icon]:hidden">
        <span className="truncate text-xs font-medium text-[var(--fg-on-dark)]" title={user ?? undefined}>
          {user ?? 'Admin only'}
        </span>
        <span className="text-[10px] text-[var(--muted-foreground-2)]">Migration control plane</span>
      </div>
    </div>
  );
}

/** The "catalog.schema · from config" pill from the design — reinforces that the target
 *  is server-authoritative (single source of truth), never chosen in the UI. */
function ConfigPill() {
  const { catalog, schema } = useCatalog();
  return (
    <div className="flex items-start gap-2 rounded-md bg-[var(--surface-deep-2)] px-2.5 py-2 group-data-[collapsible=icon]:hidden">
      <span className="mt-1 size-1.5 shrink-0 rounded-full bg-[var(--success)]" aria-hidden />
      {/* Show the full catalog.schema — it is the server-authoritative target, so wrap it rather
          than truncate (break-all keeps the long mono identifier inside the narrow rail). */}
      <span
        className="min-w-0 flex-1 break-all font-mono text-[11px] leading-snug text-[var(--fg-on-dark-2)]"
        title={`${catalog}.${schema}`}
      >
        {catalog}.{schema}
      </span>
      <span className="mt-0.5 shrink-0 text-[9px] text-[var(--label-on-dark)]">from config</span>
    </div>
  );
}
