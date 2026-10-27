import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

// Explicit order (no autogeneration). Labels come from each page's first H1;
// a label is set here only when the H1 is too long for a sidebar, so the
// markdown stays untouched.
const sidebars: SidebarsConfig = {
  docs: [
    {
      type: 'category',
      label: 'Getting started',
      collapsed: false,
      items: [
        {type: 'doc', id: 'README', label: 'Introduction'},
        '00-installation',
        '01-overview',
        '02-architecture',
      ],
    },
    {
      type: 'category',
      label: 'The migration',
      collapsed: false,
      items: [
        '03-discovery-and-signals',
        '04-streaming',
        '05-external-access-and-buckets',
        '06-wave-planning',
        '07-freeze-bracket',
        '07b-convert-permissions',
        '08-conversion-and-validation',
        '08c-post-convert-maintenance',
        '09-rollback-and-cleanup',
      ],
    },
    {
      type: 'category',
      label: 'Running it',
      collapsed: false,
      items: [
        '10-cockpit-and-conductor',
        '11-using-notebooks',
        '12-using-the-app',
        '13-estate-migration-playbook',
        {type: 'doc', id: '14-bulk-convert', label: '14 - Bulk convert'},
      ],
    },
    {
      type: 'category',
      label: 'Integrations',
      collapsed: false,
      items: ['15-genie-code-mcp', '16-dashboard-and-genie'],
    },
  ],
};

export default sidebars;
