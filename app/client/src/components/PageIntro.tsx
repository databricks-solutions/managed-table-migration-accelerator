import { useLocation } from 'react-router';
import { pageIntroForPath } from '../lib/sectionCopy';

/** The one-line page subtitle under the shell title. Renders nothing on How it works. */
export function PageIntro() {
  const { pathname } = useLocation();
  const intro = pageIntroForPath(pathname);
  if (!intro) return null;
  return <p className="mb-4 text-sm text-muted-foreground">{intro.subtitle}</p>;
}
