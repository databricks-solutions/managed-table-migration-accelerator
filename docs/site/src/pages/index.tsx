import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import styles from './index.module.css';

// Every fact on this page comes from README.md or docs/01-overview.md. Check a
// number against its source before changing it.
const STATS = [
  // README "There is one engine and three ways to drive it."
  {value: '3', label: 'ways to drive it: the app, notebooks, or Genie Code'},
  // README / docs/09: rollback with UNSET MANAGED within 14 days.
  {value: '14 days', label: 'to roll a converted table back'},
  // backend/src/migration/actions.py ALL_KINDS.
  {value: '15', label: 'kinds of follow-up task it tracks'},
  // README "One Databricks Asset Bundle".
  {value: '1', label: 'Databricks Asset Bundle to deploy'},
];

// The README lifecycle diagram, in the same order.
const LIFECYCLE: {step: string; detail: string; optional?: boolean; key?: boolean}[] = [
  {step: 'Discover', detail: 'Find external tables'},
  {step: 'Assess', detail: 'What would break?'},
  {step: 'Plan waves', detail: 'Pilot first'},
  {step: 'Dry run', detail: 'Go or no-go per schema'},
  {step: 'Freeze', detail: 'Pause compaction, stop streams', optional: true},
  {step: 'Authorize', detail: 'Grant missing privileges', optional: true},
  {step: 'Convert', detail: 'Wave by wave', key: true},
  {step: 'Validate', detail: 'Check, then unfreeze'},
  {step: 'Finish steps', detail: 'Re-share, Compatibility Mode, optimization'},
  {step: 'Cleanup', detail: 'After day 14'},
];

const PATHS = [
  {
    who: 'Operator',
    what: 'Run the migration day to day.',
    links: [
      {label: 'Using the app', to: '/docs/12-using-the-app'},
      {label: 'Estate migration playbook', to: '/docs/13-estate-migration-playbook'},
    ],
  },
  {
    who: 'Platform admin',
    what: 'Install it once and grant what the jobs need.',
    links: [
      {label: 'Installation', to: '/docs/00-installation'},
      {label: 'Convert permissions', to: '/docs/07b-convert-permissions'},
    ],
  },
  {
    who: 'Contributor',
    what: 'Change the tooling or build on its tables.',
    links: [
      {label: 'Architecture', to: '/docs/02-architecture'},
      {label: 'Reference', to: '/reference/contracts/control-tables'},
    ],
  },
];

function Hero(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={styles.hero}>
      <div className="container">
        <Heading as="h1" className={styles.heroTitle}>
          {siteConfig.title}
        </Heading>
        <p className={styles.heroLead}>
          Convert Unity Catalog external tables to managed tables with{' '}
          <code>ALTER TABLE … SET MANAGED</code>. One Databricks Asset Bundle does the bookkeeping.
        </p>
        <div className={styles.heroButtons}>
          <Link className="button button--primary button--lg" to="/docs/00-installation">
            Get started
          </Link>
          <Link
            className="button button--secondary button--outline button--lg"
            href="https://github.com/databricks-solutions/managed-table-migration-accelerator">
            View on GitHub
          </Link>
        </div>
      </div>
    </header>
  );
}

function Stats(): ReactNode {
  return (
    <section className={styles.stats} aria-label="At a glance">
      <div className={clsx('container', styles.statsGrid)}>
        {STATS.map((s) => (
          <div key={s.label} className={styles.stat}>
            <span className={styles.statValue}>{s.value}</span>
            <span className={styles.statLabel}>{s.label}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function Lifecycle(): ReactNode {
  return (
    <section className={styles.section}>
      <div className="container">
        <Heading as="h2">How a table moves</Heading>
        <p className={styles.sectionLead}>
          Every table follows the same steps. Each step is a job you launch.
        </p>
        <ol className={styles.loop}>
          {LIFECYCLE.map((s) => (
            <li
              key={s.step}
              className={clsx(styles.loopStep, s.key && styles.loopKey, s.optional && styles.loopOptional)}>
              <span className={styles.loopName}>
                {s.step}
                {s.optional && <span className={styles.loopTag}>optional</span>}
              </span>
              <span className={styles.loopDetail}>{s.detail}</span>
            </li>
          ))}
        </ol>
        <p className={styles.rollback}>
          <strong>Rollback:</strong> within 14 days of the convert, a converted table can go back to
          external.
        </p>
      </div>
    </section>
  );
}

function Paths(): ReactNode {
  return (
    <section className={clsx(styles.section, styles.sectionAlt)}>
      <div className="container">
        <Heading as="h2">Choose your path</Heading>
        <div className={styles.cards}>
          {PATHS.map((p) => (
            <div key={p.who} className={styles.card}>
              <Heading as="h3">{p.who}</Heading>
              <p>{p.what}</p>
              <ul>
                {p.links.map((l) => (
                  <li key={l.to}>
                    <Link to={l.to}>{l.label} →</Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Safety(): ReactNode {
  return (
    <section className={styles.section}>
      <div className="container">
        <blockquote className={styles.safety}>
          <p className={styles.safetyRule}>
            You build the tooling; a human runs the conversion.
          </p>
          <p>
            Nothing converts on its own. The tool recommends the next step. A person launches each
            conversion, and every job checks its safety gates again before it changes a table.
          </p>
        </blockquote>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout title="Home" description={siteConfig.tagline}>
      <Hero />
      <main>
        <Stats />
        <Lifecycle />
        <Paths />
        <Safety />
      </main>
    </Layout>
  );
}
