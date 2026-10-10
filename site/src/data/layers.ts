// "Where Drover fits": the capability-layer model behind the ecosystem diagram.
//
// This file is the single source for that diagram (components/LayerStack.astro)
// on both the landing page and the architecture page. The model is expected to
// evolve: edit the layers, statuses and examples here and both views follow.
//
// Status vocabulary, used honestly:
//   shipped      in a current release
//   in-progress  partly shipped, or the direction being built toward
//   roadmap      not built yet
//   yours        not part of Drover; you bring it

export type Status = 'shipped' | 'in-progress' | 'roadmap' | 'yours';

export const STATUS_LABEL: Record<Status, string> = {
  shipped: 'Shipped',
  'in-progress': 'In progress',
  roadmap: 'Roadmap',
  yours: 'You bring it',
};

export interface LayerItem {
  text: string;
  status?: Status;
}

export interface Layer {
  id: string;
  name: string;
  /** Who owns the layer. Drover's layers are drawn as one group. */
  owner: 'drover' | 'ecosystem';
  status: Status;
  /** One line, shown on hover or tap. */
  summary: string;
  /** Short phrase shown on the layer itself. */
  short: string;
  items: LayerItem[];
  /** Interface on the boundary directly below this layer, if there is one. */
  boundaryBelow?: string[];
}

// Top to bottom.
export const LAYERS: Layer[] = [
  {
    id: 'interaction',
    name: 'Interaction',
    owner: 'ecosystem',
    status: 'shipped',
    short: 'Where people act',
    summary: 'Where you make the next decision: the phone, a chat or a terminal.',
    items: [
      { text: 'Drover iOS app (source build, beta)', status: 'shipped' },
      { text: 'Drover web cockpit', status: 'shipped' },
      { text: 'Chat through an orchestrator', status: 'yours' },
      { text: 'Terminal, also from the app', status: 'shipped' },
    ],
  },
  {
    id: 'orchestration',
    name: 'Orchestration',
    owner: 'ecosystem',
    status: 'yours',
    short: 'Agents that plan and delegate',
    summary: 'Agents that plan work and delegate it. They use Drover through MCP and the HTTP API.',
    items: [
      { text: 'OpenClaw', status: 'yours' },
      { text: 'Hermes', status: 'yours' },
      { text: 'Any other agent, script or CI job', status: 'yours' },
    ],
    boundaryBelow: ['MCP', 'HTTP API'],
  },
  {
    id: 'continuity',
    name: 'Continuity',
    owner: 'drover',
    status: 'in-progress',
    short: 'The bridge between planning and doing',
    summary: 'Keeps work alive across restarts, hosts and harnesses. Partly shipped. This is the direction of the project.',
    items: [
      { text: 'Session identity across restarts and hosts', status: 'shipped' },
      { text: 'Handoff between harnesses', status: 'shipped' },
      { text: 'Wake-ups and status back to the orchestrator', status: 'in-progress' },
      { text: 'Goal-level progress', status: 'roadmap' },
    ],
  },
  {
    id: 'context',
    name: 'Context',
    owner: 'drover',
    status: 'shipped',
    short: 'What happened, kept and queryable',
    summary: 'The durable record of each session, and the tools that return it to the next session.',
    items: [
      { text: 'Event history', status: 'shipped' },
      { text: 'Recall and search', status: 'shipped' },
      { text: 'Session summaries (opt-in)', status: 'shipped' },
      { text: 'Handoff and project briefs', status: 'shipped' },
      { text: 'Portable profile with privacy tiers', status: 'shipped' },
    ],
  },
  {
    id: 'execution',
    name: 'Execution',
    owner: 'drover',
    status: 'shipped',
    short: 'Hub routing, a daemon on each host',
    summary: 'The hub routes each operation. A daemon on each host owns the sessions, processes and files.',
    items: [
      { text: 'Hub routing to direct and relay hosts', status: 'shipped' },
      { text: 'Sessions and worktrees on the host daemon', status: 'shipped' },
      { text: 'Approvals (Claude Code today) and interrupts', status: 'shipped' },
      { text: 'A structured adapter for each harness, and a terminal', status: 'shipped' },
    ],
    boundaryBelow: ['Adapters'],
  },
  {
    id: 'harnesses',
    name: 'Harnesses and compute',
    owner: 'ecosystem',
    status: 'yours',
    short: 'Native harnesses on machines you own',
    summary: 'Each native harness keeps its own models, sign-in, tools and sandbox. It runs on a machine you own.',
    items: [
      { text: 'Claude Code', status: 'yours' },
      { text: 'Codex', status: 'yours' },
      { text: 'Antigravity', status: 'yours' },
      { text: 'DeepSeek Harness', status: 'yours' },
      { text: 'More as adapters are added', status: 'roadmap' },
    ],
  },
];

// A side rail that spans several layers.
export const RAIL = {
  id: 'governance',
  name: 'Capacity and governance',
  /** ids of the first and last layer the rail spans, inclusive */
  from: 'continuity',
  to: 'harnesses',
  status: 'in-progress' as Status,
  short: 'Quota, cost, credentials, residency',
  summary: 'The limits and rules across the stack: remaining quota, cost, credentials and data location.',
  items: [
    { text: 'Provider quota windows', status: 'shipped' },
    { text: 'Measured cost vs subscription usage', status: 'shipped' },
    { text: 'Credentials and scopes', status: 'shipped' },
    { text: 'Privacy and data residency', status: 'shipped' },
    { text: 'Audit', status: 'roadmap' },
  ] as LayerItem[],
};
