// Pure link mapping for the docs site. No fs, no network: the caller passes
// everything in, so node --test can cover every link shape.
//
// The markdown in docs/ and backend/docs/ is written for github.com, so its
// relative links may point anywhere in the repo. Docusaurus resolves only
// links to pages of the same docs instance. mapRepoLink rewrites the rest:
//   - a page of another instance  -> that instance's route (/docs/..., /reference/...)
//   - anything else in the repo   -> the file (blob) or directory (tree) on GitHub
import path from 'node:path';

const SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const MARKDOWN = /\.mdx?$/i;

/**
 * @typedef {object} Instance
 * @property {string} root            absolute path of the instance's content root
 * @property {string} routeBase       route of the instance, e.g. '/docs'
 * @property {(rel: string) => boolean} owns  whether the instance publishes this
 *                                    root-relative markdown path as a page
 *
 * @typedef {object} MapOptions
 * @property {string} repoRoot        absolute path of the repository root
 * @property {string} githubBase      e.g. 'https://github.com/org/repo'
 * @property {string} [branch]        defaults to 'main'
 * @property {Instance[]} instances   every docs instance of the site
 * @property {(abs: string) => boolean} [isDirectory]  defaults to "has a trailing slash"
 */

/** Route of a published page: drop the extension; README/index name the folder. */
export function routeFor(instance, rel) {
  const parts = rel.replace(MARKDOWN, '').split('/');
  const last = parts[parts.length - 1];
  if (/^(readme|index)$/i.test(last)) parts.pop();
  return [instance.routeBase.replace(/\/$/, ''), ...parts].join('/') || '/';
}

/** `abs` relative to `root` ('' for root itself), or null when it lies outside. */
function inside(root, abs) {
  const rel = path.relative(root, abs);
  if (rel === '..' || rel.startsWith(`..${path.sep}`) || path.isAbsolute(rel)) return null;
  return rel;
}

/**
 * Map one link found in `fromFile`. Returns the new URL, or null to leave the
 * link for Docusaurus (external, in-page, site-absolute, or a page or asset of
 * the instance that contains fromFile).
 *
 * @param {string} fromFile  absolute path of the markdown file holding the link
 * @param {string} href
 * @param {MapOptions} opts
 * @returns {string | null}
 */
export function mapRepoLink(fromFile, href, opts) {
  if (!href || href.startsWith('#') || href.startsWith('/') || SCHEME.test(href)) return null;

  const cut = href.search(/[?#]/);
  const rawPath = cut === -1 ? href : href.slice(0, cut);
  const suffix = cut === -1 ? '' : href.slice(cut);
  if (!rawPath) return null;

  let decoded;
  try {
    decoded = decodeURI(rawPath);
  } catch {
    decoded = rawPath;
  }
  const target = path.resolve(path.dirname(fromFile), decoded);
  const trailingSlash = rawPath.endsWith('/');
  const isMarkdown = MARKDOWN.test(decoded);

  const current = opts.instances.find((i) => inside(i.root, fromFile) !== null);

  // Pages (and assets) of the instance holding the link: Docusaurus resolves them.
  if (current) {
    const rel = inside(current.root, target);
    if (rel !== null) {
      const relPosix = rel.split(path.sep).join('/');
      if (!isMarkdown && !trailingSlash) return null;
      if (isMarkdown && current.owns(relPosix)) return null;
    }
  }

  // A page another instance publishes: link to its route.
  if (isMarkdown) {
    for (const instance of opts.instances) {
      if (instance === current) continue;
      const rel = inside(instance.root, target);
      if (rel === null) continue;
      const relPosix = rel.split(path.sep).join('/');
      if (instance.owns(relPosix)) return routeFor(instance, relPosix) + suffix;
    }
  }

  // Everything else: the file or folder on GitHub.
  const repoRel = inside(opts.repoRoot, target);
  if (repoRel === null) return null; // outside the repo: leave it, the build reports it
  const isDirectory = opts.isDirectory ? opts.isDirectory(target) : trailingSlash;
  const kind = isDirectory || trailingSlash ? 'tree' : 'blob';
  const repoPath = repoRel.split(path.sep).map(encodeURIComponent).join('/');
  const base = `${opts.githubBase.replace(/\/$/, '')}/${kind}/${opts.branch ?? 'main'}`;
  return repoPath ? `${base}/${repoPath}${suffix}` : `${base}${suffix}`;
}
