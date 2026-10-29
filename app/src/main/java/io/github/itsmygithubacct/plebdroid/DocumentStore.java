// SPDX-License-Identifier: GPL-3.0-only
package io.github.itsmygithubacct.plebdroid;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.stream.Stream;

/** User documents are separate from the replaceable, packaged runtime. */
final class DocumentStore {
    static final long MAX_IMPORT_BYTES = 256L * 1024 * 1024;
    private final Path root;

    DocumentStore(File filesDir) throws IOException {
        Path files = filesDir.getCanonicalFile().toPath();
        Path home = files.resolve("home");
        if (Files.exists(home, LinkOption.NOFOLLOW_LINKS) && !home.toRealPath().equals(home))
            throw new IOException("Home must be a local directory");
        root = files.resolve("home/Documents");
        Files.createDirectories(root);
        if (!root.toRealPath().equals(root)) throw new IOException("Documents must be a local directory");
    }

    static String safeName(String displayName) {
        String value = displayName == null ? "" : displayName.replace('\\', '/');
        value = value.substring(value.lastIndexOf('/') + 1);
        StringBuilder clean = new StringBuilder();
        int bytes = 0;
        for (int offset = 0; offset < value.length();) {
            int codepoint = value.codePointAt(offset);
            offset += Character.charCount(codepoint);
            if (Character.isISOControl(codepoint)) continue;
            String character = new String(Character.toChars(codepoint));
            int length = character.getBytes(StandardCharsets.UTF_8).length;
            if (bytes + length > 220) break;
            clean.append(character);
            bytes += length;
        }
        value = clean.toString().trim();
        return value.isEmpty() || value.equals(".") || value.equals("..") ? "imported-file" : value;
    }

    String importFile(InputStream input, String displayName) throws IOException {
        if (input == null) throw new IOException("The selected document cannot be opened");
        String name = safeName(displayName);
        Path staging = Files.createTempFile(root, ".import-", ".tmp");
        try {
            try (OutputStream out = Files.newOutputStream(staging)) { copy(input, out, MAX_IMPORT_BYTES); }
            int dot = name.lastIndexOf('.');
            String stem = dot > 0 ? name.substring(0, dot) : name;
            String extension = dot > 0 ? name.substring(dot) : "";
            for (int i = 1; i <= 10000; i++) {
                String candidate = i == 1 ? name : stem + " (" + i + ")" + extension;
                Path destination = root.resolve(candidate);
                try {
                    // Reserve the name before the rename. Two imports can otherwise
                    // both choose it, and one rename replaces the other's bytes.
                    Files.createFile(destination);
                } catch (FileAlreadyExistsException exists) {
                    continue;
                }
                try {
                    Files.move(staging, destination, StandardCopyOption.ATOMIC_MOVE);
                    return candidate;
                } catch (IOException error) {
                    Files.deleteIfExists(destination);
                    throw error;
                }
            }
            throw new IOException("Too many documents have this name");
        } finally { Files.deleteIfExists(staging); }
    }

    List<String> list() throws IOException {
        ArrayList<String> names = new ArrayList<>();
        try (Stream<Path> paths = Files.walk(root, 16)) {
            paths.filter(p -> {
                String name = p.getFileName().toString();
                if (name.startsWith(".import-") || name.startsWith(".l2s.")) return false;
                try { documentPath(root.relativize(p).toString()); return true; }
                catch (IOException error) { return false; }
            })
                .limit(1001).forEach(p -> names.add(root.relativize(p).toString()));
        }
        if (names.size() > 1000) throw new IOException("Choose from a Documents folder with at most 1000 files");
        names.sort(Comparator.naturalOrder());
        return names;
    }

    void exportFile(String relativeName, OutputStream output) throws IOException {
        if (output == null) throw new IOException("The selected destination cannot be opened");
        Path path = documentPath(relativeName);
        try (InputStream input = Files.newInputStream(path, LinkOption.NOFOLLOW_LINKS)) {
            copy(input, output, Long.MAX_VALUE);
        }
    }

    private Path documentPath(String relativeName) throws IOException {
        Path path = root.resolve(relativeName).normalize();
        if (!path.startsWith(root)) throw new IOException("Choose a file inside Documents");
        // PRoot represents hard links as symlinks to another file in the
        // same directory. Only expose aliases whose real file stays here.
        Path real = path.toRealPath();
        if (!real.startsWith(root) || !Files.isRegularFile(real, LinkOption.NOFOLLOW_LINKS))
            throw new IOException("Choose a file inside Documents");
        return real;
    }

    private static void copy(InputStream input, OutputStream output, long limit) throws IOException {
        byte[] buffer = new byte[65536];
        long total = 0;
        for (int count; (count = input.read(buffer)) != -1;) {
            total += count;
            if (total > limit) throw new IOException("Import is limited to 256 MiB per file");
            output.write(buffer, 0, count);
        }
    }
}
