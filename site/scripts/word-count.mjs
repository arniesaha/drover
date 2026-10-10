// Prints the number of prose words on each built page, so copy edits can be
// measured. Counts the text inside <main>, without code blocks, inline SVG,
// scripts or styles. Run after a build: node scripts/word-count.mjs
import { readFileSync } from 'node:fs';
import { relative } from 'node:path';
import { DIST, walk } from './lib.mjs';

const count = (file) => {
  const html = readFileSync(file, 'utf8');
  const main = html.match(/<main[\s\S]*<\/main>/)?.[0] ?? '';
  const text = main
    .replace(/<(script|style|pre|svg)[\s\S]*?<\/\1>/g, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&[a-z#0-9]+;/gi, ' ');
  return text.split(/\s+/).filter((w) => /[A-Za-z0-9]/.test(w)).length;
};

const rows = walk(DIST)
  .filter((f) => f.endsWith('.html'))
  .map((f) => ({ page: '/' + relative(DIST, f).replace(/index\.html$/, ''), words: count(f) }))
  .sort((a, b) => a.page.localeCompare(b.page));

console.table(rows);
console.log(`total ${rows.reduce((n, r) => n + r.words, 0)} words on ${rows.length} pages`);
