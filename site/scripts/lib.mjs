// Shared helpers for the post-build checks. No dependencies on purpose.
import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const DIST = fileURLToPath(new URL('../dist/', import.meta.url));
export const ROOT = fileURLToPath(new URL('../', import.meta.url));

export function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else out.push(path);
  }
  return out;
}

export function report(name, problems, summary) {
  if (problems.length) {
    console.error(`\n${name}: ${problems.length} problem(s)`);
    for (const p of problems) console.error(`  - ${p}`);
    process.exit(1);
  }
  console.log(`${name}: ok (${summary})`);
}
