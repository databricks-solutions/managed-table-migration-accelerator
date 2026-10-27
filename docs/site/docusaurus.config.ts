import * as path from 'path';
import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';
import repoLinks from './src/remark/repoLinks';

// This runs in Node.js - don't use client-side code here (browser APIs, JSX...)

const ORG = 'databricks-solutions';
const REPO = 'managed-table-migration-accelerator';
const GITHUB = `https://github.com/${ORG}/${REPO}`;
const TITLE = 'External → Managed Table Migration';

const REPO_ROOT = path.resolve(__dirname, '../..');
const DOCS_ROOT = path.join(REPO_ROOT, 'docs');
const REFERENCE_ROOT = path.join(REPO_ROOT, 'backend/docs');

// The markdown in docs/ is the single source of truth; it must keep rendering on
// github.com, so it is never edited for the site.
const DOCS_EXCLUDE = ['site/**', 'superpowers/**', '**/node_modules/**'];

// The backend pages published under /reference. Every other backend page is
// linked on GitHub (src/remark/repoLinks.ts).
const REFERENCE_PAGES = [
  'contracts/control-tables.md',
  'contracts/jobs.md',
  'state-machine.md',
  'architecture.md',
  'install-and-permissions.md',
  'operations/privileges.md',
];

const linkOptions = {
  repoRoot: REPO_ROOT,
  githubBase: GITHUB,
  instances: [
    {
      root: DOCS_ROOT,
      routeBase: '/docs',
      owns: (rel: string) =>
        !rel.startsWith('site/') && !rel.startsWith('superpowers/') && !rel.includes('node_modules/'),
    },
    {
      root: REFERENCE_ROOT,
      routeBase: '/reference',
      owns: (rel: string) => REFERENCE_PAGES.includes(rel),
    },
  ],
};

const config: Config = {
  title: TITLE,
  tagline: 'Convert Unity Catalog external tables to managed tables, safely and at estate scale.',
  favicon: 'img/favicon.svg',

  url: `https://${ORG}.github.io`,
  baseUrl: `/${REPO}/`,
  trailingSlash: false,
  organizationName: ORG,
  projectName: REPO,

  onBrokenLinks: 'throw',

  markdown: {
    // .md files are CommonMark, not MDX: the pages contain <, { and raw HTML.
    format: 'detect',
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },
  themes: ['@docusaurus/theme-mermaid'],

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          path: '..',
          routeBasePath: 'docs',
          exclude: DOCS_EXCLUDE,
          // URLs = file names (00-installation, 07b-convert-permissions).
          numberPrefixParser: false,
          sidebarPath: './sidebars.ts',
          beforeDefaultRemarkPlugins: [[repoLinks, linkOptions]],
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  plugins: [
    [
      '@docusaurus/plugin-content-docs',
      {
        id: 'reference',
        path: '../../backend/docs',
        routeBasePath: 'reference',
        include: REFERENCE_PAGES,
        exclude: ['superpowers/**', 'genie/**', 'app/**', '**/node_modules/**'],
        numberPrefixParser: false,
        sidebarPath: './sidebarsReference.ts',
        beforeDefaultRemarkPlugins: [[repoLinks, linkOptions]],
      },
    ],
    // Full-text search (no external service).
    require.resolve('docusaurus-lunr-search'),
    // Click-to-zoom on doc images (options are read from themeConfig.zoom).
    'docusaurus-plugin-image-zoom',
    // llms.txt (+ per-page markdown) for LLM consumption.
    [
      '@signalwire/docusaurus-plugin-llms-txt',
      {
        siteTitle: TITLE,
        siteDescription:
          'Databricks tooling that converts Unity Catalog EXTERNAL tables to MANAGED tables with ALTER TABLE ... SET MANAGED, packaged as one Databricks Asset Bundle.',
      },
    ],
  ],

  themeConfig: {
    colorMode: {
      defaultMode: 'light',
      disableSwitch: false,
      respectPrefersColorScheme: true,
    },
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
    },
    // docusaurus-plugin-image-zoom ignores plugin options and reads only this key.
    zoom: {
      selector: '.markdown img',
      background: {light: '#ffffff', dark: '#1b1b1d'},
    },
    navbar: {
      title: TITLE,
      logo: {alt: TITLE, src: 'img/logo.svg'},
      items: [
        {type: 'docSidebar', sidebarId: 'docs', position: 'left', label: 'Docs'},
        {
          type: 'docSidebar',
          sidebarId: 'reference',
          docsPluginId: 'reference',
          position: 'left',
          label: 'Reference',
        },
        {href: GITHUB, label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Installation', to: '/docs/00-installation'},
            {label: 'Using the app', to: '/docs/12-using-the-app'},
            {label: 'Estate migration playbook', to: '/docs/13-estate-migration-playbook'},
          ],
        },
        {
          title: 'Reference',
          items: [
            {label: 'Control tables', to: '/reference/contracts/control-tables'},
            {label: 'Jobs', to: '/reference/contracts/jobs'},
          ],
        },
        {
          title: 'Project',
          items: [
            {label: 'GitHub', href: GITHUB},
            {label: 'License', href: `${GITHUB}/blob/main/LICENSE.md`},
            {label: 'Security', href: `${GITHUB}/blob/main/SECURITY.md`},
            {label: 'Notice', href: `${GITHUB}/blob/main/NOTICE.md`},
          ],
        },
      ],
      copyright: '© Databricks 2026. Provided by Databricks Solutions.',
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'sql', 'python', 'yaml', 'json', 'typescript'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
