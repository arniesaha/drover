// Synthetic session history for the recall demo. None of this is real data:
// the repos, hosts and findings are invented for illustration.
export interface DemoSession {
  id: string;
  title: string;
  repo: string;
  host: string;
  harness: string;
  when: string;
  ageDays: number;
  summary: string;
  loops: string[];
  files: string[];
}

export const SESSIONS: DemoSession[] = [
  {
    id: 's-1042',
    title: 'Drop the Redis cache from the quote path',
    repo: 'acme/payments',
    host: 'studio',
    harness: 'codex',
    when: '2 days ago',
    ageDays: 2,
    summary:
      'Removed the Redis cache in front of quote lookups. It hid stale prices after a currency update and saved under 4 ms. Replaced it with an in-process LRU keyed by rate version.',
    loops: ['Delete the unused Redis config block once staging has soaked'],
    files: ['src/quotes/cache.py', 'src/quotes/service.py', 'tests/test_quotes.py'],
  },
  {
    id: 's-1037',
    title: 'Fix the flaky retry test',
    repo: 'acme/payments',
    host: 'laptop',
    harness: 'claude-code',
    when: '3 days ago',
    ageDays: 3,
    summary:
      'The retry test failed about one run in twelve because jitter was not seeded. Seeded the RNG in the fixture and tightened the backoff assertion.',
    loops: ['Retry logic still sleeps for real in one integration test'],
    files: ['src/retry.py', 'tests/test_retry.py', 'tests/conftest.py'],
  },
  {
    id: 's-1029',
    title: 'Webhook signature verification',
    repo: 'acme/payments',
    host: 'studio',
    harness: 'claude-code',
    when: '6 days ago',
    ageDays: 6,
    summary:
      'Added constant-time signature checks for inbound webhooks and a replay window of five minutes. Rejected requests are counted but not logged with their body.',
    loops: ['Decide whether to rotate the signing key on a schedule'],
    files: ['src/webhooks/verify.py', 'tests/test_webhooks.py'],
  },
  {
    id: 's-1018',
    title: 'Paginate the export endpoint',
    repo: 'acme/ledger-api',
    host: 'buildbox',
    harness: 'codex',
    when: '8 days ago',
    ageDays: 8,
    summary:
      'Switched /export from offset to cursor pagination after a timeout on large accounts. Kept the old parameter working behind a deprecation warning.',
    loops: ['Remove the offset parameter after the next client release'],
    files: ['src/export/handler.py', 'src/export/cursor.py', 'docs/export.md'],
  },
  {
    id: 's-1011',
    title: 'Config loader migration to TOML',
    repo: 'acme/ledger-api',
    host: 'laptop',
    harness: 'codex',
    when: '11 days ago',
    ageDays: 11,
    summary:
      'Moved configuration from scattered environment reads to one TOML loader with typed defaults. An empty config file now fails loudly instead of starting with nulls.',
    loops: [],
    files: ['src/config/loader.py', 'src/config/defaults.py', 'tests/test_config.py'],
  },
  {
    id: 's-0996',
    title: 'Retry budget for the payout worker',
    repo: 'acme/payments',
    host: 'buildbox',
    harness: 'claude-code',
    when: '14 days ago',
    ageDays: 14,
    summary:
      'Capped payout retries at five with exponential backoff and added a dead-letter table. Decided against a Redis queue: Postgres SKIP LOCKED was enough at this volume.',
    loops: ['Alert when the dead-letter table grows past 50 rows'],
    files: ['src/payouts/worker.py', 'src/retry.py', 'migrations/0042_dead_letter.sql'],
  },
  {
    id: 's-0981',
    title: 'Speed up the CI test matrix',
    repo: 'acme/ledger-api',
    host: 'studio',
    harness: 'codex',
    when: '17 days ago',
    ageDays: 17,
    summary:
      'Split slow integration tests into their own job and cached the dependency layer. Wall clock dropped from eleven minutes to four.',
    loops: ['Two tests are still order dependent'],
    files: ['.github/workflows/ci.yml', 'tests/integration/conftest.py'],
  },
  {
    id: 's-0964',
    title: 'Onboarding copy for the mobile app',
    repo: 'acme/mobile',
    host: 'laptop',
    harness: 'claude-code',
    when: '21 days ago',
    ageDays: 21,
    summary:
      'Rewrote the three onboarding screens to explain pairing before asking for camera access. Shortened every screen to one sentence and one action.',
    loops: ['Screenshots in the store listing are out of date'],
    files: ['app/Onboarding/PairingView.swift', 'app/Onboarding/Copy.strings'],
  },
];

export const PRESETS = [
  'why did we drop the redis cache?',
  'what is still open on payments?',
  'where did I touch the retry logic?',
  'export pagination',
];
