// Content rules for the site, checked against both the source and the build:
//   1. No em dashes (or en dashes) anywhere. House style.
//   2. No private values: home directory paths, private network addresses,
//      tailnet hostnames.
//   3. No third-party requests at runtime: every script, stylesheet, font,
//      image and preload in the built HTML and CSS must be same-origin.
import { existsSync, readFileSync } from 'node:fs';
import { join, relative, extname } from 'node:path';
import { DIST, ROOT, walk, report } from './lib.mjs';

const TEXT = new Set(['.astro', '.mdx', '.md', '.ts', '.mjs', '.js', '.css', '.html', '.json', '.svg', '.yaml', '.yml', '.conf', '.txt']);
const SKIP_DIRS = ['node_modules', 'dist', '.astro'];

const sourceFiles = walk(ROOT).filter((f) => {
  const rel = relative(ROOT, f);
  if (SKIP_DIRS.some((d) => rel === d || rel.startsWith(d + '/'))) return false;
  if (rel === 'package-lock.json' || rel.startsWith('scripts/')) return false;
  return TEXT.has(extname(f)) || rel === 'Dockerfile';
});
const distFiles = existsSync(DIST) ? walk(DIST).filter((f) => TEXT.has(extname(f))) : [];
if (!distFiles.length) {
  console.error('dist/ not found. Run "npm run build" first.');
  process.exit(1);
}

const RULES = [
  { name: 'em dash', re: /—/ },
  { name: 'en dash', re: /–/ },
  { name: 'home directory path', re: /\/(?:Users|home)\/[A-Za-z0-9._-]+\// },
  { name: 'private IPv4 address', re: /\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b/ },
  { name: 'tailnet hostname', re: /[a-z0-9-]+\.ts\.net\b/i },
];

const problems = [];

for (const file of [...sourceFiles, ...distFiles]) {
  const rel = relative(ROOT, file);
  const isBundle = rel.startsWith('dist/_astro/') && rel.endsWith('.js');
  const lines = readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, i) => {
    for (const rule of RULES) {
      // Minified bundles and SVG path data are full of dotted number runs.
      if (rule.name === 'private IPv4 address' && (isBundle || / d="/.test(line) || /\bd:\s*'/.test(line))) continue;
      if (rule.re.test(line)) problems.push(`${rel}:${i + 1}: ${rule.name}`);
    }
  });
}

// Runtime third-party check. Plain <a href> links to other sites are fine;
// anything the browser would fetch by itself is not.
const fetchAttr = /<(script|link|img|source|video|audio|iframe|embed|object|use|image)\b[^>]*?\s(?:src|href|srcset|data|poster|xlink:href)\s*=\s*"((?:https?:)?\/\/[^"]+)"/gi;
const cssUrl = /url\(\s*['"]?((?:https?:)?\/\/[^'")]+)/gi;
const cssImport = /@import\s+(?:url\()?['"]?((?:https?:)?\/\/[^'")]+)/gi;
const ALLOWED_LINK_RELS = /rel="(?:canonical|noopener|noreferrer|[^"]*\b(?:canonical)\b[^"]*)"/i;

for (const file of distFiles) {
  const rel = relative(ROOT, file);
  const text = readFileSync(file, 'utf8');
  if (file.endsWith('.html')) {
    for (const m of text.matchAll(fetchAttr)) {
      const tag = m[0];
      if (m[1].toLowerCase() === 'link' && ALLOWED_LINK_RELS.test(tag)) continue;
      problems.push(`${rel}: third-party resource <${m[1]}> ${m[2]}`);
    }
  }
  if (file.endsWith('.html')) {
    // Astro drops the newline between text and an inline tag on the next
    // source line, which glues words to links ("see the<a>docs</a>"). Catch it.
    const prose = text.replace(/<(script|style|pre)[\s\S]*?<\/\1>/g, '');
    for (const m of prose.matchAll(/[A-Za-z,.:;]<(?:a|code|strong)[ >]/g)) {
      const at = prose.slice(Math.max(0, m.index - 30), m.index + 12).replace(/\s+/g, ' ');
      problems.push(`${rel}: missing space before inline element near "${at}"`);
    }
  }
  // A bare "1fr" grid track cannot shrink below its content, so one long code
  // line widens the page on a phone. Every flexible track must be minmax(0, ...)
  // or have an explicit minimum. scripts/check-overflow.mjs proves the result
  // in a real browser; this rule catches the cause without one.
  if (file.endsWith('.html') || file.endsWith('.css')) {
    for (const m of text.matchAll(/grid-template-columns:([^;}]+)/g)) {
      const bare = m[1].replace(/minmax\([^()]*(?:\([^()]*\)[^()]*)*\)/g, '');
      if (/\dfr\b/.test(bare)) problems.push(`${rel}: grid track without a minimum: "${m[1].trim().slice(0, 60)}"`);
    }
  }
  if (file.endsWith('.html') || file.endsWith('.css')) {
    for (const re of [cssUrl, cssImport]) {
      for (const m of text.matchAll(re)) problems.push(`${rel}: third-party CSS resource ${m[1]}`);
    }
  }
}

report('content', problems, `${sourceFiles.length} source files, ${distFiles.length} built files`);
