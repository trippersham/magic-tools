// Live metadata band (bug fix). The band was server-rendered from the file
// baseline and never reacted to user re-tags. This recomputes Current / Proposed
// / Adds / Drops / Changes from the LIVE curation state on every change and
// re-renders the band, mirroring MetadataBand.astro's markup.

import {
  stats,
  delta,
  CURVE_BUCKETS,
  type Stats,
  type StatsDelta,
  type MetaCard,
} from '../lib/metadata';
import { getMembership } from './store';

const PIP_ORDER = ['W', 'U', 'B', 'R', 'G', 'C'] as const;

type StatCard = MetaCard & { name: string };
let cards: StatCard[] = [];

function loadCards(): StatCard[] {
  const el = document.getElementById('cards-data');
  try {
    return JSON.parse(el?.textContent || '[]') as StatCard[];
  } catch {
    return [];
  }
}

function sign(n: number): string {
  const r = Math.round(n * 100) / 100;
  return r > 0 ? `+${r}` : `${r}`;
}
function maxCurve(...ss: Stats[]): number {
  let m = 1;
  for (const s of ss) for (const b of CURVE_BUCKETS) m = Math.max(m, s.curve[b] ?? 0);
  return m;
}
function negate(s: Stats): StatsDelta {
  const curve: Record<string, number> = {};
  for (const b of CURVE_BUCKETS) curve[b] = -(s.curve[b] ?? 0);
  return {
    curve,
    pips: { W: -s.pips.W, U: -s.pips.U, B: -s.pips.B, R: -s.pips.R, G: -s.pips.G, C: -s.pips.C },
    typeCounts: {},
    landCount: -s.landCount,
    avgCmc: -s.avgCmc,
    total: -s.total,
  };
}

function statGrid(total: number, lands: number, avg: number): string {
  return (
    `<dl class="stat-grid">` +
    `<div><dt>Cards</dt><dd>${total}</dd></div>` +
    `<div><dt>Lands</dt><dd>${lands}</dd></div>` +
    `<div><dt>Avg CMC</dt><dd>${avg}</dd></div>` +
    `</dl>`
  );
}
function signedGrid(d: { total: number; landCount: number; avgCmc: number }): string {
  const cell = (v: number) => `<dd class="${v >= 0 ? 'pos' : 'neg'}">${sign(v)}</dd>`;
  return (
    `<dl class="stat-grid">` +
    `<div><dt>Cards</dt>${cell(d.total)}</div>` +
    `<div><dt>Lands</dt>${cell(d.landCount)}</div>` +
    `<div><dt>Avg CMC</dt>${cell(d.avgCmc)}</div>` +
    `</dl>`
  );
}
function curveAbs(s: Stats, curveMax: number, barCls: string): string {
  return (
    `<div class="curve">` +
    CURVE_BUCKETS.map(
      (b) =>
        `<div class="curve-col"><div class="bar ${barCls}" style="height:${((s.curve[b] ?? 0) / curveMax) * 56}px"></div><span class="clabel">${b}</span></div>`,
    ).join('') +
    `</div>`
  );
}
function curveDelta(d: StatsDelta, curveMax: number): string {
  return (
    `<div class="curve">` +
    CURVE_BUCKETS.map((b) => {
      const v = d.curve[b] ?? 0;
      const h = (Math.abs(v) / curveMax) * 56;
      return `<div class="curve-col"><div class="bar ${v >= 0 ? 'bg-emerald-500' : 'bg-red-500'}" style="height:${h}px"></div><span class="clabel">${b}</span></div>`;
    }).join('') +
    `</div>`
  );
}
function pipsAbs(s: Stats): string {
  return (
    `<div class="pips">` +
    PIP_ORDER.filter((p) => s.pips[p] > 0)
      .map((p) => `<span class="pip">${p} ${s.pips[p]}</span>`)
      .join('') +
    `</div>`
  );
}
function pipsDelta(d: StatsDelta): string {
  return (
    `<div class="pips">` +
    PIP_ORDER.filter((p) => d.pips[p] !== 0)
      .map((p) => `<span class="pip ${d.pips[p] >= 0 ? 'pos' : 'neg'}">${p} ${sign(d.pips[p])}</span>`)
      .join('') +
    `</div>`
  );
}

function render(): void {
  const band = document.querySelector<HTMLElement>('.metadata-band');
  if (!band) return;

  // Bucket live: current = deck-as-is (untouched+drop); proposed = untouched+add.
  const current: MetaCard[] = [];
  const proposed: MetaCard[] = [];
  const adds: MetaCard[] = [];
  const drops: MetaCard[] = [];
  for (const c of cards) {
    const m = getMembership(c.name);
    if (m === 'untouched' || m === 'drop') current.push(c);
    if (m === 'untouched' || m === 'add') proposed.push(c);
    if (m === 'add') adds.push(c);
    if (m === 'drop') drops.push(c);
  }

  const cur = stats(current);
  const prop = stats(proposed);
  const net = delta(cur, prop);
  const addStats = stats(adds);
  const dropStats = negate(stats(drops));
  const curveMax = maxCurve(cur, prop);

  const activeView = document.documentElement.dataset.activeView ?? 'all';
  const hide = (key: string) => (key === activeView ? '' : ' hidden');

  const allPanel =
    `<div data-panel="all" class="grid gap-4 md:grid-cols-[1fr_1fr_auto]"${hide('all')}>` +
    `<div><h3 class="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-400">Current</h3>${statGrid(cur.total, cur.landCount, cur.avgCmc)}<div class="mt-2">${curveAbs(cur, curveMax, 'bg-zinc-500')}</div></div>` +
    `<div><h3 class="mb-1 text-xs font-semibold uppercase tracking-wide text-emerald-400">Proposed</h3>${statGrid(prop.total, prop.landCount, prop.avgCmc)}<div class="mt-2">${curveAbs(prop, curveMax, 'bg-emerald-500')}</div></div>` +
    `<div class="delta-col"><h3 class="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-400">Δ</h3>${signedGrid(net)}<div class="mt-2">${pipsDelta(net)}</div></div>` +
    `</div>`;

  const absPanel = (key: string, s: Stats, label: string) =>
    `<div data-panel="${key}"${hide(key)}><h3 class="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-400">${label}</h3>` +
    `<div class="flex flex-wrap items-end gap-6">${statGrid(s.total, s.landCount, s.avgCmc)}${curveAbs(s, curveMax, 'bg-zinc-500')}${pipsAbs(s)}</div></div>`;

  const deltaPanel = (key: string, d: StatsDelta, label: string) =>
    `<div data-panel="${key}"${hide(key)}><h3 class="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-400">${label}</h3>` +
    `<div class="flex flex-wrap items-end gap-6">${signedGrid(d)}${curveDelta(d, curveMax)}${pipsDelta(d)}</div></div>`;

  band.innerHTML =
    allPanel +
    absPanel('current', cur, 'Current deck') +
    absPanel('proposed', prop, 'Proposed deck') +
    deltaPanel('adds', { ...addStats } as StatsDelta, 'Adds — additive contribution') +
    deltaPanel('drops', dropStats, 'Drops — subtractive contribution') +
    deltaPanel('changes', net, 'All changes — net delta');
}

export function initMetadata(): void {
  cards = loadCards();
  render();
  document.addEventListener('curation-changed', render);
  // Re-render on view switch so the correct panel is shown with live numbers.
  document.querySelectorAll<HTMLElement>('[data-view-btn]').forEach((btn) => {
    btn.addEventListener('click', () => requestAnimationFrame(render));
  });
}
