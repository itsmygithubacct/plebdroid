#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the optional Debian userspace pieces for Plebdroid.

Produces exactly:
  jniLibs/arm64-v8a/libproot.so         termux/proot, unbundled loader, static talloc
  jniLibs/arm64-v8a/libproot_loader.so  the loader PRoot executes for each guest program
  assets/debian/rootfs.tar.xz           the pinned Debian rootfs, copied after verification
  manifest.json                         hashes, checks and source provenance

Pins live in native/debian/pins.json. Source archives are downloaded into
--cache/downloads, or taken from there when already present with the pinned
SHA-256. The rootfs is never built here; tools/build_debian_rootfs.sh
reproduces it, and --rootfs must name a file with the pinned digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parent.parent
PIN_DIR = PROJECT / 'native/debian'
GENERATOR = 'plebdroid-build-debian-runtime'
OUTPUT_FILES = ('jniLibs/arm64-v8a/libproot.so', 'jniLibs/arm64-v8a/libproot_loader.so',
                'assets/debian/rootfs.tar.xz')
# Compiled-in default only; the launcher always sets PROOT_LOADER explicitly.
UNBUNDLED_LOADER = '/plebdroid-unset-loader'


def log(message: str) -> None:
    print('[debian-runtime] ' + message, flush=True)


def load_pins() -> dict:
    pins = json.loads((PIN_DIR / 'pins.json').read_text())
    if pins.get('format') != 1:
        raise ValueError('Unsupported native/debian/pins.json format')
    return pins


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, expected: str, downloads: Path) -> Path:
    """Return a cached archive with the pinned digest, downloading it if needed."""
    downloads.mkdir(parents=True, exist_ok=True)
    target = downloads / url.rsplit('/', 1)[1]
    if target.is_file() and sha256_file(target) == expected:
        return target
    partial = target.with_name(target.name + '.part')
    log(f'downloading {url}')
    with urllib.request.urlopen(url, timeout=60) as response, open(partial, 'wb') as handle:
        shutil.copyfileobj(response, handle)
    actual = sha256_file(partial)
    if actual != expected:
        partial.unlink()
        raise ValueError(f'{url} has sha256 {actual}, expected {expected}')
    partial.replace(target)
    return target


def unpack(archive: Path, destination: Path, top: str) -> Path:
    if archive.name.endswith('.zip'):
        with zipfile.ZipFile(archive) as source:
            for name in source.namelist():
                parts = Path(name).parts
                if name.startswith('/') or '..' in parts or not parts or parts[0] != top:
                    raise ValueError(f'Unexpected member in {archive.name}: {name}')
            source.extractall(destination)
            # zipfile drops Unix modes; restore the executable bits scripts need.
            for info in source.infolist():
                mode = info.external_attr >> 16
                if mode & 0o111:
                    os.chmod(destination / info.filename, mode & 0o755)
    else:
        with tarfile.open(archive) as source:
            source.extractall(destination, filter='data')
    root = destination / top
    if not root.is_dir():
        raise ValueError(f'{archive.name} does not contain {top}/')
    return root


def build_environment(ndk: Path, extra: dict[str, str]) -> dict[str, str]:
    # Host compiler variables would change the pinned bytes, so start clean.
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C', 'LC_ALL': 'C',
           'HOME': os.environ.get('HOME', '/'), 'PYTHON': sys.executable,
           'SOURCE_DATE_EPOCH': '0'}
    env.update(extra)
    return env


def toolchain(ndk: Path, api: int) -> dict[str, Path]:
    bin_dir = ndk / 'toolchains/llvm/prebuilt/linux-x86_64/bin'
    tools = {'cc': bin_dir / f'aarch64-linux-android{api}-clang', 'ar': bin_dir / 'llvm-ar',
             'strip': bin_dir / 'llvm-strip', 'objcopy': bin_dir / 'llvm-objcopy',
             'objdump': bin_dir / 'llvm-objdump'}
    for name, path in tools.items():
        if not path.is_file():
            raise ValueError(f'NDK tool {name} is missing: {path}')
    return tools


def ndk_revision(ndk: Path) -> str:
    for line in (ndk / 'source.properties').read_text().splitlines():
        key, _, value = line.partition('=')
        if key.strip() == 'Pkg.Revision':
            return value.strip()
    raise ValueError('Cannot read the NDK revision from source.properties')


