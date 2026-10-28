#!/usr/bin/env python3
"""Build the Android arm64 Kilix Python desktop runtime pieces.

Produces a PIE launcher (libplebdroid_python.so), libkilix-state.so, the
CPython libraries those need, and a real Pillow 12.3.0 site tree.

The CPython prefix, NDK, and native-deps prefix are read only. JPEG, WebP,
and PIC rebuilds of FreeType/libpng/LittleCMS land in --cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


PAGE_SIZE = 16384
ABI = "arm64-v8a"
API = 29
TRIPLE = "aarch64-linux-android29"
SOABI = "cpython-314-aarch64-linux-android"
EXT_SUFFIX = "." + SOABI + ".so"
STATE_COMMIT = "cac25a6d6d2036fa5e36855f4ba36ccdfb88af32"

PILLOW_VERSION = "12.3.0"
PILLOW_URL = (
    "https://files.pythonhosted.org/packages/1c/3d/"
    "bb7fca845737cf9d7dbde16ed1843984665ff2e0a518f5db43e77ec540b9/"
    "pillow-12.3.0.tar.gz"
)
PILLOW_SHA256 = "3b8182a766685eaa002637e28b4ec8d6b18819a0c71f579bf0dbaa5830297cce"

JPEG_VERSION = "3.1.2"
JPEG_URL = (
    "https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/"
    "3.1.2/libjpeg-turbo-3.1.2.tar.gz"
)
JPEG_SHA256 = "8f0012234b464ce50890c490f18194f913a7b1f4e6a03d6644179fa0f867d0cf"

WEBP_VERSION = "1.6.0"
WEBP_URL = (
    "https://storage.googleapis.com/downloads.webmproject.org/releases/webp/"
    "libwebp-1.6.0.tar.gz"
)
WEBP_SHA256 = "e4ab7009bf0629fd11982d4c2aa83964cf244cffba7347ecd39019a9e38c4564"

# Same upstream archives and digests as the Kilix Android engine's
# tools/native-dependencies.json. The engine builder caches each one as
# <deps>/../downloads/<name>-<sha256[:12]>.tar, which is reused after
# verification instead of downloading again. The extracted native-deps
# source trees are never used.
PIC_SOURCES = {
    "libpng": {
        "version": "1.6.59",
        "url": "https://github.com/pnggroup/libpng/archive/refs/tags/v1.6.59.tar.gz",
        "sha256": "2540302a1844ad2b2b501977abecfa850f265f97b78f065a712ab4074a89f5b5",
        "archive": "libpng-1.6.59.tar.gz",
        "top": "libpng-1.6.59",
    },
    "freetype": {
        "version": "2.15.0",
        "url": "https://downloads.sourceforge.net/freetype/freetype-2.15.0.tar.xz",
        "sha256": "93cd47eb719c325107f2d3cf087d06299e944c3b48a40b55537302304dea1dd5",
        "archive": "freetype-2.15.0.tar.xz",
        "top": "freetype-2.15.0",
    },
    "lcms": {
        "version": "2.19.1",
        "url": "https://github.com/mm2/Little-CMS/archive/refs/tags/lcms2.19.1.tar.gz",
        "sha256": "267705e278e2f7c2fb886c259dadcbaeb2be52748bcbc71c79f08aacacb7a709",
        "archive": "lcms2-2.19.1.tar.gz",
        "top": "Little-CMS-lcms2.19.1",
    },
}
PIC_DIGEST_ORIGIN = (
    "Kilix Android engine tools/native-dependencies.json, blob "
    "db615c6192ddddcec98d55851ef443347018d04e, added in commit "
    "510c8f7b56b653741c2d4d3e3acc75eb23e81e4d"
)

EM_AARCH64 = 183
ET_DYN = 3
PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3
DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_SONAME = 14
DT_RPATH = 15
DT_TEXTREL = 22
DT_RUNPATH = 29
DT_FLAGS = 30
DT_FLAGS_1 = 0x6FFFFFFB
DF_TEXTREL = 0x4
DF_1_PIE = 0x08000000
SHT_NOTE = 7

PYTHON_LIBS = (
    "libpython3.14.so",
    "libcrypto_python.so",
    "libssl_python.so",
    "libsqlite3_python.so",
)

ALLOWED_EXTENSION_NEEDED = {
    "libpython3.14.so",
    "libc.so",
    "libm.so",
    "libdl.so",
    "liblog.so",
    "libz.so",
}

LIBIMAGING = (
    "Access", "AlphaComposite", "Arrow", "Bands", "BcnDecode", "BcnEncode",
    "BitDecode", "Blend", "BoxBlur", "Chops", "ColorLUT", "Convert",
    "ConvertYCbCr", "Copy", "Crop", "Dib", "Draw", "Effects", "EpsEncode",
    "Fill", "Filter", "FliDecode", "Geometry", "GetBBox", "GifDecode",
    "GifEncode", "HexDecode", "Histo", "Jpeg2KDecode", "Jpeg2KEncode",
    "JpegDecode", "JpegEncode", "Matrix", "Mode", "ModeFilter", "Negative",
    "Offset", "Pack", "PackDecode", "Palette", "Paste", "PcdDecode",
    "PcxDecode", "PcxEncode", "Point", "Quant", "QuantHash", "QuantHeap",
    "QuantOctree", "QuantPngQuant", "RankFilter", "RawDecode", "RawEncode",
    "Reduce", "Resample", "SgiRleDecode", "Storage", "SunRleDecode",
    "TgaRleDecode", "TgaRleEncode", "TiffDecode", "Unpack", "UnpackYCC",
    "UnsharpMask", "XbmDecode", "XbmEncode", "ZipDecode", "ZipEncode",
    "codec_fd",
)

IMAGING_CORE = (
    "decode", "encode", "map", "display", "outline", "path",
)


class HashMismatch(Exception):
    pass


class BuildFailure(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    log("+ " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        raise BuildFailure(f"command failed with {result.returncode}: {cmd[0]}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha256(path: Path, expected: str) -> str:
    got = sha256_file(path)
    if got != expected:
        raise HashMismatch(f"{path}: expected {expected} got {got}")
    return got


def u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little")


def u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")


def u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def vaddr_to_offset(loads: list[dict], vaddr: int) -> int | None:
    for load in loads:
        if load["vaddr"] <= vaddr < load["vaddr"] + load["filesz"]:
            return load["offset"] + (vaddr - load["vaddr"])
    return None


def cstring(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("utf-8", "replace")


def parse_elf(path: Path) -> dict:
    data = path.read_bytes()
    if data[:4] != b"\x7fELF":
        raise BuildFailure(f"{path} is not an ELF file")
    if data[4] != 2 or data[5] != 1:
        raise BuildFailure(f"{path} is not a little-endian ELF64 file")
    machine = u16(data, 18)
    loads = []
    interp = False
    dynamic_offset = None
    dynamic_size = 0
    phoff = u64(data, 32)
    phentsize = u16(data, 54)
    phnum = u16(data, 56)
    for index in range(phnum):
        off = phoff + index * phentsize
        p_type = u32(data, off)
        p_offset = u64(data, off + 8)
        p_vaddr = u64(data, off + 16)
        p_filesz = u64(data, off + 32)
        p_align = u64(data, off + 48)
        if p_type == PT_LOAD:
            loads.append({
                "offset": p_offset,
                "vaddr": p_vaddr,
                "filesz": p_filesz,
                "align": p_align,
            })
        elif p_type == PT_INTERP:
            interp = True
        elif p_type == PT_DYNAMIC:
            dynamic_offset = p_offset
            dynamic_size = p_filesz

    needed: list[str] = []
    runpath: list[str] = []
    soname = None
    pie = False
    textrel = False
    if dynamic_offset is not None:
        tags = {}
        cursor = dynamic_offset
        end = dynamic_offset + dynamic_size
        while cursor + 16 <= end:
            tag = u64(data, cursor)
            val = u64(data, cursor + 8)
            cursor += 16
            if tag == DT_NULL:
                break
            tags.setdefault(tag, []).append(val)
        strtab = tags.get(DT_STRTAB, [None])[0]
        str_off = None if strtab is None else vaddr_to_offset(loads, strtab)
        if str_off is not None:
            for val in tags.get(DT_NEEDED, []):
                needed.append(cstring(data, str_off + val))
            for tag in (DT_RUNPATH, DT_RPATH):
                for val in tags.get(tag, []):
                    runpath.append(cstring(data, str_off + val))
            if DT_SONAME in tags:
                soname = cstring(data, str_off + tags[DT_SONAME][0])
        if DT_TEXTREL in tags:
            textrel = True
        for val in tags.get(DT_FLAGS, []):
            if val & DF_TEXTREL:
                textrel = True
        for val in tags.get(DT_FLAGS_1, []):
            if val & DF_1_PIE:
                pie = True

    android_api = None
    debug_sections = []
    shoff = u64(data, 40)
    shentsize = u16(data, 58)
    shnum = u16(data, 60)
    shstrndx = u16(data, 62)
    if shnum and shstrndx < shnum:
        shstr_off = u64(data, shoff + shstrndx * shentsize + 24)
        for index in range(shnum):
            base = shoff + index * shentsize
            name_off = u32(data, base)
            sh_type = u32(data, base + 4)
            sh_offset = u64(data, base + 24)
            sh_size = u64(data, base + 32)
            name = cstring(data, shstr_off + name_off)
            if sh_type == SHT_NOTE and name == ".note.android.ident":
                android_api = android_note_api(data[sh_offset:sh_offset + sh_size])
            elif name.startswith(".debug_"):
                debug_sections.append(name)

    aligns = [item["align"] for item in loads]
    return {
        "type": u16(data, 16),
        "machine": machine,
        "interp": interp,
        "pie": pie,
        "load_align": aligns,
        "min_load_align": min(aligns) if aligns else 0,
        "needed": needed,
        "runpath": runpath,
        "soname": soname,
        "textrel": textrel,
        "android_api": android_api,
        "debug_sections": debug_sections,
    }


def android_note_api(note: bytes) -> int | None:
    cursor = 0
    while cursor + 12 <= len(note):
        namesz = u32(note, cursor)
        descsz = u32(note, cursor + 4)
        cursor += 12
        name_end = cursor + namesz
        desc = cursor + ((namesz + 3) & ~3)
        desc_end = desc + descsz
        name = note[cursor:name_end].split(b"\0", 1)[0]
        if name == b"Android" and descsz >= 4:
            return u32(note, desc)
        cursor = (desc_end + 3) & ~3
    return None


def file_description(path: Path) -> str:
    result = subprocess.run(
        ["file", "-b", str(path)],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def app_source() -> Path:
    return Path(__file__).resolve().parent.parent / "native" / "python_app.c"


def selftest_hash() -> bool:
    handle = tempfile.NamedTemporaryFile(prefix="plebdroid-hash-", delete=False)
    try:
        handle.write(b"plebdroid-hash-mismatch-check")
        handle.close()
        path = Path(handle.name)
        require_sha256(path, sha256_file(path))
        try:
            require_sha256(path, "0" * 64)
        except HashMismatch:
            return True
        return False
    finally:
        Path(handle.name).unlink(missing_ok=True)


def selftest_sources() -> tuple[bool, bool, str]:
    """A wrong seed is never installed; extraction checks digest and top."""
    work = Path(tempfile.mkdtemp(prefix="plebdroid-sources-"))
    try:
        tree = work / "tree" / "pkg-1"
        tree.mkdir(parents=True)
        (tree / "README").write_text("pkg\n")
        good = work / "good.tar"
        with tarfile.open(good, "w") as handle:
            handle.add(tree, arcname="pkg-1")
        digest = sha256_file(good)
        bad = work / "bad.tar"
        bad.write_bytes(good.read_bytes() + b"\0" * 512)
        dest = work / "downloads" / "pkg-1.tar"
        unreachable = (work / "missing.tar").as_uri()
        seed_rejected = False
        try:
            ensure_tarball(unreachable, digest, dest, (bad,))
        except BuildFailure:
            seed_rejected = not dest.exists()
        seed_used = ensure_tarball(unreachable, digest, dest, (bad, good)) == str(good)
        extracted = extract_tarball(dest, digest, work / "src", "pkg-1")
        (extracted / "README").write_text("modified after extraction\n")
        extracted = extract_tarball(dest, digest, work / "src", "pkg-1")
        refreshed = (extracted / "README").read_text() == "pkg\n"
        wrong_top = False
        try:
            extract_tarball(dest, digest, work / "src", "other-1")
        except BuildFailure:
            wrong_top = True
        wrong_digest = False
        try:
            extract_tarball(bad, digest, work / "src", "pkg-1")
        except HashMismatch:
            wrong_digest = True
        seeds_ok = seed_rejected and seed_used
        extract_ok = refreshed and wrong_top and wrong_digest
        detail = (
            f"bad seed rejected={seed_rejected} good seed used={seed_used} "
            f"re-extracted={refreshed} wrong top rejected={wrong_top} "
            f"wrong digest rejected={wrong_digest}"
        )
        return seeds_ok, extract_ok, detail
    finally:
        shutil.rmtree(work, ignore_errors=True)


def selftest_launcher() -> bool:
    source = app_source()
    if not source.is_file():
        raise BuildFailure(f"missing launcher source: {source}")
    binary = Path(tempfile.mkdtemp(prefix="plebdroid-selftest-")) / "python_app_selftest"
    run([
        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-DPLEBDROID_HOST_SELFTEST",
        "-o", str(binary), str(source),
    ])
    result = subprocess.run([str(binary)], text=True, capture_output=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    shutil.rmtree(binary.parent, ignore_errors=True)
    return result.returncode == 0 and "selftest ok" in result.stdout


def compile_align_fixture(path: Path, page: int) -> None:
    source = Path(tempfile.mkdtemp(prefix="plebdroid-align-src-")) / "align.c"
    source.write_text("int plebdroid_align_fixture(void) { return 1; }\n")
    run([
        "cc", "-shared", "-fPIC",
        f"-Wl,-z,max-page-size={page}",
        f"-Wl,-z,common-page-size={page}",
        "-o", str(path), str(source),
    ])
    shutil.rmtree(source.parent, ignore_errors=True)


def selftest_alignment() -> tuple[bool, bool, str]:
    work = Path(tempfile.mkdtemp(prefix="plebdroid-align-"))
    small = work / "align4k.so"
    large = work / "align16k.so"
    try:
        compile_align_fixture(small, 4096)
        compile_align_fixture(large, 16384)
        small_elf = parse_elf(small)
        large_elf = parse_elf(large)
        rejects = small_elf["min_load_align"] < PAGE_SIZE
        accepts = large_elf["min_load_align"] >= PAGE_SIZE
        detail = (
            f"4k align {small_elf['min_load_align']}, "
            f"16k align {large_elf['min_load_align']}"
        )
        return rejects, accepts, detail
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_self_tests() -> list[dict]:
    log("running host self-tests")
    launcher_ok = selftest_launcher()
    hash_ok = selftest_hash()
    rejects, accepts, align_detail = selftest_alignment()
    seeds_ok, extract_ok, sources_detail = selftest_sources()
    checks = [
        {"name": "source_seed_digest_enforced", "passed": seeds_ok,
         "detail": sources_detail},
        {"name": "source_extract_fresh_and_verified", "passed": extract_ok,
         "detail": sources_detail},
        {"name": "host_launcher_selftest", "passed": launcher_ok,
         "detail": "plebdroid python_app selftest ok" if launcher_ok else "failed"},
        {"name": "sha256_mismatch_rejected", "passed": hash_ok,
         "detail": "require_sha256 raises HashMismatch"},
        {"name": "elf_alignment_fixture_rejects_4k", "passed": rejects,
         "detail": align_detail},
        {"name": "elf_alignment_fixture_accepts_16k", "passed": accepts,
         "detail": align_detail},
    ]
    failed = [item["name"] for item in checks if not item["passed"]]
    if failed:
        raise BuildFailure("host self-test failed: " + ", ".join(failed))
    log("host self-tests passed")
    return checks


def clean_cmake_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS", "AR", "AS", "NM",
        "RANLIB", "STRIP", "OBJCOPY", "OBJDUMP", "READELF",
        "PKG_CONFIG", "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR",
        "PKG_CONFIG_SYSROOT_DIR",
    ):
        env.pop(key, None)
    return env


def ensure_tarball(url: str, expected: str, dest: Path, seeds: tuple[Path, ...] = ()) -> str:
    """Place the archive with this sha256 at dest; return where it came from."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        try:
            require_sha256(dest, expected)
            log(f"cache hit {dest.name}")
            return "cache"
        except HashMismatch as exc:
            log(f"replacing {dest.name}: {exc}")
            dest.unlink()
    partial = dest.with_suffix(dest.suffix + ".partial")
    for seed in seeds:
        if not seed.is_file():
            continue
        shutil.copyfile(seed, partial)
        try:
            require_sha256(partial, expected)
        except HashMismatch as exc:
            partial.unlink(missing_ok=True)
            log(f"ignoring {seed}: {exc}")
            continue
        partial.replace(dest)
        log(f"verified {seed} for {dest.name}")
        return str(seed)
    run(["curl", "-fsSL", "--retry", "5", "-o", str(partial), url])
    try:
        require_sha256(partial, expected)
    except HashMismatch:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)
    return url


