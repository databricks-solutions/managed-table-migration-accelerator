import { useEffect, useRef, useState } from 'react';

/**
 * useInView — a thin IntersectionObserver wrapper for scroll-reveal and scrollytelling.
 * Returns a ref to attach and whether the element has entered the viewport. `once` (default
 * true) latches the first entry so a revealed element never flickers back out on scroll-up.
 *
 * Reveal is expressed in CSS via the `data-inview` attribute (see how.css) using a
 * transition, so it is interruptible and runs off the main thread. No scroll listeners.
 */
export function useInView<T extends Element = HTMLDivElement>(options?: {
  once?: boolean;
  rootMargin?: string;
  threshold?: number;
}): { ref: React.RefObject<T | null>; inView: boolean } {
  const { once = true, rootMargin = '-100px 0px', threshold = 0 } = options ?? {};
  const ref = useRef<T | null>(null);
  // No IntersectionObserver (old browser / test env): start revealed rather than hide content.
  // Initialized lazily so we never setState synchronously inside the effect.
  const [inView, setInView] = useState(() => typeof IntersectionObserver === 'undefined');

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setInView(true);
            if (once) observer.unobserve(entry.target);
          } else if (!once) {
            setInView(false);
          }
        }
      },
      { rootMargin, threshold },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [once, rootMargin, threshold]);

  return { ref, inView };
}

/**
 * useReducedMotion — true when the user asked the OS to reduce motion. Components use it to
 * skip decorative movement (keeping opacity/colour) rather than assuming everyone wants it.
 */
export function useReducedMotion(): boolean {
  // Read the initial value lazily (during render, not in the effect) so we only ever call
  // setState from the change listener — never synchronously inside the effect body.
  const [reduced, setReduced] = useState(
    () =>
      typeof window !== 'undefined' &&
      !!window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  );
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return reduced;
}