def run(command: list, cwd: Path, env: dict[str, str], log_path: Path) -> None:
    with open(log_path, 'ab') as output:
        output.write(('\n$ ' + ' '.join(map(str, command)) + '\n').encode())
        output.flush()
        result = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                                stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'{command[0]} failed with status {result.returncode}; see {log_path}')


def build_talloc(pins: dict, tools: dict, source: Path, prefix: Path, jobs: int, log_path: Path) -> None:
    talloc = pins['talloc']
    shutil.copyfile(PIN_DIR / talloc['cross_answers'], source / 'cross-answers.txt')
    env = build_environment(Path(), {'CC': str(tools['cc']), 'AR': str(tools['ar']), 'CFLAGS': '-O2 -fPIC'})
    run(['./configure', f'--prefix={prefix}', '--disable-rpath', '--disable-python',
         '--cross-compile', '--cross-answers=cross-answers.txt'], source, env, log_path)
    run(['make', f'-j{jobs}'], source, env, log_path)
    commands = json.loads((source / 'bin/default/compile_commands.json').read_text())
    objects = []
    for wanted in talloc['objects']:
        expected = (source / wanted).resolve()
        found = []
        for entry in commands:
            if (Path(entry['directory']) / entry['file']).resolve() != expected:
                continue
            arguments = entry['arguments']
            output = next(a[2:] if a != '-o' else arguments[i + 1]
                          for i, a in enumerate(arguments) if a.startswith('-o'))
            found.append(Path(entry['directory']) / output)
        if len(found) != 1 or not found[0].is_file():
            raise RuntimeError(f'Expected one talloc object for {wanted}, found {found}')
        objects.extend(found)
    (prefix / 'lib').mkdir(parents=True)
    (prefix / 'include').mkdir(parents=True)
    run([tools['ar'], 'rcs', prefix / 'lib/libtalloc.a', *objects], source, env, log_path)
    shutil.copyfile(source / 'talloc.h', prefix / 'include/talloc.h')


def build_proot(pins: dict, tools: dict, source: Path, prefix: Path, jobs: int, log_path: Path) -> tuple[Path, Path]:
    proot = pins['proot']
    for patch in proot['patches']:
        run(['patch', '-p1', '--forward', '-i', PIN_DIR / patch], source, build_environment(Path(), {}), log_path)
    page = str(pins['target']['page_size'])
    env = build_environment(Path(), {
        'CC': str(tools['cc']), 'STRIP': str(tools['strip']), 'OBJCOPY': str(tools['objcopy']),
        'OBJDUMP': str(tools['objdump']),
        'CPPFLAGS': f'-I{prefix}/include -DARG_MAX=131072 -DVERSION=\\"{proot["version_define"]}\\"',
        'LDFLAGS': f'-L{prefix}/lib -Wl,-z,max-page-size={page}',
        'LOADER_LDFLAGS': f'-Wl,-z,max-page-size={page}',
        'PROOT_UNBUNDLE_LOADER': UNBUNDLED_LOADER})
    run(['make', '-C', 'src', 'V=1', f'-j{jobs}', 'proot'], source, env, log_path)
    return source / 'src/proot', source / 'src/loader/loader'


# --- ELF checks -------------------------------------------------------------

PT_LOAD, PT_DYNAMIC, PT_INTERP, PT_GNU_STACK = 1, 2, 3, 0x6474e551
DT_NEEDED, DT_STRTAB = 1, 5


