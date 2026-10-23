/** Outline-button classes for the dark guided card (shared by FinishStepCta and DeferFinishActions). */
export const OUTLINE_ON_DARK =
  'border-[var(--border-on-dark)] bg-transparent text-[var(--fg-on-dark)] hover:bg-[var(--surface-deep-2)] hover:text-[var(--fg-on-dark)]';

/**
 * The main button of a guided step on the dark card: white fill, navy text, the one thing to press.
 * (The stock `default` variant is navy on the navy card and reads as disabled.) Lava red stays
 * reserved for the convert, which changes tables (`destructive`).
 */
export const PRIMARY_ON_DARK =
  'bg-[var(--fg-on-dark)] text-[var(--surface-deep)] shadow-sm hover:bg-[var(--fg-on-dark-2)] hover:text-[var(--surface-deep)] focus-visible:ring-2 focus-visible:ring-[var(--fg-on-dark)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--surface-deep)]';

/** A quiet text-link style for a minor choice next to the main button (e.g. "Skip … for now"). */
export const LINK_ON_DARK =
  'h-auto border-0 bg-transparent px-0 text-xs font-normal text-[var(--fg-on-dark-2)] underline underline-offset-2 shadow-none hover:bg-transparent hover:text-[var(--fg-on-dark)]';
export const LINK_ON_LIGHT =
  'h-auto border-0 bg-transparent px-0 text-xs font-normal text-muted-foreground underline underline-offset-2 shadow-none hover:bg-transparent hover:text-foreground';

/** The main-button classes for a surface (`undefined` keeps the stock button on light pages). */
export function primaryOn(surface: 'dark' | 'light'): string | undefined {
  return surface === 'dark' ? PRIMARY_ON_DARK : undefined;
}
