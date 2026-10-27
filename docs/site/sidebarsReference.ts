import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

// The backend pages published under /reference (see REFERENCE_PAGES in
// docusaurus.config.ts). Other backend pages are linked on GitHub.
const sidebars: SidebarsConfig = {
  reference: [
    {
      type: 'category',
      label: 'Contracts',
      collapsed: false,
      items: ['contracts/control-tables', 'contracts/jobs'],
    },
    {
      type: 'category',
      label: 'Design',
      collapsed: false,
      items: [
        'architecture',
        'state-machine',
        {type: 'doc', id: 'install-and-permissions', label: 'Install and permissions'},
        {type: 'doc', id: 'operations/privileges', label: 'Privileges'},
      ],
    },
  ],
};

export default sidebars;
