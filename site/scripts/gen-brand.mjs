// Brand assets derived from the README hero artwork (src/assets/drover-hero.png):
//   - prints the dominant colours of the image, which the theme tokens in
//     src/styles/global.css are built around;
//   - writes the mascot mark (the dog's head on the dusk-teal sky) used as the
//     header logo and favicon.
// Run with: node scripts/gen-brand.mjs
import sharp from 'sharp';
import { fileURLToPath } from 'node:url';

const at = (p) => fileURLToPath(new URL(p, import.meta.url));
const HERO = at('../src/assets/drover-hero.png');

// 1. Palette: quantise opaque pixels into coarse buckets and rank by count.
const { data, info } = await sharp(HERO).resize(160, 160).ensureAlpha().raw().toBuffer({ resolveWithObject: true });
const buckets = new Map();
for (let i = 0; i < data.length; i += info.channels) {
  const [r, g, b, a] = [data[i], data[i + 1], data[i + 2], data[i + 3]];
  if (a < 200) continue;
  const key = [r, g, b].map((v) => Math.round(v / 24)).join(',');
  const cur = buckets.get(key) ?? { n: 0, r: 0, g: 0, b: 0 };
  cur.n++;
  cur.r += r;
  cur.g += g;
  cur.b += b;
  buckets.set(key, cur);
}
const hex = (v) => Math.round(v).toString(16).padStart(2, '0');
const ranked = [...buckets.values()].sort((x, y) => y.n - x.n).slice(0, 18);
console.log('Dominant colours in the hero artwork:');
for (const c of ranked) {
  console.log(`  #${hex(c.r / c.n)}${hex(c.g / c.n)}${hex(c.b / c.n)}  ${c.n}`);
}

// 2. Mascot mark: a square crop around the head, on the sky colour.
const SKY = '#1d6079';
const crop = { left: 236, top: 22, width: 400, height: 400 };
const head = await sharp(HERO).extract(crop).resize(256, 256).png().toBuffer();
const circle = Buffer.from('<svg width="256" height="256"><circle cx="128" cy="128" r="128"/></svg>');
const mark = await sharp({ create: { width: 256, height: 256, channels: 4, background: SKY } })
  .composite([{ input: head }, { input: circle, blend: 'dest-in' }])
  .png({ compressionLevel: 9 })
  .toBuffer();
await sharp(mark).toFile(at('../src/assets/drover-mark.png'));
await sharp(mark).resize(64, 64).png({ compressionLevel: 9 }).toFile(at('../public/favicon.png'));
await sharp(mark).resize(180, 180).png({ compressionLevel: 9 }).toFile(at('../public/apple-touch-icon.png'));
console.log('Wrote src/assets/drover-mark.png, public/favicon.png, public/apple-touch-icon.png');
