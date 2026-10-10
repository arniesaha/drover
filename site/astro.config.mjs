import { defineConfig } from 'astro/config';
import mdx from '@astrojs/mdx';

// Fully static output. No adapter, no server routes, no runtime third parties.
export default defineConfig({
  site: 'https://drover.fyi',
  output: 'static',
  trailingSlash: 'always',
  build: { format: 'directory' },
  integrations: [mdx()],
  markdown: {
    shikiConfig: {
      themes: { light: 'vitesse-light', dark: 'vitesse-dark' },
      defaultColor: false,
      wrap: false,
    },
  },
  devToolbar: { enabled: false },
});