def extract_tarball(archive: Path, expected: str, source_root: Path, top: str) -> Path:
    """Extract a verified archive fresh on every run.

    Build inputs never come from a tree that could have changed after an
    earlier extraction.
    """
    require_sha256(archive, expected)
    extracted = source_root / top
    source_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".extract-", dir=source_root))
    try:
        log(f"extracting {archive.name}")
        with tarfile.open(archive) as handle:
            handle.extractall(work, filter="data")
        roots = list(work.iterdir())
        if [path.name for path in roots] != [top] or not roots[0].is_dir():
            raise BuildFailure(
                f"archive {archive} must contain only {top}; found {[path.name for path in roots]}"
            )
        if extracted.exists():
            shutil.rmtree(extracted)
        roots[0].rename(extracted)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return extracted


def option_digest(options: list[str]) -> str:
    return hashlib.sha256("\0".join(options).encode()).hexdigest()[:16]


def cmake_install(
    name: str,
    source: Path,
    prefix: Path,
    ndk: Path,
    cache: Path,
    jobs: int,
    extra: list[str],
    stamp_key: str,
    archive_names: tuple[str, ...],
) -> Path:
    stamp = cache / "stamps" / f"{name}.stamp"
    stamp_key = f"{stamp_key};options={option_digest(extra)}"
    existing = find_static(prefix, archive_names) if prefix.exists() else None
    if stamp.is_file() and stamp.read_text().strip() == stamp_key and existing:
        log(f"cmake cache hit {name}: {existing}")
        return existing
    build = cache / "build" / name
    if build.exists():
        shutil.rmtree(build)
    if prefix.exists():
        shutil.rmtree(prefix)
    prefix.mkdir(parents=True)
    toolchain = ndk / "build" / "cmake" / "android.toolchain.cmake"
    cmd = [
        "cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        "-DANDROID_ABI=arm64-v8a",
        "-DANDROID_PLATFORM=android-29",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        "-DCMAKE_C_FLAGS=-fPIC",
        f"-DCMAKE_INSTALL_PREFIX={prefix}",
        f"-DCMAKE_FIND_ROOT_PATH={prefix}",
        f"-DCMAKE_PREFIX_PATH={prefix}",
    ]
    cmd.extend(extra)
    env = clean_cmake_env()
    run(cmd, env=env)
    run(["cmake", "--build", str(build), "--parallel", str(jobs)], env=env)
    run(["cmake", "--install", str(build)], env=env)
    archive = find_static(prefix, archive_names)
    if archive is None:
        found = [str(path) for path in prefix.rglob("*.a")]
        raise BuildFailure(f"{name} install did not produce {archive_names}; found {found}")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(stamp_key + "\n")
    return archive


