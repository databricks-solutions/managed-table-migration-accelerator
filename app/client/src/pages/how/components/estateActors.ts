import type { Actor } from '../estateMap.pure';

/**
 * Actor identity for the bird's-eye map — shared by EstateMap (swimlanes) and Legend (key) so
 * the colour/label mapping lives in exactly one place. Kept out of the component files so both
 * can import it without tripping react-refresh's "only export components" rule, and out of
 * estateMap.pure.ts so that stays pure logic (this carries presentation: Tailwind classes).
 */
export const ACTOR_META: Record<Actor, { label: string; verb: string; dot: string }> = {
  operator: { label: 'Operator', verb: 'approves', dot: 'bg-[var(--info)]' },
  app: { label: 'App', verb: 'reflects & dispatches', dot: 'bg-[var(--brand)]' },
  job: { label: 'Stage jobs', verb: 'enforce', dot: 'bg-[var(--warning)]' },
  control: { label: 'Control tables', verb: 'remember', dot: 'bg-[var(--success)]' },
};

export const ACTOR_ORDER: Actor[] = ['operator', 'app', 'job', 'control'];
