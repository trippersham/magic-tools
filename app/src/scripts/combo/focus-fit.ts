// PURE block-wrap / fit math for the fit-aware FOCUS layout (Phase 7).
//
// The focus DAG is laid out left→right by rank (layers.ts). This module answers a
// single question for a given viewport: can the WHOLE DAG be shown at once as a
// block DAG (Mode A), where each layer that has too many cards to stack vertically
// wraps into multiple side-by-side sub-columns forming one rectangular BLOCK per
// layer? If yes it returns the per-layer column counts + total size; if no, the
// caller falls back to the one-layer-at-a-time pager (Mode B).
//
// It is intentionally DOM-free and unit-tested: it takes per-layer card COUNTS +
// DIMENSIONS (measured by graph-focus.ts from the real tiles, so mixed card sizes
// — min-size enabler floods vs. larger hub cards — are handled per layer) plus the
// available viewport box, and returns { perLayer, totalWidth, fits }.

/**
 * One layer's node count + the cell size to stack it in.
 *  • `cardH` is the layer's MAX card height (drives rowsPerCol + block height so the
 *    tallest card in a sub-column never overlaps its neighbour).
 *  • `cardW` is the layer's MAX card width — the fallback cell width.
 *  • `widths` (optional) is the per-node width in FILL ORDER (top-to-bottom then next
 *    sub-column). When present, each sub-column is only as wide as ITS OWN widest
 *    card, so one big hub card in a flood of min-size enablers no longer inflates
 *    every column. Length should equal `count`; missing entries fall back to `cardW`.
 */
export interface LayerDim {
  count: number;
  cardW: number;
  cardH: number;
  widths?: number[];
}

/** Per-sub-column widths for a layer given its fill order + rowsPerCol. */
function columnWidths(dim: LayerDim, rowsPerCol: number, nCols: number): number[] {
  const widths: number[] = [];
  for (let c = 0; c < nCols; c++) {
    let w = 0;
    const start = c * rowsPerCol;
    const end = Math.min(dim.count, start + rowsPerCol);
    for (let i = start; i < end; i++) {
      w = Math.max(w, dim.widths?.[i] ?? dim.cardW);
    }
    widths.push(w || dim.cardW);
  }
  return widths;
}

export interface FitOptions {
  /** Usable width  = viewport width  − 2×padding. */
  availW: number;
  /** Usable height = viewport height − 2×padding (header/legend already excluded). */
  availH: number;
  /** Minimum vertical gap between stacked cards in a block. */
  gap: number;
  /** Minimum horizontal gap between a block's sub-columns. */
  colGap: number;
  /** Horizontal gap between adjacent layer blocks (the rank separation). */
  layerGap: number;
}

export interface LayerBlock {
  /** Number of side-by-side sub-columns this layer wraps into. */
  nCols: number;
  /** Max cards stacked in a single sub-column (= min(count, rowsPerCol)). */
  rowsPerCol: number;
  /** Rendered block width  (nCols·cardW + (nCols−1)·colGap). */
  blockW: number;
  /** Rendered block height (rows·cardH + (rows−1)·gap, rows = min(count,rowsPerCol)). */
  blockH: number;
}

export interface FitResult {
  perLayer: LayerBlock[];
  /** Σ blockW + (numLayers−1)·layerGap. */
  totalWidth: number;
  /** Tallest layer block height (for a caller sanity check). */
  maxBlockH: number;
  /**
   * True iff the whole DAG fits BOTH ways at min card size:
   *   • every layer can stack ≥1 card in availH (viewport tall enough for a card),
   *   • every layer's block height ≤ availH,
   *   • totalWidth ≤ availW.
   * When false the caller uses the pager (Mode B).
   */
  fits: boolean;
}

/**
 * Compute the block-wrapped layout for a set of layers at a viewport, and whether
 * it fits. Pure: no DOM, no window. `rowsPerCol` is computed PER LAYER from that
 * layer's own card height, so a flood of short enabler cards stacks more per column
 * than a column of tall hub cards.
 */