def find_static(prefix: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        for libdir in ("lib", "lib64"):
            candidate = prefix / libdir / name
            if candidate.is_file() and candidate.read_bytes()[:8] == b"!<arch>\n":
                return candidate
    return None


def find_include(prefix: Path, relative: str) -> Path | None:
    relative_path = Path(relative)
    for include in ("include", "include/freetype2", "include/libpng16"):
        candidate = prefix / include / relative_path
        if candidate.is_file():
            return candidate.parents[len(relative_path.parts) - 1]
    matches = [path for path in prefix.rglob(relative_path.name) if path.is_file()]
    if matches:
        return matches[0].parents[len(relative_path.parts) - 1]
    return None


def nested(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def copy_file(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    mode = dest.stat().st_mode
    dest.chmod(mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def compile_objects(
    compiler: str,
    sources: list[Path],
    obj_dir: Path,
    includes: list[Path],
    defines: list[str],
    jobs: int,
    flags: tuple[str, ...] = (),
) -> list[Path]:
    obj_dir.mkdir(parents=True, exist_ok=True)
    objects: list[Path] = []

    def one(source: Path) -> Path:
        obj = obj_dir / (source.name + ".o")
        cmd = [
            compiler, "-c", "-fPIC", "-O2", "-fno-strict-aliasing", "-fwrapv",
            *flags, "-o", str(obj), str(source),
        ]
        for include in includes:
            cmd.extend(["-I", str(include)])
        for define in defines:
            cmd.append("-D" + define)
        run(cmd)
        return obj

    workers = max(1, min(jobs, len(sources)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, source) for source in sources]
        for future in as_completed(futures):
            objects.append(future.result())
    order = {source.name + ".o": index for index, source in enumerate(sources)}
    objects.sort(key=lambda path: order[path.name])
    return objects


def link_shared(
    compiler: str,
    output: Path,
    objects: list[Path],
    python_lib: Path,
    static_libs: list[Path],
    extra: list[str],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        compiler, "-shared", "-fPIC",
        f"-Wl,-soname,{output.name}",
        "-Wl,-z,max-page-size=16384",
        "-Wl,-z,common-page-size=16384",
        "-Wl,-z,relro", "-Wl,-z,now",
        "-Wl,--no-undefined",
        "-o", str(output),
    ]
    cmd.extend(str(obj) for obj in objects)
    cmd.extend(["-L", str(python_lib), "-lpython3.14"])
    if static_libs:
        cmd.append("-Wl,--start-group")
        cmd.extend(str(path) for path in static_libs)
        cmd.append("-Wl,--end-group")
    cmd.extend(extra)
    run(cmd)


def object_symbols(nm_tool: str, path: Path) -> tuple[set[str], set[str]]:
    result = subprocess.run(
        [nm_tool, str(path)],
        check=False, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise BuildFailure(f"llvm-nm failed for {path}: {result.stderr}")
    defined: set[str] = set()
    undefined: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "U":
            undefined.add(parts[-1])
        elif len(parts) >= 3 and parts[-2] in {"T", "D", "B", "R", "C", "W"}:
            defined.add(parts[-1])
    return defined, undefined


def support_objects(nm_tool: str, seeds: list[Path], pool: list[Path]) -> list[Path]:
    """Objects from pool required to resolve symbols used by seeds."""
    defined_owner: dict[str, Path] = {}
    undefined_by: dict[Path, set[str]] = {}
    for obj in pool:
        defined, undefined = object_symbols(nm_tool, obj)
        undefined_by[obj] = undefined
        for name in defined:
            defined_owner.setdefault(name, obj)
    pending: set[str] = set()
    for seed in seeds:
        _, undefined = object_symbols(nm_tool, seed)
        pending |= undefined
    chosen: list[Path] = []
    chosen_set: set[Path] = set()
    changed = True
    while changed:
        changed = False
        for name in list(pending):
            owner = defined_owner.get(name)
            if owner is None or owner in chosen_set:
                continue
            chosen.append(owner)
            chosen_set.add(owner)
            pending |= undefined_by[owner]
            changed = True
    return chosen


def defined_symbols(nm: str, path: Path) -> list[str]:
    result = subprocess.run(
        [nm, "-D", "--defined-only", str(path)],
        check=False, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise BuildFailure(f"llvm-nm failed for {path}: {result.stderr}")
    symbols = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts:
            symbols.append(parts[-1])
    return symbols


def check_add(checks: list[dict], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def elf_summary(info: dict, description: str) -> dict:
    return {
        "type": "DYN" if info["type"] == ET_DYN else info["type"],
        "machine": "AArch64" if info["machine"] == EM_AARCH64 else info["machine"],
        "interp": info["interp"],
        "pie": info["pie"],
        "load_align": info["load_align"],
        "min_load_align": info["min_load_align"],
        "needed": info["needed"],
        "runpath": info["runpath"],
        "soname": info["soname"],
        "textrel": info["textrel"],
        "android_api_note": info["android_api"],
        "file": description,
    }


def require_built_elf(path: Path, checks: list[dict], *, executable: bool) -> dict:
    info = parse_elf(path)
    description = file_description(path)
    summary = elf_summary(info, description)
    prefix = path.name
    check_add(checks, f"{prefix}.aarch64", info["machine"] == EM_AARCH64 and info["type"] == ET_DYN,
              f"machine={info['machine']} type={info['type']}")
    check_add(checks, f"{prefix}.align", info["min_load_align"] >= PAGE_SIZE,
              f"min_load_align={info['min_load_align']}")
    check_add(checks, f"{prefix}.textrel", not info["textrel"],
              "DT_TEXTREL present" if info["textrel"] else "no text relocations")
    check_add(checks, f"{prefix}.no_debug_info", not info["debug_sections"],
              " ".join(info["debug_sections"]) or "no .debug_* sections")
    api_ok = info["android_api"] == API or "for Android 29" in description
    check_add(checks, f"{prefix}.api29", api_ok,
              f"note={info['android_api']} file={description}")
    if executable:
        origin = any("ORIGIN" in item for item in info["runpath"])
        needed = info["needed"]
        try:
            crypto = needed.index("libcrypto_python.so")
            ssl = needed.index("libssl_python.so")
            order_ok = crypto < ssl
        except ValueError:
            crypto = ssl = -1
            order_ok = False
        required = [
            "libpython3.14.so",
            "libcrypto_python.so",
            "libssl_python.so",
            "libsqlite3_python.so",
            "libkilix-state.so",
        ]
        missing = [name for name in required if name not in needed]
        check_add(checks, f"{prefix}.pie", info["pie"] and info["interp"],
                  f"pie={info['pie']} interp={info['interp']}")
        check_add(checks, f"{prefix}.runpath", origin, " ".join(info["runpath"]))
        check_add(checks, f"{prefix}.needed", not missing and order_ok,
                  f"missing={missing} crypto_index={crypto} ssl_index={ssl} needed={needed}")
    else:
        check_add(checks, f"{prefix}.shared", not info["interp"] and not info["pie"],
                  f"interp={info['interp']} pie={info['pie']}")
    return summary


def copy_tree_files(source: Path, dest: Path, suffixes: tuple[str, ...]) -> None:
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in suffixes and path.name not in suffixes:
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(source)
        copy_file(path, dest / relative)


def read_pc_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def header_version(path: Path, prefix: str) -> str | None:
    if not path.is_file():
        return None
    values = {}
    for line in path.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "#define" and parts[1].startswith(prefix):
            values[parts[1]] = parts[2]
    return None if not values else " ".join(f"{key}={values[key]}" for key in sorted(values))


def git_head(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False, text=True, capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def build(args: argparse.Namespace, checks: list[dict]) -> None:
    ndk = Path(args.ndk).resolve()
    python_prefix = Path(args.python).resolve()
    deps = Path(args.deps).resolve()
    state = Path(args.state).resolve()
    cache = Path(args.cache).resolve()
    output = Path(args.output).resolve()
    for label, path in (
        ("ndk", ndk), ("python", python_prefix), ("deps", deps), ("state", state),
    ):
        if not path.exists():
            raise BuildFailure(f"--{label} does not exist: {path}")
    if cache == output or nested(cache, output) or nested(output, cache):
        raise BuildFailure("cache and output must be separate directories")
    # A failed run must not leave an older passing manifest behind.
    (output / "manifest.json").unlink(missing_ok=True)

    toolchain_bin = ndk / "toolchains" / "llvm" / "prebuilt" / "linux-x86_64" / "bin"
    compiler = toolchain_bin / f"{TRIPLE}-clang"
    nm = toolchain_bin / "llvm-nm"
    strip_tool = toolchain_bin / "llvm-strip"
    if not compiler.is_file():
        raise BuildFailure(f"missing Android compiler: {compiler}")
    python_include = python_prefix / "include" / "python3.14"
    python_lib = python_prefix / "lib"
    if not (python_include / "Python.h").is_file():
        raise BuildFailure(f"missing Python.h under {python_include}")
    for name in PYTHON_LIBS:
        if not (python_lib / name).is_file():
            raise BuildFailure(f"missing {python_lib / name}")
    stdlib = python_prefix / "lib" / "python3.14"
    if not stdlib.is_dir():
        raise BuildFailure(f"missing CPython stdlib directory: {stdlib}")

    state_head = git_head(state)
    if state_head is None:
        raise BuildFailure(f"kilix-state has no git HEAD: {state}")
    if state_head != STATE_COMMIT:
        raise BuildFailure(
            f"kilix-state HEAD {state_head} does not match pinned {STATE_COMMIT}"
        )

    launcher_obj = cache / "build" / "launcher" / "python_app.o"
    launcher_obj.parent.mkdir(parents=True, exist_ok=True)
    log("compiling launcher against the Android Python headers")
    run([
        str(compiler), "-c", "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2", "-fPIE",
        "-I", str(python_include),
        "-o", str(launcher_obj), str(app_source()),
    ])

    downloads = cache / "downloads"
    ensure_tarball(JPEG_URL, JPEG_SHA256, downloads / "libjpeg-turbo-3.1.2.tar.gz")
    ensure_tarball(WEBP_URL, WEBP_SHA256, downloads / "libwebp-1.6.0.tar.gz")
    ensure_tarball(PILLOW_URL, PILLOW_SHA256, downloads / "pillow-12.3.0.tar.gz")
    pic_origin = {}
    for name, pin in PIC_SOURCES.items():
        pic_origin[name] = ensure_tarball(
            pin["url"], pin["sha256"], downloads / pin["archive"],
            (deps.parent / "downloads" / f"{name}-{pin['sha256'][:12]}.tar",),
        )
    src_root = cache / "src"
    jpeg_src = extract_tarball(
        downloads / "libjpeg-turbo-3.1.2.tar.gz", JPEG_SHA256, src_root,
        "libjpeg-turbo-3.1.2",
    )
    webp_src = extract_tarball(
        downloads / "libwebp-1.6.0.tar.gz", WEBP_SHA256, src_root, "libwebp-1.6.0",
    )
    pillow_src = extract_tarball(
        downloads / "pillow-12.3.0.tar.gz", PILLOW_SHA256, src_root, "pillow-12.3.0",
    )
    png_src, freetype_src, lcms_src = (
        extract_tarball(
            downloads / PIC_SOURCES[name]["archive"], PIC_SOURCES[name]["sha256"],
            src_root, PIC_SOURCES[name]["top"],
        )
        for name in ("libpng", "freetype", "lcms")
    )

    jobs = args.jobs or os.cpu_count() or 1
    common_stamp = f"ndk={ndk};api=29;abi=arm64-v8a;pic=1"
    log("building PIC libpng, freetype, lcms, libjpeg-turbo, and libwebp")
    png_archive = cmake_install(
        "libpng", png_src, cache / "prefix" / "libpng", ndk, cache, jobs,
        [
            "-DPNG_SHARED=OFF", "-DPNG_STATIC=ON", "-DPNG_TESTS=OFF",
            "-DPNG_TOOLS=OFF", "-DPNG_FRAMEWORK=OFF",
            "-DPNG_HARDWARE_OPTIMIZATIONS=ON",
        ],
        common_stamp + ";png;" + PIC_SOURCES["libpng"]["sha256"],
        ("libpng16.a", "libpng.a"),
    )
    freetype_archive = cmake_install(
        "freetype", freetype_src, cache / "prefix" / "freetype", ndk, cache, jobs,
        [
            "-DFT_DISABLE_BZIP2=ON", "-DFT_DISABLE_BROTLI=ON",
            "-DFT_DISABLE_HARFBUZZ=ON", "-DFT_DISABLE_HVF=ON",
            "-DFT_REQUIRE_ZLIB=ON", "-DFT_REQUIRE_PNG=ON",
            f"-DCMAKE_FIND_ROOT_PATH={cache / 'prefix' / 'libpng'}",
            f"-DCMAKE_PREFIX_PATH={cache / 'prefix' / 'libpng'}",
            f"-DPNG_LIBRARY={png_archive}",
            f"-DPNG_PNG_INCLUDE_DIR={png_archive.parent.parent / 'include'}",
        ],
        common_stamp + ";freetype;" + PIC_SOURCES["freetype"]["sha256"] + ";"
        + sha256_file(png_archive),
        ("libfreetype.a",),
    )
    lcms_archive = cmake_install(
        "lcms", lcms_src, cache / "prefix" / "lcms", ndk, cache, jobs,
        [
            "-DBUILD_SHARED_LIBS=OFF",
            "-DLCMS2_BUILD_SHARED=OFF", "-DLCMS2_BUILD_STATIC=ON",
            "-DLCMS2_BUILD_TOOLS=OFF", "-DLCMS2_BUILD_TESTS=OFF",
            "-DLCMS2_WITH_JPEG=OFF", "-DLCMS2_WITH_TIFF=OFF",
            "-DLCMS2_WITH_FASTFLOAT=OFF", "-DLCMS2_WITH_THREADED_PLUGIN=OFF",
        ],
        common_stamp + ";lcms;" + PIC_SOURCES["lcms"]["sha256"],
        ("liblcms2.a",),
    )
    jpeg_archive = cmake_install(
        "libjpeg", jpeg_src, cache / "prefix" / "libjpeg", ndk, cache, jobs,
        [
            "-DENABLE_SHARED=OFF", "-DENABLE_STATIC=ON",
            "-DWITH_TURBOJPEG=OFF", "-DWITH_TOOLS=OFF", "-DWITH_TESTS=OFF",
            "-DWITH_JAVA=OFF",
        ],
        common_stamp + ";jpeg;" + JPEG_SHA256,
        ("libjpeg.a",),
    )
    webp_prefix = cache / "prefix" / "libwebp"
    cmake_install(
        "libwebp", webp_src, webp_prefix, ndk, cache, jobs,
        [
            "-DBUILD_SHARED_LIBS=OFF",
            "-DWEBP_BUILD_ANIM_UTILS=OFF", "-DWEBP_BUILD_CWEBP=OFF",
            "-DWEBP_BUILD_DWEBP=OFF", "-DWEBP_BUILD_GIF2WEBP=OFF",
            "-DWEBP_BUILD_IMG2WEBP=OFF", "-DWEBP_BUILD_VWEBP=OFF",
            "-DWEBP_BUILD_WEBPINFO=OFF", "-DWEBP_BUILD_WEBPMUX=OFF",
            "-DWEBP_BUILD_EXTRAS=OFF", "-DWEBP_BUILD_LIBWEBPMUX=ON",
            "-DWEBP_NEAR_LOSSLESS=ON", "-DWEBP_BUILD_WEBP_JS=OFF",
        ],
        common_stamp + ";webp;" + WEBP_SHA256,
        ("libwebp.a",),
    )
    webp_libs = []
    for name in (
        "libwebpmux.a", "libwebpdemux.a", "libwebp.a",
        "libsharpyuv.a", "libcpufeatures-webp.a",
    ):
        found = find_static(webp_prefix, (name,))
        if found is None:
            found_all = [str(path.name) for path in webp_prefix.rglob("*.a")]
            raise BuildFailure(f"missing {name} under {webp_prefix}; found {found_all}")
        webp_libs.append(found)

    includes = [
        pillow_src / "src",
        pillow_src / "src" / "libImaging",
        python_include,
    ]
    for prefix, header in (
        (cache / "prefix" / "libjpeg", "jpeglib.h"),
        (webp_prefix, "webp/decode.h"),
        (cache / "prefix" / "freetype", "ft2build.h"),
        (cache / "prefix" / "libpng", "png.h"),
        (cache / "prefix" / "lcms", "lcms2.h"),
    ):
        header_dir = find_include(prefix, header)
        if header_dir is None:
            raise BuildFailure(f"missing header {header} under {prefix}")
        if header_dir not in includes:
            includes.append(header_dir)
        if header == "ft2build.h":
            freetype2 = header_dir / "freetype2"
            if freetype2.is_dir() and freetype2 not in includes:
                includes.append(freetype2)
            parent_ft = header_dir.parent
            if (parent_ft / "ft2build.h").is_file() and parent_ft not in includes:
                includes.append(parent_ft)

    imaging_sources = [pillow_src / "src" / "_imaging.c"]
    imaging_sources.extend(pillow_src / "src" / f"{name}.c" for name in IMAGING_CORE)
    imaging_sources.extend(
        pillow_src / "src" / "libImaging" / f"{name}.c" for name in LIBIMAGING
    )
    for source in imaging_sources:
        if not source.is_file():
            raise BuildFailure(f"missing Pillow source {source}")

    log("compiling Pillow extensions")
    # NDEBUG matches the interpreter's own extension CFLAGS, so a Pillow
    # assert cannot abort the whole desktop process.
    base_defines = ['PILLOW_VERSION="12.3.0"', "NDEBUG"]
    # Python headers expand __FILE__; keep the cache location out of outputs.
    prefix_map = (f"-ffile-prefix-map={src_root}=src",)
    imaging_objects = compile_objects(
        str(compiler), imaging_sources, cache / "build" / "pillow" / "imaging",
        includes, base_defines + ["HAVE_LIBJPEG", "HAVE_LIBZ"], jobs, prefix_map,
    )
    # Every file under python-site is packaged, so start it empty.
    python_site = output / "python-site"
    if python_site.exists():
        shutil.rmtree(python_site)
    site_pil = python_site / "PIL"
    link_shared(
        str(compiler), site_pil / f"_imaging{EXT_SUFFIX}", imaging_objects, python_lib,
        [jpeg_archive], ["-lz", "-lm", "-ldl", "-llog", "-pthread"],
    )

    support_pool = [obj for obj in imaging_objects if obj.name != "_imaging.c.o"]

    def one_extension(module: str, static_libs: list[Path], extra: list[str]) -> None:
        source = pillow_src / "src" / f"{module}.c"
        if not source.is_file():
            raise BuildFailure(f"missing Pillow source {source}")
        objects = compile_objects(
            str(compiler), [source], cache / "build" / "pillow" / module,
            includes, base_defines, jobs, prefix_map,
        )
        support = support_objects(str(nm), objects, support_pool)
        if support:
            log(module + " also links " + ", ".join(path.name for path in support))
        link_shared(
            str(compiler), site_pil / f"{module}{EXT_SUFFIX}",
            [*support, *objects], python_lib, static_libs, extra,
        )

    one_extension(
        "_imagingft", [freetype_archive, png_archive],
        ["-lz", "-lm", "-ldl", "-llog", "-pthread"],
    )
    one_extension(
        "_imagingcms", [lcms_archive],
        ["-lm", "-ldl", "-llog", "-pthread"],
    )
    one_extension(
        "_webp", webp_libs,
        ["-lm", "-ldl", "-llog", "-pthread"],
    )
    one_extension("_imagingmath", [], ["-lm", "-ldl", "-llog", "-pthread"])
    one_extension("_imagingmorph", [], ["-ldl", "-llog", "-pthread"])

    copy_tree_files(pillow_src / "src" / "PIL", site_pil, (".py", ".pyi", "py.typed"))

    log("building libkilix-state.so")
    jni = output / "jniLibs" / ABI
    if jni.exists():
        shutil.rmtree(jni)
    jni.mkdir(parents=True)
    state_include = state / "include"
    state_sources = [
        state / "src" / "kilix_state.c",
        state / "src" / "kilix_state_codec.c",
    ]
    for source in state_sources:
        if not source.is_file():
            raise BuildFailure(f"missing kilix-state source {source}")
    state_objects = compile_objects(
        str(compiler), state_sources, cache / "build" / "kilix-state",
        [state_include],
        ["_POSIX_C_SOURCE=200809L", "_DEFAULT_SOURCE", "_FILE_OFFSET_BITS=64"],
        jobs,
    )
    state_so = jni / "libkilix-state.so"
    run([
        str(compiler), "-shared",
        "-Wl,-soname,libkilix-state.so",
        "-Wl,-z,max-page-size=16384",
        "-Wl,-z,common-page-size=16384",
        "-Wl,-z,relro", "-Wl,-z,now",
        "-Wl,--no-undefined",
        "-o", str(state_so),
        *[str(obj) for obj in state_objects],
    ])
    state_pkg = state / "python" / "src" / "kilix_state"
    copy_tree_files(state_pkg, output / "python-site" / "kilix_state", (".py", ".pyi", "py.typed"))

    log("linking libplebdroid_python.so")
    launcher = jni / "libplebdroid_python.so"
    run([
        str(compiler), "-pie", "-fPIE", "-O2",
        "-Wl,-z,max-page-size=16384",
        "-Wl,-z,common-page-size=16384",
        "-Wl,-z,relro", "-Wl,-z,now",
        "-Wl,--enable-new-dtags",
        "-Wl,-rpath,$ORIGIN",
        "-Wl,--no-undefined",
        "-o", str(launcher), str(launcher_obj),
        "-L", str(python_lib),
        "-L", str(jni),
        "-Wl,--no-as-needed",
        "-lpython3.14",
        "-lcrypto_python",
        "-lssl_python",
        "-lsqlite3_python",
        "-lkilix-state",
        "-Wl,--as-needed",
        "-llog", "-lm", "-ldl", "-lz",
    ])
    for name in PYTHON_LIBS:
        copy_file(python_lib / name, jni / name)

    licenses = output / "licenses"
    if licenses.exists():
        shutil.rmtree(licenses)
    license_sources = [
        (pillow_src / "LICENSE", "pillow-LICENSE"),
        (jpeg_src / "LICENSE.md", "libjpeg-turbo-LICENSE.md"),
        (jpeg_src / "README.ijg", "libjpeg-turbo-README.ijg"),
        (webp_src / "COPYING", "libwebp-COPYING"),
        (webp_src / "PATENTS", "libwebp-PATENTS"),
        (freetype_src / "LICENSE.TXT", "freetype-LICENSE.TXT"),
        (freetype_src / "docs" / "FTL.TXT", "freetype-FTL.TXT"),
        (freetype_src / "docs" / "GPLv2.TXT", "freetype-GPLv2.TXT"),
        (png_src / "LICENSE", "libpng-LICENSE"),
        (lcms_src / "LICENSE", "lcms-LICENSE"),
        (python_prefix / "lib" / "python3.14" / "LICENSE.txt", "cpython-LICENSE.txt"),
        (state / "LICENSE", "kilix-state-LICENSE"),
    ]
    for source, name in license_sources:
        if not source.is_file():
            raise BuildFailure(f"missing license file {source}")
        copy_file(source, licenses / name)

    # The NDK CMake toolchain always adds -g. Python extensions are packaged
    # as runtime assets that the Android build does not strip.
    built_elves = [launcher, state_so, *sorted(site_pil.glob(f"*{EXT_SUFFIX}"))]
    run([str(strip_tool), "--strip-debug", *[str(path) for path in built_elves]])

    log("checking ELF outputs")
    artifacts = []
    launcher_elf = require_built_elf(launcher, checks, executable=True)
    state_elf = require_built_elf(state_so, checks, executable=False)
    state_info = parse_elf(state_so)
    check_add(
        checks, "libkilix-state.so.soname",
        state_info["soname"] == "libkilix-state.so",
        f"soname={state_info['soname']}",
    )
    check_add(
        checks, "libkilix-state.so.no_libpython",
        not any("python" in name for name in state_info["needed"]),
        " ".join(state_info["needed"]),
    )
    pillow_modules = (
        "_imaging", "_imagingft", "_imagingcms", "_webp",
        "_imagingmath", "_imagingmorph",
    )
    pillow_elf = {}
    for module in pillow_modules:
        path = site_pil / f"{module}{EXT_SUFFIX}"
        summary = require_built_elf(path, checks, executable=False)
        info = parse_elf(path)
        unexpected = [name for name in info["needed"] if name not in ALLOWED_EXTENSION_NEEDED]
        check_add(
            checks, f"{path.name}.needed_subset", not unexpected,
            f"unexpected={unexpected} needed={info['needed']}",
        )
        init = "PyInit_" + module
        symbols = defined_symbols(str(nm), path)
        check_add(
            checks, f"{path.name}.{init}", init in symbols,
            "present" if init in symbols else "missing from dynamic symbols",
        )
        pillow_elf[path.name] = summary

    for name in PYTHON_LIBS:
        path = jni / name
        info = parse_elf(path)
        description = file_description(path)
        check_add(
            checks, f"{name}.align", info["min_load_align"] >= PAGE_SIZE,
            f"min_load_align={info['min_load_align']} note={info['android_api']}",
        )
        check_add(
            checks, f"{name}.aarch64",
            info["machine"] == EM_AARCH64 and info["type"] == ET_DYN,
            f"machine={info['machine']} type={info['type']}",
        )
        artifacts.append({
            "path": str(path.relative_to(output)),
            "role": "copied-cpython-library",
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "elf": elf_summary(info, description),
        })

    for path in sorted(output.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "manifest.json":
            continue
        relative = str(path.relative_to(output))
        if any(item["path"] == relative for item in artifacts):
            continue
        entry = {
            "path": relative,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        if path.name == "libplebdroid_python.so":
            entry["role"] = "pie-executable"
            entry["elf"] = launcher_elf
        elif path.name == "libkilix-state.so":
            entry["role"] = "shared-library"
            entry["elf"] = state_elf
        elif path.name in pillow_elf:
            entry["role"] = "python-extension"
            entry["elf"] = pillow_elf[path.name]
        elif relative.startswith("licenses/"):
            entry["role"] = "license"
        elif relative.startswith("python-site/"):
            entry["role"] = "python-module"
        artifacts.append(entry)

    failed = [item["name"] for item in checks if not item["passed"]]
    prefix_pc = deps / "lib" / "pkgconfig"
    manifest = {
        "format": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": {
            "abi": ABI,
            "api": API,
            "page_size": PAGE_SIZE,
            "triple": TRIPLE,
            "soabi": SOABI,
        },
        "inputs": {
            "ndk": str(ndk),
            "python": str(python_prefix),
            "deps": str(deps),
            "state": str(state),
            "cache": str(cache),
            "state_commit": state_head,
            "launcher_source": str(app_source()),
        },
        "sources": {
            "pillow": {
                "version": PILLOW_VERSION,
                "url": PILLOW_URL,
                "sha256": PILLOW_SHA256,
                "license": "licenses/pillow-LICENSE",
                "built_from": "sdist",
                "not_used": "PyPI pillow-android is a py3-none-any wrapper and was not used",
            },
            "libjpeg_turbo": {
                "version": JPEG_VERSION,
                "url": JPEG_URL,
                "sha256": JPEG_SHA256,
                "license": [
                    "licenses/libjpeg-turbo-LICENSE.md",
                    "licenses/libjpeg-turbo-README.ijg",
                ],
                "link": "static",
                "archive_sha256": sha256_file(jpeg_archive),
            },
            "libwebp": {
                "version": WEBP_VERSION,
                "url": WEBP_URL,
                "sha256": WEBP_SHA256,
                "license": ["licenses/libwebp-COPYING", "licenses/libwebp-PATENTS"],
                "link": "static",
                "archive_sha256": {path.name: sha256_file(path) for path in webp_libs},
            },
            "freetype": {
                **pic_source_record("freetype", pic_origin),
                "header_version": header_version(
                    freetype_src / "include" / "freetype" / "freetype.h", "FREETYPE_"
                ),
                "prefix_pc_version": read_pc_version(prefix_pc / "freetype2.pc"),
                "prefix_archive_sha256": sha256_file(deps / "lib" / "libfreetype.a"),
                "linked_archive_sha256": sha256_file(freetype_archive),
                "link": "static PIC rebuild from the verified upstream archive",
                "reason": "prefix libfreetype.a contains R_AARCH64_ABS64 and was not linked",
                "license": [
                    "licenses/freetype-LICENSE.TXT",
                    "licenses/freetype-FTL.TXT",
                    "licenses/freetype-GPLv2.TXT",
                ],
            },
            "libpng": {
                **pic_source_record("libpng", pic_origin),
                "header_version": header_version(png_src / "png.h", "PNG_LIBPNG_VER_STRING"),
                "prefix_pc_version": read_pc_version(prefix_pc / "libpng.pc"),
                "prefix_archive_sha256": sha256_file(deps / "lib" / "libpng16.a"),
                "linked_archive_sha256": sha256_file(png_archive),
                "link": "static PIC rebuild; used by FreeType, not by Pillow PNG",
                "reason": "prefix libpng16.a contains R_AARCH64_ABS64 and was not linked",
                "license": "licenses/libpng-LICENSE",
            },
            "lcms": {
                **pic_source_record("lcms", pic_origin),
                "header_version": header_version(lcms_src / "include" / "lcms2.h", "LCMS_VERSION"),
                "prefix_pc_version": read_pc_version(prefix_pc / "lcms2.pc"),
                "prefix_archive_sha256": sha256_file(deps / "lib" / "liblcms2.a"),
                "linked_archive_sha256": sha256_file(lcms_archive),
                "link": "static PIC rebuild from the verified upstream archive",
                "reason": "prefix liblcms2.a contains R_AARCH64_ABS64 and was not linked",
                "license": "licenses/lcms-LICENSE",
            },
            "zlib": {
                "link": "NDK/system libz.so, not bundled",
                "prefix_pc_version": read_pc_version(prefix_pc / "zlib.pc"),
                "reason": "Pillow PNG and ZIP use zlib. The device provides libz.so.",
            },
            "cpython": {
                "version": "3.14.10",
                "prefix": str(python_prefix),
                "copied_not_rebuilt": list(PYTHON_LIBS),
                "license": "licenses/cpython-LICENSE.txt",
                "stdlib": "not packed by this script; parent extracts lib/python3.14",
            },
            "kilix_state": {
                "commit": state_head,
                "path": str(state),
                "license": "licenses/kilix-state-LICENSE",
                "library": f"jniLibs/{ABI}/libkilix-state.so",
                "package": "python-site/kilix_state",
            },
        },
        "artifacts": artifacts,
        "checks": checks,
        "checks_passed": not failed,
        "launch": launch_contract(),
        "features": {
            "pillow": ["jpeg", "png-zlib", "gif", "bmp", "ppm", "webp", "freetype", "lcms2"],
            "pillow_omitted": ["tiff", "jpeg2000", "avif", "imagequant", "xcb", "tk", "raqm"],
            "openssl_legacy_provider": False,
        },
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    log(f"wrote {manifest_path}")
    if failed:
        raise BuildFailure("ELF or export checks failed: " + ", ".join(failed))
    log("build checks passed")


def pic_source_record(name: str, origin: dict[str, str]) -> dict:
    pin = PIC_SOURCES[name]
    return {
        "version": pin["version"],
        "url": pin["url"],
        "sha256": pin["sha256"],
        "sha256_origin": PIC_DIGEST_ORIGIN,
        "archive_obtained_from": origin[name],
        "source": f"extracted fresh from {pin['archive']} each run, top {pin['top']}",
    }


def launch_contract() -> dict:
    smoke = textwrap.dedent(
        """\
        export PLEBDROID_RUNTIME_ROOT="${RUNTIME}/python"
        export KILIX_STATE_LIBRARY="${NATIVE}/libkilix-state.so"
        export KILIX_ANDROID_MODE=1
        export KILIX_HOME="${RUNTIME}/kilix"
        export KILIX95_DIR="${RUNTIME}/apps/kilix-95"
        export PYTHONPATH="${RUNTIME}/kilix/config"
        export KILIX_ANDROID_REQUEST_DIR="${FILES}/activity-requests"
        EXE="${NATIVE}/libplebdroid_python.so"
        "$EXE" -c 'import sys,PIL,PIL.Image,PIL.ImageDraw,PIL.ImageFont; print(sys.version); print(PIL.__version__); im=PIL.Image.new("RGB",(8,8),"red"); d=PIL.ImageDraw.Draw(im); d.rectangle((1,1,6,6), outline="white"); im.save("/data/local/tmp/pleb-pillow.png"); print("png-ok", im.size)'
        "$EXE" -c 'import PIL.Image; PIL.Image.new("RGB",(4,4)).save("/data/local/tmp/a.jpg","JPEG"); PIL.Image.new("RGB",(4,4)).save("/data/local/tmp/a.webp","WEBP"); PIL.Image.open("/data/local/tmp/a.jpg"); PIL.Image.open("/data/local/tmp/a.webp"); print("jpeg-webp-ok")'
        "$EXE" -c 'import kilix_state; kilix_state.default_library(); print("state-ok")'
        "$EXE" "${RUNTIME}/apps/kilix-95/main.py"
        """
    ).strip()
    return {
        "executable": "jniLibs/arm64-v8a/libplebdroid_python.so",
        "exec_form": "execve(executable, [executable, script, ...], env). argv[0] is the PIE path. Catalog arguments follow. Do not System.load this file to start Python.",
        "runtime_root_env": "PLEBDROID_RUNTIME_ROOT",
        "runtime_root": "${RUNTIME}/python, an absolute prefix that contains lib/python3.14",
        "native_libraries": "${NATIVE}, the same directory as the executable (nativeLibraryDir)",
        "python_site_install": "${RUNTIME}/python-site",
        "python_site_search": [
            "$PYTHONPATH (parent sets ${RUNTIME}/kilix/config; kept first)",
            "${RUNTIME}/python/lib/python3.14/site-packages",
            "${RUNTIME}/python-site when that directory exists",
        ],
        "kilix_state_library_env": "KILIX_STATE_LIBRARY=${NATIVE}/libkilix-state.so",
        "inherited_env_not_read_by_helper": [
            "KILIX_ANDROID_MODE",
            "KILIX_HOME",
            "KILIX95_DIR",
            "KILIX_ANDROID_REQUEST_DIR",
        ],
        "smoke": smoke,
        "assumptions": [
            "This lane did not execute the Android binary. The host has no bionic sysroot.",
            "Device API is at least 29 and provides libz, libm, libdl, liblog, and libc.",
            "Extension modules under ${RUNTIME}/python-site are dlopen'd from the app files directory by a raw exec of the PIE.",
            "libpython3.14.so, libcrypto_python.so, libssl_python.so, libsqlite3_python.so, and libkilix-state.so are already loaded via DT_NEEDED and $ORIGIN before those dlopen calls.",
            "If a device rejects dlopen outside nativeLibraryDir, move the Pillow extension files there and keep the PIL package on the module path.",
            "Do not also copy libkilix-state.so into python-site/kilix_state/_libs. The absolute KILIX_STATE_LIBRARY path is the load that works on API 29.",
            "The helper does not create or read the activity-request queue.",
            "OpenSSL legacy.so is not packaged. TIFF, JPEG 2000, AVIF, imagequant, and Tk are not built.",
            "stdout and stderr stay on the PTY: program_name (absolute argv[0], else /proc/self/exe) makes sys.executable non-empty, so CPython does not redirect them to logcat, and stdio is unbuffered.",
        ],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="run host self-tests; with the other flags, run them and then build")
    parser.add_argument("--ndk")
    parser.add_argument("--python")
    parser.add_argument("--deps")
    parser.add_argument("--state")
    parser.add_argument("--cache")
    parser.add_argument("--output")
    parser.add_argument("--jobs", type=int)
    args = parser.parse_args(argv)
    building = any(getattr(args, name) for name in ("ndk", "python", "deps", "state", "cache", "output"))
    if building:
        missing = [
            name for name in ("ndk", "python", "deps", "state", "cache", "output")
            if not getattr(args, name)
        ]
        if missing:
            parser.error("building requires " + ", ".join("--" + name for name in missing))
    elif not args.self_test:
        parser.error("pass --self-test and/or the build paths")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    checks: list[dict] = []
    if args.self_test or args.ndk:
        checks.extend(run_self_tests())
    if args.ndk:
        build(args, checks)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (BuildFailure, HashMismatch) as exc:
        log(f"error: {exc}")
        sys.exit(1)
