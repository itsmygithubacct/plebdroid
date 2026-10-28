#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Exercise document persistence, name collisions, containment, and failed imports."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = r'''
package io.github.itsmygithubacct.plebdroid;
import java.io.*;
import java.nio.file.*;
import java.util.*;
public final class DocumentStoreTest {
    public static void main(String[] args) throws Exception {
        Path files = Path.of(args[0]);
        Files.createDirectories(files);
        DocumentStore store = new DocumentStore(files.toFile());
        byte[] content = "hello π 世界\n".getBytes("UTF-8");
        String first = store.importFile(new ByteArrayInputStream(content), "../../notes.txt");
        String second = store.importFile(new ByteArrayInputStream(new byte[]{42}), "notes.txt");
        check(first.equals("notes.txt") && second.equals("notes (2).txt"), "collision/traversal naming");
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        new DocumentStore(files.toFile()).exportFile(first, out);
        check(Arrays.equals(content, out.toByteArray()), "persistent byte round trip");
        check(store.list().equals(Arrays.asList(second, first)), "document listing");
        try {
            store.importFile(new InputStream() { public int read() throws IOException { throw new IOException("broken provider"); } }, "broken.txt");
            throw new AssertionError("failed provider was accepted");
        } catch (IOException expected) { }
        check(store.list().size() == 2, "partial import visible");
        try (var paths = Files.list(files.resolve("home/Documents"))) {
            check(paths.noneMatch(p -> p.getFileName().toString().startsWith(".import-")), "staging file leaked");
        }
        Path outside = files.resolve("outside.txt");
        Files.writeString(outside, "outside");
        Files.createSymbolicLink(files.resolve("home/Documents/link"), outside);
        for (String name : Arrays.asList("../../outside.txt", "link")) {
            try { store.exportFile(name, new ByteArrayOutputStream()); throw new AssertionError("escaped document root"); }
            catch (IOException expected) { }
        }
        check(store.list().size() == 2, "export list followed a symlink");
        Path documents = files.resolve("home/Documents");
        Path backing = documents.resolve(".l2s.backing");
        Files.write(backing, content);
        Files.createSymbolicLink(documents.resolve("debian.txt"), backing.getFileName());
        Files.createSymbolicLink(documents.resolve("debian-copy.txt"), Path.of("debian.txt"));
        out.reset();
        store.exportFile("debian-copy.txt", out);
        check(Arrays.equals(content, out.toByteArray()), "contained hard-link alias export");
        check(store.list().containsAll(Arrays.asList("debian.txt", "debian-copy.txt")), "contained aliases omitted");
        check(!store.list().contains(".l2s.backing"), "internal hard-link backing file exposed");
        Files.createSymbolicLink(documents.resolve("broken"), Path.of("missing"));
        Files.createSymbolicLink(documents.resolve("cycle"), Path.of("cycle"));
        Files.createSymbolicLink(documents.resolve("escape-chain"), Path.of("link"));
        for (String name : Arrays.asList("broken", "cycle", "escape-chain")) {
            try { store.exportFile(name, new ByteArrayOutputStream()); throw new AssertionError("invalid alias exported"); }
            catch (IOException expected) { }
            check(!store.list().contains(name), "invalid alias listed");
        }
        Path linked = files.resolve("linked");
        Files.createDirectories(linked);
        Path externalHome = files.resolve("external-home");
        Files.createDirectories(externalHome);
        Files.createSymbolicLink(linked.resolve("home"), externalHome);
        try { new DocumentStore(linked.toFile()); throw new AssertionError("accepted linked home"); }
        catch (IOException expected) { }
        check(!Files.exists(externalHome.resolve("Documents")), "created directory outside private home");
        check(DocumentStore.safeName("..\\x\u0000.txt").equals("x.txt"), "display-name normalization");
        String longName = store.importFile(new ByteArrayInputStream(content), "字".repeat(120) + ".txt");
        check(longName.getBytes("UTF-8").length <= 220, "filename exceeds byte limit");
        System.out.println("DocumentStore: persistent bytes, collisions, failed-provider cleanup and containment PASS");
    }
    static void check(boolean value, String message) { if (!value) throw new AssertionError(message); }
}
'''


class DocumentsTest(unittest.TestCase):
    def test_real_filesystem_round_trip(self):
        project = Path(__file__).resolve().parent.parent
        java = Path(os.environ['JAVA_HOME']) / 'bin' if os.environ.get('JAVA_HOME') else Path('/usr/bin')
        with tempfile.TemporaryDirectory(prefix='plebdroid-documents-') as directory:
            root = Path(directory)
            (root / 'DocumentStoreTest.java').write_text(SOURCE)
            source = project / 'app/src/main/java/io/github/itsmygithubacct/plebdroid/DocumentStore.java'
            subprocess.run([str(java / 'javac'), '-d', str(root), str(source), str(root / 'DocumentStoreTest.java')], check=True)
            subprocess.run([str(java / 'java'), '-cp', str(root), 'io.github.itsmygithubacct.plebdroid.DocumentStoreTest', str(root / 'files')], check=True)


if __name__ == '__main__':
    unittest.main()
