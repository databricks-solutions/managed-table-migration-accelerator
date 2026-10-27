export interface Instance {
  root: string;
  routeBase: string;
  owns: (rel: string) => boolean;
}
export interface MapOptions {
  repoRoot: string;
  githubBase: string;
  branch?: string;
  instances: Instance[];
  isDirectory?: (abs: string) => boolean;
}
export function routeFor(instance: Instance, rel: string): string;
export function mapRepoLink(fromFile: string, href: string, opts: MapOptions): string | null;