def elf_info(path: Path) -> dict:
    data = path.read_bytes()
    if data[:4] != b'\x7fELF' or data[4] != 2 or data[5] != 1:
        raise ValueError(f'{path.name} is not a little-endian ELF64 file')
    e_type, e_machine = struct.unpack_from('<HH', data, 16)
    phoff, = struct.unpack_from('<Q', data, 32)
    phentsize, phnum = struct.unpack_from('<HH', data, 54)
    segments = []
    for index in range(phnum):
        p_type, flags, offset, vaddr, _, filesz, memsz, align = struct.unpack_from(
            '<IIQQQQQQ', data, phoff + index * phentsize)
        segments.append({'type': p_type, 'flags': flags, 'offset': offset, 'vaddr': vaddr,
                         'filesz': filesz, 'memsz': memsz, 'align': align})
    interp = None
    needed = []
    for segment in segments:
        if segment['type'] == PT_INTERP:
            interp = data[segment['offset']:segment['offset'] + segment['filesz']].rstrip(b'\0').decode()
    dynamic = [s for s in segments if s['type'] == PT_DYNAMIC]
    if dynamic:
        entries = []
        start = dynamic[0]['offset']
        for position in range(start, start + dynamic[0]['filesz'], 16):
            tag, value = struct.unpack_from('<qQ', data, position)
            if tag == 0:
                break
            entries.append((tag, value))
        strtab = next(value for tag, value in entries if tag == DT_STRTAB)
        loads = [s for s in segments if s['type'] == PT_LOAD]
        load = next(s for s in loads if s['vaddr'] <= strtab < s['vaddr'] + s['filesz'])
        base = strtab - load['vaddr'] + load['offset']
        for tag, value in entries:
            if tag == DT_NEEDED:
                end = data.index(b'\0', base + value)
                needed.append(data[base + value:end].decode())
    return {'type': e_type, 'machine': e_machine, 'segments': segments,
            'interp': interp, 'needed': needed, 'dynamic': bool(dynamic)}


def check_elf(path: Path, kind: str, page: int) -> dict:
    info = elf_info(path)
    problems = []
    if info['machine'] != 183:
        problems.append('machine is not AArch64')
    loads = [s for s in info['segments'] if s['type'] == PT_LOAD]
    for segment in loads:
        if segment['align'] < page or segment['offset'] % segment['align'] != segment['vaddr'] % segment['align']:
            problems.append(f'PT_LOAD at {segment["vaddr"]:#x} is not {page}-byte aligned')
        if segment['flags'] & 3 == 3:
            problems.append('a PT_LOAD segment is both writable and executable')
    stack = [s for s in info['segments'] if s['type'] == PT_GNU_STACK]
    if not stack or stack[0]['flags'] & 1:
        problems.append('stack is not marked non-executable')
    if kind == 'proot':
        if info['type'] != 3 or info['interp'] != '/system/bin/linker64':
            problems.append('PRoot is not an Android PIE using /system/bin/linker64')
        if not set(info['needed']) <= {'libc.so', 'libdl.so'}:
            problems.append(f'unexpected shared libraries {info["needed"]}')
    else:
        if info['type'] != 2 or info['interp'] is not None or info['dynamic']:
            problems.append('the loader is not a static ET_EXEC')
    if problems:
        raise ValueError(f'{path.name}: ' + '; '.join(problems))
    return {'elf_type': {2: 'ET_EXEC', 3: 'ET_DYN'}[info['type']], 'interp': info['interp'],
            'needed': info['needed'], 'load_align': sorted({s['align'] for s in loads})}


# --- rootfs ---------------------------------------------------------------

def verify_rootfs(path: Path, pins: dict) -> dict:
    rootfs = pins['rootfs']
    if path.is_symlink() or not path.is_file():
        raise ValueError('--rootfs must be a regular file')
    if path.stat().st_size != rootfs['xz_bytes'] or sha256_file(path) != rootfs['xz_sha256']:
        raise ValueError(f'{path} is not the pinned rootfs archive {rootfs["xz_sha256"]}')
    digest = hashlib.sha256()
    size = 0
    with lzma.open(path) as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
            size += len(block)
    if size != rootfs['tar_bytes'] or digest.hexdigest() != rootfs['tar_sha256']:
        raise ValueError('The rootfs archive does not decompress to the pinned tar')
    return {'xz_sha256': rootfs['xz_sha256'], 'tar_sha256': rootfs['tar_sha256']}


# --- driver ---------------------------------------------------------------

def prepare_output(output: Path) -> Path:
    """Return a fresh staging directory; refuse to replace unrelated content."""
    if output.exists():
        manifest = output / 'manifest.json'
        owned = manifest.is_file() and json.loads(manifest.read_text()).get('generator') == GENERATOR
        if any(output.iterdir()) and not owned:
            raise ValueError(f'{output} exists and was not produced by this tool')
    staging = output.with_name(output.name + '.new')
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    return staging


