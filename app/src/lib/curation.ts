// Pure curation state machine (v2.8). No DOM, no storage — just the logic that
// can be wrong: the transition table, the 2-vs-3 donut geometry selection, and
// the export/keying serialization. Unit-tested in curation.test.ts.

export type Membership = 'untouched' | 'add' | 'drop' | 'consideration' | 'dismissed';
export type Action = 'remove' | 'add' | 'annotate' | 'dismiss';

export interface CardState {
  membership: Membership;
  note: string | null;
}

/**
 * The actions valid for a card in a given state (annotate is always valid for
 * anything draggable). Order is meaningful only as a stable list; geometry is
 * decided by donutSegments/hitTest.
 */
export function validActions(state: Membership): Action[] {
  switch (state) {
    case 'untouched':
      return ['remove', 'annotate'];
    case 'add':
      return ['remove', 'annotate'];
    case 'drop':
      return ['add', 'annotate'];
    case 'consideration':
      return ['add', 'dismiss', 'annotate'];
    case 'dismissed':
      return [];
  }
}

export interface ActionResult {
  membership: Membership;
  /** annotate opens a note input; membership is unchanged. */
  annotate?: boolean;
}

/**
 * Apply an action to a card state. Throws if the action is not valid for the
 * state (guards against UI bugs firing impossible transitions).
 */
export function applyAction(state: Membership, action: Action): ActionResult {
  if (!validActions(state).includes(action)) {
    throw new Error(`invalid action "${action}" for state "${state}"`);
  }
  if (action === 'annotate') return { membership: state, annotate: true };

  switch (state) {
    case 'untouched':
      // remove
      return { membership: 'drop' };
    case 'add':
      // remove → back to considerations
      return { membership: 'consideration' };
    case 'drop':
      // add → back to untouched (kept)
      return { membership: 'untouched' };
    case 'consideration':
      return action === 'add' ? { membership: 'add' } : { membership: 'dismissed' };
    default:
      throw new Error(`no transition for state "${state}"`);
  }
}

/* ------------------------------------------------------------------ *
 * Donut geometry
 * ------------------------------------------------------------------ */

export type Region = 'left' | 'right' | 'top-left' | 'top-right' | 'bottom';
export type SegColor = 'red' | 'green' | 'gray';

export interface Segment {
  action: Action;
  color: SegColor;
  label: string;
  region: Region;
}

const COLOR: Record<Action, SegColor> = {
  remove: 'red',
  add: 'green',
  dismiss: 'red',
  annotate: 'gray',
};
const LABEL: Record<Action, string> = {
  remove: 'Remove',
  add: 'Add',
  dismiss: 'Dismiss',
  annotate: 'Note',
};

/**
 * The donut segment layout for a state. Positioning is SEMANTIC and consistent
 * with the color convention:
 * - Add / Include actions always sit on the RIGHT (green).
 * - Remove / Dismiss actions always sit on the LEFT (red).
 * - 2 valid actions → the add/remove action takes its semantic side, annotate the other.
 * - 3 (consideration only) → fixed dismiss red top-left, add green top-right,
 *   annotate gray bottom.
 */
export function donutSegments(state: Membership): Segment[] {
  const actions = validActions(state);
  if (actions.length === 0) return [];

  if (state === 'consideration') {
    return [
      { action: 'dismiss', color: COLOR.dismiss, label: LABEL.dismiss, region: 'top-left' },
      { action: 'add', color: COLOR.add, label: LABEL.add, region: 'top-right' },
      { action: 'annotate', color: COLOR.annotate, label: LABEL.annotate, region: 'bottom' },
    ];
  }

  // 2-option: the non-annotate action takes its SEMANTIC side (add → right,
  // remove/dismiss → left); annotate takes whatever half is left.
  const priority = actions.find((a) => a !== 'annotate')!;
  const priorityRegion: Region = priority === 'add' ? 'right' : 'left';
  const annotateRegion: Region = priorityRegion === 'right' ? 'left' : 'right';
  return [
    { action: priority, color: COLOR[priority], label: LABEL[priority], region: priorityRegion },
    { action: 'annotate', color: COLOR.annotate, label: LABEL.annotate, region: annotateRegion },
  ];
}

export interface DonutGeometry {
  radius: number;
  deadzone: number;
}

/**
 * Given a pointer offset (dx, dy) from the donut center — screen coords, +y
 * pointing down — return the action for the segment it lands in, or null for a
 * dead-center (cancel) release.
 */
export function hitTest(
  state: Membership,
  dx: number,
  dy: number,
  geo: DonutGeometry,
): Action | null {
  const segs = donutSegments(state);
  if (segs.length === 0) return null;
  const r = Math.hypot(dx, dy);
  if (r < geo.deadzone) return null;

  const is3 = segs.some((s) => s.region === 'top-left');
  let region: Region;
  if (is3) {
    region = dy > 0 ? 'bottom' : dx < 0 ? 'top-left' : 'top-right';
  } else {
    region = dx < 0 ? 'left' : 'right';
  }
  return segs.find((s) => s.region === region)?.action ?? null;
}

/* ------------------------------------------------------------------ *
 * Export serialization
 * ------------------------------------------------------------------ */

export interface CurationExport {
  adds: string[];
  drops: string[];
  considerations: string[];
  dismissed: string[];
  notes: Record<string, string>;
}

/** Serialize the resolved curation state into the export/handoff shape. */
export function buildExport(states: Record<string, CardState>): CurationExport {
  const out: CurationExport = { adds: [], drops: [], considerations: [], dismissed: [], notes: {} };
  for (const [name, s] of Object.entries(states)) {
    switch (s.membership) {
      case 'add':
        out.adds.push(name);
        break;
      case 'drop':
        out.drops.push(name);
        break;
      case 'consideration':
        out.considerations.push(name);
        break;
      case 'dismissed':
        out.dismissed.push(name);
        break;
    }
    if (s.note && s.note.trim()) out.notes[name] = s.note;
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * Initial state + baseline keying
 * ------------------------------------------------------------------ */

interface InitialSource {
  deck: { name: string }[];
  adds: { name: string }[];
  considerations: { name: string }[];
  changeset: { adds: string[]; drops: string[]; considerations: string[] };
}

/** Derive the baseline curation state from the enriched file shape. */
export function buildInitialState(src: InitialSource): Record<string, CardState> {
  const st: Record<string, CardState> = {};
  const dropSet = new Set(src.changeset.drops);
  for (const c of src.deck) {
    st[c.name] = { membership: dropSet.has(c.name) ? 'drop' : 'untouched', note: null };
  }
  for (const c of src.adds) st[c.name] = { membership: 'add', note: null };
  for (const c of src.considerations) st[c.name] = { membership: 'consideration', note: null };
  return st;
}

interface HashableChangeset {
  adds: string[];
  drops: string[];
  considerations: string[];
}

/** Stable, order-independent hash of the baseline changeset (FNV-1a hex). */
export function changesetHash(cs: HashableChangeset): string {
  const canon = JSON.stringify({
    adds: [...cs.adds].sort(),
    drops: [...cs.drops].sort(),
    considerations: [...(cs.considerations ?? [])].sort(),
  });
  let h = 0x811c9dc5;
  for (let i = 0; i < canon.length; i++) {
    h ^= canon.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, '0');
}

/** localStorage key: deck name + baseline hash (so a data change invalidates). */
export function storageKey(deckName: string, cs: HashableChangeset): string {
  return `deck-diff:v2:${deckName}:${changesetHash(cs)}`;
}
