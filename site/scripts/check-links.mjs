// Link checker for the built site. Verifies that every internal href and src
// resolves to a file in dist/, and that every #fragment points at an element
// id that exists on the target page. External links are counted, not fetched,
// so the check works offline and never depends on someone else's uptime.
import { existsSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { DIST, walk, report } from './lib.mjs';

if (!existsSync(DIST)) {
  console.error('dist/ not found. Run "npm run build" first.');
  process.exit(1);
}

const pages = walk(DIST).filter((f) => f.endsWith('.html'));
const idsByFile = new Map();
const attrRe = /\s(?:href|src|poster)\s*=\s*"([^"]*)"/g;
const srcsetRe = /\ssrcset\s*=\s*"([^"]*)"/g;
const idRe = /\sid\s*=\s*"([^"]+)"/g;

const strip = (html) => html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '');

function idsFor(file) {
  if (!idsByFile.has(file)) {
    const html = strip(readFileSync(file, 'utf8'));
    idsByFile.set(file, new Set([...html.matchAll(idRe)].map((m) => m[1])));
  }
  return idsByFile.get(file);
}

// Map a site-absolute path to the file that a static server would return.
function resolve(pathname) {
  const clean = decodeURIComponent(pathname);
  const direct = join(DIST, clean);
  if (clean.endsWith('/')) {
    const index = join(direct, 'index.html');
    return existsSync(index) ? index : null;
  }
  if (existsSync(direct) && statSync(direct).isFile()) return direct;
  return null;
}

const problems = [];
let internal = 0;
let external = 0;
let anchors = 0;

for (const page of pages) {
  const rel = '/' + relative(DIST, page).replace(/\\/g, '/');
  const pageUrl = new URL(rel.replace(/index\.html$/, ''), 'http://site.local');
  const html = strip(readFileSync(page, 'utf8'));

  const targets = [...html.matchAll(attrRe)].map((m) => m[1]);
  for (const m of html.matchAll(srcsetRe)) {
    for (const part of m[1].split(',')) targets.push(part.trim().split(/\s+/)[0]);
  }

  for (const raw of targets) {
    const target = raw.replace(/&amp;/g, '&').replace(/&#38;/g, '&');
    if (!target || target.startsWith('data:') || target.startsWith('mailto:')) continue;

    let url;
    try {
      url = new URL(target, pageUrl);
    } catch {
      problems.push(`${rel}: unparseable link "${raw}"`);
      continue;
    }
    if (url.origin !== pageUrl.origin) {
      external++;
      continue;
    }

    internal++;
    const file = resolve(url.pathname);
    if (!file) {
      const hint = !url.pathname.endsWith('/') && resolve(url.pathname + '/') ? ' (missing trailing slash)' : '';
      problems.push(`${rel}: broken link "${raw}"${hint}`);
      continue;
    }
    if (url.hash.length > 1 && file.endsWith('.html')) {
      anchors++;
      const id = decodeURIComponent(url.hash.slice(1));
      if (!idsFor(file).has(id)) problems.push(`${rel}: missing anchor "${raw}"`);
    }
  }
}

report('links', problems, `${pages.length} pages, ${internal} internal links, ${anchors} anchors, ${external} external links not fetched`);