def build(args: argparse.Namespace) -> Path:
    pins = load_pins()
    page = pins['target']['page_size']
    ndk = args.ndk.resolve()
    revision = ndk_revision(ndk)
    drift = []
    if revision != pins['ndk']['revision']:
        drift.append(f'NDK {revision} differs from the pinned {pins["ndk"]["revision"]}')
    tools = toolchain(ndk, pins['target']['api'])
    rootfs = verify_rootfs(args.rootfs, pins)
    log('rootfs archive and decompressed tar match the pins')
    downloads = args.cache / 'downloads'
    archives = {name: fetch(pins[name]['url'], pins[name]['sha256'], downloads)
                for name in ('proot', 'talloc')}
    staging = prepare_output(args.output)
    work_root = args.cache / 'work'
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_root, prefix='debian-') as temporary:
        work = Path(temporary)
        log_path = args.cache / 'build-debian-runtime.log'
        log_path.write_text('')
        talloc_source = unpack(archives['talloc'], work, pins['talloc']['top'])
        proot_source = unpack(archives['proot'], work, pins['proot']['top'])
        prefix = work / 'prefix'
        log('building talloc')
        build_talloc(pins, tools, talloc_source, prefix, args.jobs, log_path)
        log('building PRoot and its loader')
        proot, loader = build_proot(pins, tools, proot_source, prefix, args.jobs, log_path)
        natives = staging / 'jniLibs/arm64-v8a'
        natives.mkdir(parents=True)
        for built, name in ((proot, 'libproot.so'), (loader, 'libproot_loader.so')):
            run([tools['strip'], '-o', natives / name, built], work, build_environment(ndk, {}), log_path)
            os.chmod(natives / name, 0o755)
    elf = {'libproot.so': check_elf(natives / 'libproot.so', 'proot', page),
           'libproot_loader.so': check_elf(natives / 'libproot_loader.so', 'loader', page)}
    asset = staging / 'assets' / pins['rootfs']['asset']
    asset.parent.mkdir(parents=True)
    shutil.copyfile(args.rootfs, asset)
    artifacts = []
    for name in OUTPUT_FILES:
        path = staging / name
        record = {'path': name, 'bytes': path.stat().st_size, 'sha256': sha256_file(path)}
        library = Path(name).name
        if library in pins['outputs']:
            record['pinned_sha256'] = pins['outputs'][library]
            record['elf'] = elf[library]
            if record['sha256'] != pins['outputs'][library]:
                drift.append(f'{library} sha256 {record["sha256"]} differs from the pinned build')
        artifacts.append(record)
    if drift and not args.allow_output_drift:
        raise ValueError('Output differs from the reviewed pins: ' + '; '.join(drift)
                         + '. Rebuild with the pinned NDK or pass --allow-output-drift to record it.')
    patches = [{'path': 'native/debian/' + patch, 'sha256': sha256_file(PIN_DIR / patch)}
               for patch in pins['proot']['patches']]
    manifest = {
        'format': 1, 'generator': GENERATOR, 'checks_passed': True,
        'target': pins['target'], 'ndk': {'revision': revision},
        'drift': drift,
        'artifacts': artifacts,
        'sources': {
            'proot': {key: pins['proot'][key] for key in
                      ('upstream', 'tag', 'commit', 'url', 'sha256', 'license', 'notice')} | {'patches': patches},
            'talloc': {key: pins['talloc'][key] for key in
                       ('upstream', 'url', 'sha256', 'objects', 'license', 'notice')},
            'rootfs': pins['rootfs'] | {'verified': rootfs},
        },
        'build': {'unbundled_loader_default': UNBUNDLED_LOADER, 'max_page_size': page,
                  'pins_sha256': sha256_file(PIN_DIR / 'pins.json')},
    }
    (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    if args.output.exists():
        shutil.rmtree(args.output)
    staging.replace(args.output)
    log(f'wrote {args.output}')
    return args.output


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ndk', required=True, type=Path, help='Android NDK root (pinned 30.0.16248370)')
    parser.add_argument('--cache', required=True, type=Path, help='download and work directory')
    parser.add_argument('--output', required=True, type=Path, help='directory to replace with the outputs')
    parser.add_argument('--rootfs', required=True, type=Path, help='the pinned rootfs .tar.xz')
    parser.add_argument('--jobs', type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument('--allow-output-drift', action='store_true',
                        help='record, rather than reject, outputs that differ from the reviewed hashes')
    args = parser.parse_args(argv)
    args.cache = args.cache.resolve()
    args.output = args.output.resolve()
    args.rootfs = args.rootfs.resolve()
    return args


def main(argv: list[str]) -> int:
    try:
        build(parse_args(argv))
    except (ValueError, RuntimeError, OSError) as error:
        print(f'build_debian_runtime: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
