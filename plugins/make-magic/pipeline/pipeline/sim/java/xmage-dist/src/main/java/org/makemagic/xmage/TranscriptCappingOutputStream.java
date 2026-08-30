package org.makemagic.xmage;

import java.io.FilterOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/**
 * T3 — a HARD per-game transcript BYTE CAP: a defense-in-depth disk backstop around the
 * per-game transcript stream. A base XMage livelock once wrote an 84 MB / 1.7M-line transcript
 * before anything stopped it; the T2 wall-clock deadline now bounds that INDIRECTLY (it ends the
 * game at ~budget), but there is no EXPLICIT ceiling on the transcript FILE. A slow-but-legal
 * long game — or any future loop the deadline is slow to catch — can still pressure disk. This
 * wrapper caps the file directly.
 *
 * <p>Semantics: forward writes to the delegate until the running byte count STRICTLY exceeds the
 * budget; at that point write ONE truncation-marker line and DISCARD every further byte (never
 * buffer them). Capping the log NEVER ends the game or changes its outcome — the game keeps
 * playing to its RESULT; only transcript bytes are dropped. A fresh instance is created per game
 * (via {@link #capForBudget}), so the counter resets per game, not per worker lifetime.
 *
 * <p>A budget {@code <= 0} means "no cap": {@link #capForBudget} returns the RAW delegate
 * unwrapped, preserving byte-identical prior behavior (no counting, no marker) — mirroring how
 * the sibling tunables gate to stock behavior for the CI-reproducible jar.
 *
 * <p>Intentionally dependency-free (JDK only) so it is unit-testable without the XMage reactor.
 */
final class TranscriptCappingOutputStream extends FilterOutputStream {

    private final long budget;
    private long written;
    private boolean truncated;

    TranscriptCappingOutputStream(OutputStream delegate, long budget) {
        super(delegate);
        this.budget = budget;
    }

    /**
     * Wrap {@code delegate} with a per-game byte cap, OR — for a budget {@code <= 0} — return the
     * RAW delegate unwrapped (no cap; byte-identical prior behavior). Returns {@link OutputStream}
     * so both branches are drop-in for the caller's stream chain.
     */
    static OutputStream capForBudget(OutputStream delegate, long budget) {
        return budget <= 0 ? delegate : new TranscriptCappingOutputStream(delegate, budget);
    }

    /** The single truncation-marker line emitted once the budget is exceeded. */
    static String markerText(long budget) {
        return "\n--- TRANSCRIPT TRUNCATED AT " + budget + " BYTES ---\n";
    }

    @Override
    public void write(int b) throws IOException {
        if (truncated) {
            return; // discard, never buffer
        }
        if (written < budget) {
            out.write(b);
            written++;
        } else {
            trip();
        }
    }

    @Override
    public void write(byte[] b, int off, int len) throws IOException {
        if (truncated || len <= 0) {
            return; // discard, never buffer
        }
        long remaining = budget - written;
        if (len <= remaining) {
            out.write(b, off, len);
            written += len;
        } else {
            if (remaining > 0) {
                out.write(b, off, (int) remaining);
                written += remaining;
            }
            trip();
        }
    }

    /** Emit the marker line ONCE, then latch into discard mode. */
    private void trip() throws IOException {
        out.write(markerText(budget).getBytes(StandardCharsets.UTF_8));
        out.flush();
        truncated = true;
    }
}
