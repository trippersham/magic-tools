package org.makemagic.xmage;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.stream.Stream;

/**
 * LOAD-TIME forbidden-API scan for in-search quad drivers (defense in depth — the Python gate
 * {@code pipeline.sim.driver_lint} is layer 1; this is layer 2).
 *
 * <p>A make-magic quad driver may only enqueue LEGAL game actions; every terminal state must
 * come from the rules engine. A driver that reaches for a game/player TERMINAL API is asserting
 * a win it never played (the {@code MACRO_FIRE_REAL -> gameOver} on turn 1 pathology). Before
 * the worker instantiates a driver's class, it scans every compiled {@code .class} on that
 * driver's classpath dir for forbidden {@code Methodref} references — reading the constant pool
 * directly (no ASM dependency; the {@code .class} format is self-describing).</p>
 *
 * <p>FAIL denylist (owner-class-name SUBSTRING, so concrete subclasses like {@code GameImpl}
 * are caught too):
 * <ul>
 *   <li>{@code mage/players/Player}.{lost, won, leave, quit, setLosses, setWins}</li>
 *   <li>{@code mage/game/Game}.{end, setWinner}</li>
 *   <li>{@code concede} (any owner) — a driver-forced opponent concession is an
 *       engine-legitimate, aggregator-credited decisive win; drivers may not concede at all.</li>
 * </ul>
 * A forbidden reference makes the worker REFUSE to load the driver and emit a terminal
 * {@code RESULT reason=driver-rejected} (non-decisive, no requeue) rather than crash-looping.</p>
 */
final class DriverClassLint {

    private DriverClassLint() {
    }

    /** owner-substring -> forbidden method names (terminal-state / victory-assertion APIs). */
    private static final Map<String, Set<String>> FAIL_DENYLIST = new TreeMap<>();

    static {
        FAIL_DENYLIST.put("mage/players/Player",
                new TreeSet<>(Set.of("lost", "won", "leave", "quit", "setLosses", "setWins")));
        FAIL_DENYLIST.put("mage/game/Game", new TreeSet<>(Set.of("end", "setWinner")));
    }

    // Constant-pool tags (JVMS 4.4).
    private static final int TAG_UTF8 = 1;
    private static final int TAG_INTEGER = 3;
    private static final int TAG_FLOAT = 4;
    private static final int TAG_LONG = 5;
    private static final int TAG_DOUBLE = 6;
    private static final int TAG_CLASS = 7;
    private static final int TAG_STRING = 8;
    private static final int TAG_FIELDREF = 9;
    private static final int TAG_METHODREF = 10;
    private static final int TAG_INTERFACE_METHODREF = 11;
    private static final int TAG_NAME_AND_TYPE = 12;
    private static final int TAG_METHOD_HANDLE = 15;
    private static final int TAG_METHOD_TYPE = 16;
    private static final int TAG_DYNAMIC = 17;
    private static final int TAG_INVOKE_DYNAMIC = 18;
    private static final int TAG_MODULE = 19;
    private static final int TAG_PACKAGE = 20;

    /**
     * Scan every {@code .class} under {@code classesDir} and return the human-readable forbidden
     * references found (empty ⇒ clean). Each entry reads e.g. {@code "BadDriver: Player.lost"}.
     * A missing/empty dir yields no findings (nothing to reject).
     */
    static List<String> scanDir(Path classesDir) {
        List<String> findings = new ArrayList<>();
        if (classesDir == null || !Files.isDirectory(classesDir)) {
            return findings;
        }
        try (Stream<Path> walk = Files.walk(classesDir)) {
            List<Path> classes = walk
                    .filter(p -> p.toString().endsWith(".class"))
                    .sorted()
                    .toList();
            for (Path cls : classes) {
                byte[] data;
                try {
                    data = Files.readAllBytes(cls);
                } catch (IOException ioe) {
                    continue; // an unreadable .class is not a lint pass, but cannot be scanned here.
                }
                String name = cls.getFileName().toString();
                if (name.endsWith(".class")) {
                    name = name.substring(0, name.length() - ".class".length());
                }
                for (String[] ref : forbiddenRefs(data)) {
                    findings.add(name + ": " + shortOwner(ref[0]) + "." + ref[1]);
                }
            }
        } catch (IOException ioe) {
            // A dir-walk failure cannot prove the tree clean; surface it as a finding so the
            // worker refuses rather than silently loading an unscanned driver.
            findings.add("<scan-error>: " + ioe);
        }
        return findings;
    }

