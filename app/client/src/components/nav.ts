import {
  LayoutDashboard,
  Layers,
  TriangleAlert,
  Waves,
  Wrench,
  History,
  CirclePause,
  Gauge,
  Radar,
  ClipboardList,
  BookOpen,
  Settings,
  type LucideIcon,
} from 'lucide-react';
import { PAGE_COPY } from '../lib/sectionCopy';

/**
 * The navigation model, taken straight from the v4 design's sidebar: four groups
 * (MIGRATE / OBSERVE / LEARN / SETUP) holding the 12 destinations, in the design's order.
 * Icons map the design's Databricks glyphs to their closest lucide equivalents.
 *
 * WorkstreamDetail (/workstreams/:workstream) is a route but NOT a sidebar item — it's
 * reached by drilling into a workstream — so it lives only in ROUTE_TITLES.
 */

export interface NavItem {
  /** URL path (no leading slash duplication — used with react-router <Link>). */
  path: string;
  label: string;
  icon: LucideIcon;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: 'MIGRATE',
    items: [
      { path: '/overview', label: 'Overview', icon: LayoutDashboard },
      { path: '/workstreams', label: 'Workstreams', icon: Layers },
      { path: '/blocked', label: 'Blocked & review', icon: TriangleAlert },
      { path: '/waves', label: 'Waves', icon: Waves },
      { path: '/convert', label: 'Convert (expert)', icon: Wrench },
    ],
  },
  {
    label: 'OBSERVE',
    items: [
      { path: '/runs', label: 'Runs & history', icon: History },
      { path: '/freeze', label: 'Paused & stopped', icon: CirclePause },
      { path: '/coverage', label: 'Coverage', icon: Radar },
      { path: '/dashboard', label: 'Estate dashboard', icon: Gauge },
      { path: '/runbook', label: 'Do by hand', icon: ClipboardList },
    ],
  },
  {
    label: 'LEARN',
    items: [{ path: '/how-it-works', label: 'How it works', icon: BookOpen }],
  },
  {
    label: 'SETUP',
    items: [{ path: '/settings', label: 'Connection & policy', icon: Settings }],
  },
];

/** Header title per path, from the copy module (sidebar labels stay in NAV_GROUPS). Includes
 *  the detail route, which has no sidebar entry. */
export const ROUTE_TITLES: Record<string, string> = Object.fromEntries(
  Object.entries(PAGE_COPY).map(([path, c]) => [path, c.title]),
);

/** Resolve the header title for a pathname, including the workstream + academy detail routes. */
export function titleForPath(pathname: string): string {
  if (pathname.startsWith('/workstreams/')) return ROUTE_TITLES['/workstreams/:workstream'];
  if (pathname.startsWith('/how-it-works/')) return ROUTE_TITLES['/how-it-works'];
  return ROUTE_TITLES[pathname] ?? 'Overview';
}
