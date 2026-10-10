# Drover site

The product and documentation site for Drover, intended for `drover.fyi`. It is
a fully static [Astro](https://astro.build) site: MDX for the docs, plain CSS,
and no client JavaScript except the theme toggle and the three interactive
use-case demos. There are no analytics, trackers, external fonts or CDN
requests at runtime. Fonts are self-hosted from `@fontsource-variable`
packages and bundled at build time.

It lives in the main repository so the docs can change in the same pull
request as the code they describe.

## Requirements

- Node.js 22.12 or later
- npm (the lockfile is `package-lock.json`)

## Local development

```bash
cd site
npm ci
npm run dev        # dev server with hot reload, http://localhost:4321
```

## Build, check, preview

```bash
npm run build      # static output in dist/
npm run check      # link, content and size checks against dist/
npm test           # build, then check
npm run preview    # serve dist/ locally
```

`npm run check` runs three dependency-free scripts from `scripts/`:

| Script | What it verifies |
| --- | --- |
| `check-links.mjs` | Every internal link, asset and `#anchor` in the built HTML resolves. External links are counted, not fetched, so the check works offline. |
| `check-content.mjs` | No em or en dashes, no home-directory paths, private addresses or tailnet hostnames, no third-party resources loaded at runtime, and no words glued to links. |
| `check-budget.mjs` | A small offline stand-in for a Lighthouse budget: gzipped first-load size per page, client JavaScript per page, fonts and total output. |

Set `ASTRO_TELEMETRY_DISABLED=1` if you do not want the Astro CLI to send its
own anonymous build telemetry. CI and the Dockerfile set it.

## Layout

```text
site/
  astro.config.mjs
  src/
    site.ts                 site name, repository links, navigation
    styles/                 design tokens, global styles, font faces
    layouts/                BaseLayout (chrome), DocsLayout (sidebar + TOC)
    components/
      ArchitectureDiagram.astro   the SVG diagram, generated from data
      LayerStack.astro            the interactive ecosystem layer stack
      demos/                      the three interactive use cases
    data/layers.ts          the capability-layer model behind "Where Drover fits"
    data/sessions.ts        synthetic data for the recall demo
    content/docs/*.mdx      the docs pages
    pages/                  landing, features, use cases, architecture, FAQ, contribute
  scripts/                  post-build checks
  deploy/                   nginx.conf and k8s.yaml
  Dockerfile
  vercel.json
```

### Editing content

- Docs pages are MDX files in `src/content/docs/`. The frontmatter `order`
  sets the sidebar position and `source` names the in-repo document the page
  summarizes.
- The architecture diagram is data in `src/components/ArchitectureDiagram.astro`.
  Edit the `cards` and `edges` arrays, then compare against
  `docs/architecture.md` and `docs/drover-architecture.png`.
- The "Where Drover fits" layer stack renders from `src/data/layers.ts`. Layer
  names, statuses, examples and boundary interfaces all live in that one file.
- The theme colours are sampled from `src/assets/drover-hero.png`. Run
  `node scripts/gen-brand.mjs` to print the palette and regenerate the mark.
- Demo data is synthetic. Keep it that way: no real hostnames, repositories or
  session content.
- Write for a stranger. Claim only what the code and docs support today, and
  label anything else as opt-in or roadmap.
- House style: no em dashes. `npm run check` enforces it.

## Deploy

### Container (internal sign-off)

```bash
docker build -t REGISTRY/drover-site:TAG site/
docker run --rm -p 8080:8080 REGISTRY/drover-site:TAG
# http://localhost:8080, health check at /healthz
```

The image is a two-stage build. The first stage runs `npm ci`,
`npm run build` and `npm run check`. The second copies `dist/` into
`nginxinc/nginx-unprivileged`, which runs as a non-root user on port 8080.
`deploy/nginx.conf` enables gzip, sends immutable cache headers for the
fingerprinted `/_astro/` assets and `no-cache` for HTML, sets security headers
including a same-origin content security policy, and returns a real 404 page
for unknown paths. There is no single-page-app fallback.

`deploy/k8s.yaml` holds a Deployment and a NodePort Service. Replace the
`REGISTRY/drover-site:TAG` image placeholder and the `nodePort` value before
applying it. It needs no secrets or volumes other than an in-memory `/tmp`,
and runs with a read-only root filesystem.

### Vercel

`vercel.json` in this directory carries the settings. In the project settings
choose:

| Setting | Value |
| --- | --- |
| Root Directory | `site` |
| Framework Preset | Astro |
| Install Command | `npm ci` |
| Build Command | `npm run build` |
| Output Directory | `dist` |
| Node.js Version | 22.x |

No environment variables are required. The canonical URL comes from `site` in
`astro.config.mjs`; change it there if the domain changes.

## Continuous integration

`.github/workflows/site.yml` builds the site and runs the checks whenever
`site/` changes. A pull request that touches only `site/` skips the Python and
iOS test suites in the main workflows.

## Placeholders

These need a decision before public launch:

- The header mark and favicon are cropped from the README hero artwork by
  `scripts/gen-brand.mjs`. Replace them if a dedicated logo is drawn.
- No social sharing image is set yet.
- The iOS section says a public beta is coming soon and has no download link.