    private static String shortOwner(String internal) {
        int slash = internal.lastIndexOf('/');
        return slash < 0 ? internal : internal.substring(slash + 1);
    }

    /** Parse one {@code .class} blob's constant pool → the forbidden {@code {owner, method}} refs. */
    private static List<String[]> forbiddenRefs(byte[] d) {
        List<String[]> out = new ArrayList<>();
        if (d.length < 10 || (d[0] & 0xFF) != 0xCA || (d[1] & 0xFF) != 0xFE
                || (d[2] & 0xFF) != 0xBA || (d[3] & 0xFF) != 0xBE) {
            return out; // not a .class (bad magic) — nothing to scan.
        }
        int count = u2(d, 8);
        Map<Integer, String> utf8 = new TreeMap<>();
        Map<Integer, Integer> classNameIdx = new TreeMap<>();
        Map<Integer, int[]> nameAndType = new TreeMap<>();
        List<int[]> methodrefs = new ArrayList<>();

        int off = 10;
        int i = 1;
        try {
            while (i < count) {
                int tag = d[off] & 0xFF;
                off += 1;
                switch (tag) {
                    case TAG_UTF8: {
                        int len = u2(d, off);
                        off += 2;
                        utf8.put(i, new String(d, off, len, java.nio.charset.StandardCharsets.UTF_8));
                        off += len;
                        break;
                    }
                    case TAG_INTEGER:
                    case TAG_FLOAT:
                    case TAG_FIELDREF:
                    case TAG_DYNAMIC:
                    case TAG_INVOKE_DYNAMIC:
                        off += 4;
                        break;
                    case TAG_METHODREF:
                    case TAG_INTERFACE_METHODREF:
                        methodrefs.add(new int[] {u2(d, off), u2(d, off + 2)});
                        off += 4;
                        break;
                    case TAG_NAME_AND_TYPE:
                        nameAndType.put(i, new int[] {u2(d, off), u2(d, off + 2)});
                        off += 4;
                        break;
                    case TAG_LONG:
                    case TAG_DOUBLE:
                        off += 8;
                        i += 1; // 8-byte constants take two pool slots (JVMS 4.4.5).
                        break;
                    case TAG_CLASS:
                        classNameIdx.put(i, u2(d, off));
                        off += 2;
                        break;
                    case TAG_STRING:
                    case TAG_METHOD_TYPE:
                    case TAG_MODULE:
                    case TAG_PACKAGE:
                        off += 2;
                        break;
                    case TAG_METHOD_HANDLE:
                        off += 3;
                        break;
                    default:
                        return out; // unknown tag — cannot safely walk further; scanned what we could.
                }
                i += 1;
            }
        } catch (ArrayIndexOutOfBoundsException aioobe) {
            return out; // truncated/malformed pool — scanned what parsed cleanly.
        }

        for (int[] mr : methodrefs) {
            Integer cnIdx = classNameIdx.get(mr[0]);
            String owner = cnIdx == null ? null : utf8.get(cnIdx);
            int[] nt = nameAndType.get(mr[1]);
            String method = nt == null ? null : utf8.get(nt[0]);
            if (owner == null || method == null) {
                continue;
            }
            for (Map.Entry<String, Set<String>> e : FAIL_DENYLIST.entrySet()) {
                if (owner.contains(e.getKey()) && e.getValue().contains(method)) {
                    out.add(new String[] {owner, method});
                }
            }
            // concede is FAIL on ANY owner: a driver forcing the OPPONENT to concede yields an
            // engine-legitimate, aggregator-CREDITED decisive win (a concession is a rules-legal
            // loss). Drivers may not concede at all — safety wins over the theoretical self-concede.
            if ("concede".equals(method)) {
                out.add(new String[] {owner, method});
            }
        }
        return out;
    }

    private static int u2(byte[] d, int off) {
        return ((d[off] & 0xFF) << 8) | (d[off + 1] & 0xFF);
    }
}
