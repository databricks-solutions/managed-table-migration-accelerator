/**
 * How an action / runbook status renders. DEFERRED is operator-set (08/08b finish_action=defer)
 * and must never read like OPEN work or like DONE — it is set aside, still listed, reversible.
 */
export interface ActionStatusPresentation {
  label: string;
  deferred: boolean;
  title?: string;
}

export function actionStatusPresentation(status: string | null | undefined): ActionStatusPresentation {
  const s = (status ?? '').trim();
  if (s.toUpperCase() === 'DEFERRED') {
    return {
      label: 'Deferred',
      deferred: true,
      title: 'Set aside by an operator (finish_action=defer). Does not hold the wave; Reopen returns it to OPEN.',
    };
  }
  return { label: s || '—', deferred: false };
}
