package org.makemagic.xmage;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;

/**
 * Dependency-free (JDK-only) unit test for {@link TranscriptCappingOutputStream} — the T3
 * hard per-game transcript BYTE CAP backstop. Run as a plain {@code main} (no JUnit on the
 * harness classpath); a thrown {@link AssertionError} exits non-zero. Compiled + executed by
 * {@code tests/test_xmage_transcript_cap.py::test_capping_outputstream_unit}.
 *
 * Proves, at the OutputStream level (no XMage/reactor needed):
 *   (a) with a small budget, a stream that would write a LOT yields at most
 *       {@code budget + markerBytes} to the delegate, ends with the truncation marker, and the
 *       marker appears EXACTLY once; and
 *   (b) with budget {@code <= 0} the wrapper is never constructed by the worker — asserted
 *       instead by {@link TranscriptCappingOutputStream#capForBudget} returning the RAW delegate
 *       (byte-identical prior behavior: no counting, no marker).
 */
public final class TranscriptCappingOutputStreamTest {

    private static void check(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    public static void main(String[] args) throws Exception {
        testBoundedWithMarker();
        testMarkerWrittenExactlyOnce();
        testSplitWriteCappedAtBudget();
        testDisabledBudgetReturnsRawDelegate();
        testExactBudgetNoMarker();
        System.out.println("TranscriptCappingOutputStreamTest OK");
    }

    /** (a) A firehose of writes past a tiny budget stays bounded and ends with the marker. */
    private static void testBoundedWithMarker() throws Exception {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        final int budget = 1024;
        TranscriptCappingOutputStream cap = new TranscriptCappingOutputStream(sink, budget);
        byte[] line = "the game keeps playing while the transcript is discarded\n"
                .getBytes(StandardCharsets.UTF_8);
        for (int i = 0; i < 10_000; i++) {
            cap.write(line);
        }
        cap.flush();
        byte[] out = sink.toByteArray();
        String marker = TranscriptCappingOutputStream.markerText(budget);
        int markerLen = marker.getBytes(StandardCharsets.UTF_8).length;
        check(out.length <= budget + markerLen,
                "delegate exceeded budget+marker: " + out.length + " > " + (budget + markerLen));
        check(out.length >= budget,
                "delegate should hold up to the budget before truncating, got " + out.length);
        String tail = new String(out, StandardCharsets.UTF_8);
        check(tail.endsWith(marker), "output must end with the truncation marker; tail=" + tail.substring(Math.max(0, tail.length() - 80)));
    }

    /** The marker must be emitted ONCE even under a storm of post-cap writes. */
    private static void testMarkerWrittenExactlyOnce() throws Exception {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        final int budget = 256;
        TranscriptCappingOutputStream cap = new TranscriptCappingOutputStream(sink, budget);
        for (int i = 0; i < 5000; i++) {
            cap.write(('a' + (i % 26)));
        }
        cap.flush();
        String s = new String(sink.toByteArray(), StandardCharsets.UTF_8);
        String needle = "TRANSCRIPT TRUNCATED AT";
        int first = s.indexOf(needle);
        check(first >= 0, "marker missing");
        check(s.indexOf(needle, first + 1) < 0, "marker written more than once");
    }

    /** A single oversized array write is truncated at exactly the remaining budget. */
    private static void testSplitWriteCappedAtBudget() throws Exception {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        final int budget = 100;
        TranscriptCappingOutputStream cap = new TranscriptCappingOutputStream(sink, budget);
        byte[] big = new byte[500];
        java.util.Arrays.fill(big, (byte) 'x');
        cap.write(big);
        cap.write(big); // fully discarded
        cap.flush();
        byte[] out = sink.toByteArray();
        String marker = TranscriptCappingOutputStream.markerText(budget);
        // Exactly budget payload bytes ('x') then the marker.
        int xCount = 0;
        for (byte b : out) {
            if (b == 'x') {
                xCount++;
            }
        }
        check(xCount == budget, "expected exactly budget payload bytes, got " + xCount);
        check(new String(out, StandardCharsets.UTF_8).endsWith(marker), "must end with marker");
    }

    /** (b) Disabled budget (<=0): the factory hands back the RAW delegate — no wrapping. */
    private static void testDisabledBudgetReturnsRawDelegate() {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        check(TranscriptCappingOutputStream.capForBudget(sink, 0L) == sink,
                "budget 0 must return the raw delegate (byte-identical prior behavior)");
        check(TranscriptCappingOutputStream.capForBudget(sink, -1L) == sink,
                "negative budget must return the raw delegate");
        check(TranscriptCappingOutputStream.capForBudget(sink, 64L) instanceof TranscriptCappingOutputStream,
                "positive budget must wrap");
    }

    /** Writing EXACTLY the budget must NOT trip the marker (only strictly-exceeding does). */
    private static void testExactBudgetNoMarker() throws Exception {
        ByteArrayOutputStream sink = new ByteArrayOutputStream();
        final int budget = 64;
        TranscriptCappingOutputStream cap = new TranscriptCappingOutputStream(sink, budget);
        byte[] exact = new byte[budget];
        java.util.Arrays.fill(exact, (byte) 'y');
        cap.write(exact);
        cap.flush();
        String s = new String(sink.toByteArray(), StandardCharsets.UTF_8);
        check(s.length() == budget, "exact-budget write should not add a marker; len=" + s.length());
        check(!s.contains("TRUNCATED"), "no marker at exactly the budget");
    }
}
