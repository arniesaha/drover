// Shared site constants. Keep every outbound URL here so they are easy to audit.
export const SITE = {
  name: 'Drover',
  tagline: 'Drive your coding-agent fleet from your pocket.',
  description:
    'Drover is one interface above your native coding harnesses. Start work, see what needs attention, answer a question and move a task to the harness with quota to spare, from your phone, on machines you own.',
  url: 'https://drover.fyi',
  repo: 'https://github.com/arniesaha/drover',
  license: 'Apache-2.0',
};

const blob = `${SITE.repo}/blob/main`;

export const LINKS = {
  repo: SITE.repo,
  issues: `${SITE.repo}/issues`,
  goodFirstIssues: `${SITE.repo}/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22`,
  helpWanted: `${SITE.repo}/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22`,
  releases: `${SITE.repo}/releases`,
  changelog: `${blob}/CHANGELOG.md`,
  contributing: `${blob}/CONTRIBUTING.md`,
  securityPolicy: `${blob}/SECURITY.md`,
  license: `${blob}/LICENSE`,
  installScript: 'https://raw.githubusercontent.com/arniesaha/drover/main/install.sh',
  // The launch article this site takes its story from, and the earlier post it builds on.
  article: 'https://x.com/arniesaha/status/2088854301087289783',
  shippingOnTheGo: 'https://www.arnabsaha.com/posts/shipping-on-the-go',
  herdr: 'https://herdr.dev/',
  doc: (path: string) => `${blob}/docs/${path}`,
  file: (path: string) => `${blob}/${path}`,
};

export const NAV = [
  { href: '/features/', label: 'Features' },
  { href: '/use-cases/', label: 'Use cases' },
  { href: '/architecture/', label: 'Architecture' },
  { href: '/docs/', label: 'Docs' },
  { href: '/faq/', label: 'FAQ' },
  { href: '/contribute/', label: 'Contribute' },
];
