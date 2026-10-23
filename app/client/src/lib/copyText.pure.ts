/** The one Clipboard API method the copy affordances use. */
export interface ClipboardLike {
  writeText: (text: string) => Promise<void>;
}

/**
 * Copy `text` to the clipboard. Resolves `true` on success and `false` when the Clipboard API is
 * missing (non-secure context) or the write is refused — never throws, so a failed copy leaves
 * the selectable text as the fallback.
 */
export async function copyText(text: string, clipboard?: ClipboardLike | null): Promise<boolean> {
  if (!clipboard || typeof clipboard.writeText !== 'function') return false;
  try {
    await clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
