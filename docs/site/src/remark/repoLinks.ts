// Remark plugin: rewrite relative links that Docusaurus cannot resolve (they
// point outside the docs instance) to a site route or a GitHub URL, at build
// time. The markdown itself is never edited, so it keeps working on github.com.
// The mapping is the pure mapRepoLink (./linkMap.mjs, unit-tested).
import fs from 'node:fs';
import {mapRepoLink, type MapOptions} from './linkMap.mjs';

type Node = {type: string; url?: string; children?: Node[]};
type VFile = {path?: string; history?: string[]};

function isDirectory(abs: string): boolean {
  try {
    return fs.statSync(abs).isDirectory();
  } catch {
    return false;
  }
}

function walk(node: Node, visit: (n: Node) => void): void {
  visit(node);
  for (const child of node.children ?? []) walk(child, visit);
}

export default function repoLinks(options: MapOptions) {
  const opts: MapOptions = {isDirectory, ...options};
  return (tree: Node, file: VFile) => {
    const from = file.path ?? file.history?.[0];
    if (!from) return;
    walk(tree, (node) => {
      if ((node.type === 'link' || node.type === 'definition') && node.url) {
        const mapped = mapRepoLink(from, node.url, opts);
        if (mapped !== null) node.url = mapped;
      }
    });
  };
}