export function computeBlockFit(layers: LayerDim[], opts: FitOptions): FitResult {
  const { availW, availH, gap, colGap, layerGap } = opts;
  const perLayer: LayerBlock[] = [];
  let totalWidth = 0;
  let maxBlockH = 0;
  let everyLayerHasRoom = true;

  for (const dim of layers) {
    const { count, cardH } = dim;
    const n = Math.max(0, count);
    // How many cards stack in one sub-column within availH (≥1 by clamp). If even a
    // single card doesn't fit the height, mark the layer as having no room.
    const rawRows = Math.floor(availH / (cardH + gap));
    if (rawRows < 1) everyLayerHasRoom = false;
    const rowsPerCol = Math.max(1, rawRows);
    const nCols = n <= 0 ? 0 : Math.ceil(n / rowsPerCol);
    const rows = Math.min(n, rowsPerCol);
    // Per-sub-column widths (each column only as wide as its own widest card).
    const colW = nCols <= 0 ? [] : columnWidths(dim, rowsPerCol, nCols);
    const blockW =
      nCols <= 0 ? 0 : colW.reduce((s, w) => s + w, 0) + (nCols - 1) * colGap;
    const blockH = rows <= 0 ? 0 : rows * cardH + (rows - 1) * gap;
    perLayer.push({ nCols, rowsPerCol, blockW, blockH });
    totalWidth += blockW;
    maxBlockH = Math.max(maxBlockH, blockH);
  }
  if (layers.length > 1) totalWidth += (layers.length - 1) * layerGap;

  const fits = everyLayerHasRoom && totalWidth <= availW && maxBlockH <= availH;
  return { perLayer, totalWidth, maxBlockH, fits };
}

/** A laid-out node in a Mode-A block: centre coords in an abstract left→right space. */
export interface BlockCoord {
  x: number;
  y: number;
}

/**
 * Assign analytic centre coordinates for a Mode-A block DAG. Each layer is a block
 * placed at its horizontal offset; within a block, nodes FILL top-to-bottom then
 * the next sub-column. Each sub-column is centred vertically about y=0, so short
 * trailing columns sit centred (matches the single-column layout's centring).
 *
 * `layers` is the rank-ordered node ids (layers.ts `layers`); `dims`/`blocks` are
 * per-layer and index-aligned to it. Returns id → {x,y} centre.
 */
export function computeBlockCoords(
  layers: string[][],
  dims: LayerDim[],
  blocks: LayerBlock[],
  opts: Pick<FitOptions, 'gap' | 'colGap' | 'layerGap'>,
): Record<string, BlockCoord> {
  const { gap, colGap, layerGap } = opts;
  const coords: Record<string, BlockCoord> = {};
  let layerX = 0;
  layers.forEach((layer, r) => {
    const dim = dims[r];
    const { cardH } = dim;
    const { rowsPerCol, blockW, nCols } = blocks[r];
    // Per-sub-column widths + their left offsets within the block.
    const colW = columnWidths(dim, rowsPerCol, Math.max(nCols, 1));
    const colX: number[] = [];
    let acc = 0;
    for (let c = 0; c < colW.length; c++) {
      colX.push(acc);
      acc += colW[c] + colGap;
    }
    layer.forEach((id, j) => {
      const col = Math.floor(j / rowsPerCol);
      const row = j % rowsPerCol;
      const rowsInCol = Math.min(rowsPerCol, layer.length - col * rowsPerCol);
      const colH = rowsInCol * cardH + (rowsInCol - 1) * gap;
      const cw = colW[col] ?? dim.cardW;
      const x = layerX + (colX[col] ?? 0) + cw / 2;
      const y = -colH / 2 + row * (cardH + gap) + cardH / 2;
      coords[id] = { x, y };
    });
    layerX += blockW + layerGap;
  });
  return coords;
}
