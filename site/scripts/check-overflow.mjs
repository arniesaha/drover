// Browser check for horizontal overflow, on every built page, at phone, tablet
// and desktop widths. It fails when:
//   - the document scrolls sideways, or
//   - a visible element extends past the viewport edge (even if a parent with
//     overflow: hidden clips it, which is how text ends up cut off), or
//   - the hero artwork is not centred on phone and tablet widths.
// Elements inside a deliberate horizontal scroller (overflow-x: auto) pass.
//
// Needs Playwright, which is not a dependency of the site. Run it with:
//   npm install --no-save playwright && npx playwright install chromium-headless-shell
//   npm run build && npm run check:overflow
// Pass --shots to also write screenshots at 375 and 1280 px to .shots/.
import { createServer } from 'node:http';
import { existsSync, mkdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join, relative } from 'node:path';
import { DIST, ROOT, walk } from './lib.mjs';

let chromium;
try {
  ({ chromium } = await import('playwright'));
} catch {
  console.error('check-overflow: Playwright is not installed. See the header of this script.');
  process.exit(process.argv.includes('--optional') ? 0 : 1);
}

const WIDTHS = [320, 360, 375, 390, 430, 768, 1024, 1280];
const SHOT_WIDTHS = [375, 1280];
const shots = process.argv.includes('--shots');
const shotDir = join(ROOT, '.shots');
if (shots) mkdirSync(shotDir, { recursive: true });

const TYPES = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp', '.woff2': 'font/woff2', '.txt': 'text/plain' };
const server = createServer((req, res) => {
  let path = join(DIST, decodeURIComponent(new URL(req.url, 'http://x').pathname));
  if (existsSync(path) && statSync(path).isDirectory()) path = join(path, 'index.html');
  if (!existsSync(path)) {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, { 'content-type': TYPES[extname(path)] ?? 'application/octet-stream' }).end(readFileSync(path));
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;

const pages = walk(DIST)
  .filter((f) => f.endsWith('.html'))
  .map((f) => '/' + relative(DIST, f).replace(/index\.html$/, ''))
  .sort();

const browser = await chromium.launch();
const problems = [];

for (const width of WIDTHS) {
  const context = await browser.newContext({ viewport: { width, height: 800 }, deviceScaleFactor: 1, isMobile: width < 768, hasTouch: width < 768 });
  const page = await context.newPage();
  for (const path of pages) {
    await page.goto(base + path, { waitUntil: 'load' });
    const result = await page.evaluate(() => {
      const vw = document.documentElement.clientWidth;
      const scrolls = document.documentElement.scrollWidth > vw + 1;
      const inScroller = (el) => {
        for (let p = el.parentElement; p; p = p.parentElement) {
          const ox = getComputedStyle(p).overflowX;
          if (ox === 'auto' || ox === 'scroll') return true;
        }
        return false;
      };
      const offenders = [];
      for (const el of document.querySelectorAll('body *')) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none' || style.position === 'fixed') continue;
        if (el.closest('.sr-only, .skip')) continue;
        if (rect.right > vw + 1 || rect.left < -1) {
          if (inScroller(el)) continue;
          const name = el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.split(/\s+/).filter(Boolean).slice(0, 2).join('.') : '');
          offenders.push(`${name} [${Math.round(rect.left)}..${Math.round(rect.right)}]`);
          if (offenders.length >= 4) break;
        }
      }
      const art = document.querySelector('.hero-art');
      let heroOffset = null;
      if (art) {
        const r = art.getBoundingClientRect();
        heroOffset = Math.round((r.left + r.right) / 2 - vw / 2);
      }
      return { vw, scrolls, offenders, heroOffset };
    });
    if (result.scrolls) problems.push(`${path} @${width}: the page scrolls sideways`);
    for (const o of result.offenders) problems.push(`${path} @${width}: ${o} is outside the ${result.vw}px viewport`);
    if (result.heroOffset !== null && width <= 900 && Math.abs(result.heroOffset) > 2) {
      problems.push(`${path} @${width}: hero artwork is ${result.heroOffset}px off centre`);
    }
    if (shots && SHOT_WIDTHS.includes(width) && ['/', '/architecture/', '/docs/mcp/', '/use-cases/'].includes(path)) {
      const name = (path === '/' ? 'home' : path.replace(/\//g, '-').replace(/^-|-$/g, '')) + `-${width}.png`;
      await page.waitForTimeout(300);
      await page.screenshot({ path: join(shotDir, name), fullPage: false });
    }
  }
  await context.close();
}

await browser.close();
server.close();

if (problems.length) {
  console.error(`\noverflow: ${problems.length} problem(s)`);
  for (const p of problems.slice(0, 60)) console.error(`  - ${p}`);
  process.exit(1);
}
console.log(`overflow: ok (${pages.length} pages at ${WIDTHS.join(', ')} px, no sideways scroll, nothing outside the viewport, hero centred)`);
