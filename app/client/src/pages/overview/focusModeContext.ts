import { createContext, useContext } from 'react';

/**
 * Focus mode for the guided card's descendants: the focused scope, or `null` in the estate view.
 * The convert gate and the freeze card read it to pin their params to the scope's selected wave
 * (a focused schema is walked one wave at a time). `status` is the scope's `scope_status`, so a
 * finished step says why (complete, handled elsewhere, re-plan). The estate card never sets it.
 */
export interface FocusModeValue {
  focus: string | null;
  status?: string | null;
}

export const FocusModeContext = createContext<FocusModeValue>({ focus: null });

export function useFocusMode(): FocusModeValue {
  return useContext(FocusModeContext);
}
