// node --test src/remark/  (no network, no fs: the instances are stubbed)
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mapRepoLink, routeFor} from './linkMap.mjs';

const REPO = '/repo';
const GH = 'https://github.com/databricks-solutions/managed-table-migration-accelerator';
const REFERENCE_PAGES = new Set([
  'contracts/control-tables.md',
  'contracts/jobs.md',
  'install-and-permissions.md',
]);
const docs = {root: '/repo/docs', routeBase: '/docs', owns: (rel) => !rel.startsWith('site/')};
const reference = {
  root: '/repo/backend/docs',
  routeBase: '/reference',
  owns: (rel) => REFERENCE_PAGES.has(rel),
};
const opts = {repoRoot: REPO, githubBase: GH, instances: [docs, reference]};
const fromDocs = '/repo/docs/12-using-the-app.md';
const fromRef = '/repo/backend/docs/install-and-permissions.md';

test('leaves external, in-page and site-absolute links alone', () => {
  for (const href of ['https://learn.microsoft.com/x', 'mailto:a@b.c', '#anchor', '/docs/x', '']) {
    assert.equal(mapRepoLink(fromDocs, href, opts), null, href);
  }
});

test('leaves pages and assets of the same instance to Docusaurus', () => {
  assert.equal(mapRepoLink(fromDocs, './00-installation.md#who-needs', opts), null);
  assert.equal(mapRepoLink(fromDocs, 'README.md', opts), null);
  assert.equal(mapRepoLink(fromDocs, 'images/install/app/granted.png', opts), null);
  assert.equal(mapRepoLink(fromRef, './contracts/jobs.md', opts), null);
});

test('docs -> an included reference page maps to its route, anchor kept', () => {
  assert.equal(
    mapRepoLink(fromDocs, '../backend/docs/contracts/jobs.md#job_04_convert', opts),
    '/reference/contracts/jobs#job_04_convert',
  );
  assert.equal(
    mapRepoLink(fromDocs, '../backend/docs/install-and-permissions.md', opts),
    '/reference/install-and-permissions',
  );
});

test('reference -> a docs page maps to the docs route', () => {
  assert.equal(
    mapRepoLink(fromRef, '../../docs/00-installation.md#install', opts),
    '/docs/00-installation#install',
  );
  assert.equal(mapRepoLink(fromRef, '../../docs/README.md', opts), '/docs');
});

test('a page in the same root that the instance does not publish goes to GitHub', () => {
  assert.equal(
    mapRepoLink(fromRef, './integration/control-tables.md', opts),
    `${GH}/blob/main/backend/docs/integration/control-tables.md`,
  );
  assert.equal(
    mapRepoLink('/repo/backend/docs/contracts/jobs.md', '../design-deltas.md#x', opts),
    `${GH}/blob/main/backend/docs/design-deltas.md#x`,
  );
});

test('anything else in the repo goes to GitHub: files as blob, folders as tree', () => {
  assert.equal(mapRepoLink(fromDocs, '../AGENTS.md', opts), `${GH}/blob/main/AGENTS.md`);
  assert.equal(
    mapRepoLink(fromDocs, '../app/resources/app.yml', opts),
    `${GH}/blob/main/app/resources/app.yml`,
  );
  assert.equal(
    mapRepoLink(fromDocs, '../backend/docs/genie/', opts),
    `${GH}/tree/main/backend/docs/genie`,
  );
  assert.equal(
    mapRepoLink(fromDocs, '../backend/notebooks', {...opts, isDirectory: () => true}),
    `${GH}/tree/main/backend/notebooks`,
  );
  assert.equal(
    mapRepoLink(fromDocs, '../backend/docs/reference/action-queue.md', opts),
    `${GH}/blob/main/backend/docs/reference/action-queue.md`,
  );
});

test('the repo README goes to GitHub, not the docs index', () => {
  assert.equal(mapRepoLink(fromDocs, '../README.md#install', opts), `${GH}/blob/main/README.md#install`);
});

test('encoded paths are decoded to resolve and re-encoded for the URL', () => {
  assert.equal(
    mapRepoLink(fromDocs, '../backend/docs/a%20b.md', opts),
    `${GH}/blob/main/backend/docs/a%20b.md`,
  );
});

test('a link that leaves the repo is left for the build to report', () => {
  assert.equal(mapRepoLink(fromDocs, '../../outside.md', opts), null);
});

test('routeFor drops the extension and names folders by README/index', () => {
  assert.equal(routeFor(docs, '00-installation.md'), '/docs/00-installation');
  assert.equal(routeFor(docs, 'README.md'), '/docs');
  assert.equal(routeFor(reference, 'contracts/control-tables.md'), '/reference/contracts/control-tables');
});
