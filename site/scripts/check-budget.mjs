// A small, offline stand-in for a Lighthouse budget. It measures what each
// page makes the browser download on first load (HTML plus the CSS, JS and
// fonts it references, gzipped) and fails when a page or the whole build
// grows past the limits below. Images are reported but budgeted separately
// because they are lazy-loaded below the fold.
import { readFileSync, statSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { join, relative } from 'node:path';
import { DIST, walk, report } from './lib.mjs';

const KB = 1024;
const BUDGET = {
  pageCriticalGzip: 170 * KB, // HTML + CSS + JS, gzipped, per page
  pageJsGzip: 12 * KB, // client JS per page
  fontsTotal: 260 * KB, // all self-hosted fonts
  distTotal: 6 * KB * KB, // whole build output
};

const files = walk(DIST);
const size = (f) => statSync(f).size;
const gz = (f) => gzipSync(readFileSync(f)).length;
const fmt = (n) => `${(n / KB).toFixed(1)} kB`;

const problems = [];
const rows = [];

for (const page of files.filter((f) => f.endsWith('.html'))) {
  const rel = '/' + relative(DIST, page).replace(/index\.html$/, '');
  const html = readFileSync(page, 'utf8');
  const assets = new Set([...html.matchAll(/(?:href|src)="(\/_astro\/[^"]+\.(?:css|js))"/g)].map((m) => m[1]));
  let css = 0;
  let js = 0;
  for (const a of assets) {
    const n = gz(join(DIST, a));
    if (a.endsWith('.js')) js += n;
    else css += n;
  }
  // Inline module scripts count as JS too.
  for (const m of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) {
    js += gzipSync(Buffer.from(m[1])).length;
  }
  const critical = gz(page) + css + js;
  rows.push({ page: rel, html: fmt(gz(page)), css: fmt(css), js: fmt(js), critical: fmt(critical) });
  if (critical > BUDGET.pageCriticalGzip) problems.push(`${rel}: critical path ${fmt(critical)} over ${fmt(BUDGET.pageCriticalGzip)}`);
  if (js > BUDGET.pageJsGzip) problems.push(`${rel}: client JS ${fmt(js)} over ${fmt(BUDGET.pageJsGzip)}`);
}

const fonts = files.filter((f) => /\.woff2?$/.test(f)).reduce((n, f) => n + size(f), 0);
const images = files.filter((f) => /\.(png|jpe?g|webp|avif|svg)$/.test(f)).reduce((n, f) => n + size(f), 0);
const total = files.reduce((n, f) => n + size(f), 0);

if (fonts > BUDGET.fontsTotal) problems.push(`fonts ${fmt(fonts)} over ${fmt(BUDGET.fontsTotal)}`);
if (total > BUDGET.distTotal) problems.push(`dist ${fmt(total)} over ${fmt(BUDGET.distTotal)}`);

console.table(rows);
console.log(`fonts ${fmt(fonts)} · images ${fmt(images)} · dist total ${fmt(total)} in ${files.length} files`);
report('budget', problems, 'gzipped first-load sizes within limits');
