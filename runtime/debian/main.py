#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Plebdroid Debian terminal.

Installs the packaged Debian rootfs once into app-private storage, then runs
it under the packaged PRoot. Run by libplebdroid_python.so in a Kilix tab.

Layout under ${FILES}/debian:
  source.json        written by the Activity: APK path, native dir, DNS servers
  installed.json     install record; written before the staging rename
  rootfs/            the installed system; never replaced or deleted here
  rootfs.staging-*   an install in progress; stale ones are removed under the lock
  install.lock       flock serializing installs between tabs
  tmp/ shm/ proc/    PRoot temporary files, /dev/shm, /proc stand-ins
  resolv.conf        Android's current DNS servers, bound over /etc/resolv.conf
  last-error.txt     the most recent launcher failure
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import hashlib
import ipaddress
import json
import lzma
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import traceback
import zipfile

# Pinned in native/debian/pins.json; tests check that the two agree.
ROOTFS_SNAPSHOT = '20261027T000000Z'
ROOTFS_XZ_SHA256 = '8c97363ebd6bf120f3b76fce6efa21bb109c7d34eeffb7dc9786a9d12c52917e'
ROOTFS_XZ_BYTES = 34598288
ROOTFS_TAR_SHA256 = 'c621d002e6280bc8ebd68992c087bf493cd7a1b6c58709ece62c3ffd9c328011'
ROOTFS_TAR_BYTES = 171671040
ROOTFS_FILE_BYTES = 165980281
ROOTFS_MEMBERS = 7913
ASSET_MEMBER = 'assets/debian/rootfs.tar.xz'

PROOT = 'libproot.so'
LOADER = 'libproot_loader.so'
HOSTNAME = 'plebdroid'
# Space for the extracted system, per-entry filesystem overhead, and the
# first `apt update` (about 180 MB of lists and caches).
FREE_SPACE_MARGIN = 256 << 20
GUEST_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
GUEST_HOME = '/root'
GUEST_DOCUMENTS = GUEST_HOME + '/Documents'
QUICK_EXIT_SECONDS = 10


class DebianError(Exception):
    """A failure the user can read and act on."""


@dataclass(frozen=True)
class Limits:
    members: int = 20000
    total_bytes: int = 512 << 20
    file_bytes: int = 256 << 20
    depth: int = 64
    name_bytes: int = 4096


@dataclass(frozen=True)
class Layout:
    files: Path
    home: Path

    @property
    def debian(self) -> Path: return self.files / 'debian'
    @property
    def rootfs(self) -> Path: return self.debian / 'rootfs'
    @property
    def record(self) -> Path: return self.debian / 'installed.json'
    @property
    def lock(self) -> Path: return self.debian / 'install.lock'
    @property
    def source(self) -> Path: return self.debian / 'source.json'
    @property
    def tmp(self) -> Path: return self.debian / 'tmp'
    @property
    def shm(self) -> Path: return self.debian / 'shm'
    @property
    def proc(self) -> Path: return self.debian / 'proc'
    @property
    def resolv(self) -> Path: return self.debian / 'resolv.conf'
    @property
    def last_error(self) -> Path: return self.debian / 'last-error.txt'
    @property
    def documents(self) -> Path: return self.home / 'Documents'


STAGING_PREFIX = 'rootfs.staging-'


@dataclass(frozen=True)
class Source:
    apk: Path
    native_dir: Path
    dns_servers: tuple[str, ...]


# --- small helpers ---------------------------------------------------------

def say(message: str) -> None:
    print(message, flush=True)


def write_all(fd: int, data: bytes) -> None:
    """os.write may store fewer bytes than asked; finish the whole buffer."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, 'write stored no bytes')
        view = view[written:]


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path: Path) -> Path:
    """Create or accept a real directory; never follow a symlink in its place."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise DebianError(f'{path} must be a directory, not a link or file')
    return path


def read_json(path: Path, limit: int = 64 << 10) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise DebianError(f'{path.name} is too large')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise DebianError(f'{path.name} is not a JSON object')
    return value


def human(size: int) -> str:
    return f'{size / (1 << 20):.0f} MiB'


# --- source.json and ABI ---------------------------------------------------

def read_source(layout: Layout, executable_dir: Path | None) -> Source:
    try:
        raw = read_json(layout.source)
    except FileNotFoundError:
        raise DebianError('The app did not provide debian/source.json; reopen Plebdroid') from None
    except (OSError, ValueError) as error:
        raise DebianError(f'debian/source.json is unreadable: {error}') from None
    if raw.get('format') != 1:
        raise DebianError('debian/source.json has an unsupported format')
    apk, native = raw.get('apk'), raw.get('native_dir')
    if not (isinstance(apk, str) and apk.startswith('/') and isinstance(native, str) and native.startswith('/')):
        raise DebianError('debian/source.json must name absolute apk and native_dir paths')
    servers = raw.get('dns_servers', [])
    if not isinstance(servers, list) or len(servers) > 16:
        raise DebianError('debian/source.json dns_servers must be a list')
    dns = []
    for server in servers:
        try:
            address = ipaddress.ip_address(server.split('%', 1)[0] if isinstance(server, str) else '')
        except ValueError:
            raise DebianError(f'Invalid DNS server from Android: {server!r}') from None
        # A scoped IPv6 address needs its zone for glibc; keep the validated text.
        text = server if isinstance(server, str) and '%' in server and address.version == 6 else str(address)
        if text not in dns:
            dns.append(text)
    native_dir = Path(native)
    if executable_dir is not None and os.path.realpath(native_dir) != os.path.realpath(executable_dir):
        raise DebianError('Plebdroid was updated since this tab was prepared; close it and open Debian again')
    return Source(Path(apk), native_dir, tuple(dns))


def getprop(name: str) -> str | None:
    try:
        result = subprocess.run(['/system/bin/getprop', name], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def abi_problem(machine: str, primary_abi: str | None) -> str | None:
    """Explain why this process cannot host the ARM64 Debian userspace, if so."""
    if primary_abi and primary_abi != 'arm64-v8a':
        return (f'This device runs {primary_abi} and executes Plebdroid through ARM translation. '
                'Debian needs PRoot to trace native ARM64 processes, which translation does not '
                'provide. Use a physical ARM64 device.')
    if machine != 'aarch64':
        return f'Debian needs an ARM64 kernel; this process reports {machine}.'
    return None


def packaged_helpers(source: Source) -> tuple[Path, Path]:
    helpers = []
    for name in (PROOT, LOADER):
        path = source.native_dir / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise DebianError(f'{name} is not packaged in this build') from None
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
            raise DebianError(f'{name} in the native library directory is not an executable file')
        helpers.append(path)
    return helpers[0], helpers[1]


# --- install ---------------------------------------------------------------

class InstallLock:
    def __init__(self, path: Path, wait: bool = True):
        self.path, self.wait, self.fd = path, wait, -1

    def __enter__(self) -> 'InstallLock':
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not self.wait:
                os.close(self.fd)
                raise DebianError('Another Debian tab is installing Debian') from None
            say('Another Debian tab is installing Debian; waiting for it to finish...')
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc) -> None:
        os.close(self.fd)


class Progress:
    def __init__(self, total: int, stream=None):
        self.total, self.stream = max(total, 1), stream or sys.stdout
        self.last = -1

    def update(self, done: int) -> None:
        percent = min(100, done * 100 // self.total)
        if percent != self.last:
            self.last = percent
            self.stream.write(f'\rInstalling Debian: {percent:3d}%')
            self.stream.flush()

    def finish(self) -> None:
        self.stream.write('\n')
        self.stream.flush()


class CountingReader:
    """Report compressed bytes consumed, which tracks extraction progress."""
    def __init__(self, raw, progress: Progress | None):
        self.raw, self.progress, self.count = raw, progress, 0

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        self.count += len(data)
        if self.progress:
            self.progress.update(self.count)
        return data

    def readable(self) -> bool:
        return True


class HashingReader:
    def __init__(self, raw):
        self.raw, self.digest, self.count = raw, hashlib.sha256(), 0

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data

    def drain(self) -> None:
        while self.read(1 << 20):
            pass


def member_parts(name: str, limits: Limits) -> list[str]:
    if '\0' in name or len(name.encode('utf-8', 'surrogateescape')) > limits.name_bytes:
        raise DebianError(f'Unsafe archive name {name!r}')
    if name.startswith('/'):
        raise DebianError(f'Absolute archive path {name!r}')
    parts = [part for part in name.split('/') if part not in ('', '.')]
    if '..' in parts:
        raise DebianError(f'Archive path leaves the rootfs: {name!r}')
    if len(parts) > limits.depth:
        raise DebianError(f'Archive path is too deep: {name!r}')
    return parts


class Extractor:
    """Write a tar stream into a fresh directory without following any link.

    Every path is walked from the root with O_NOFOLLOW directory descriptors,
    so a symlink created by an earlier member cannot redirect a later one.
    Symlink targets are kept verbatim: they are guest paths that PRoot
    resolves inside the rootfs. Hard links become copies, because Android
    denies link(2) to apps. Device and FIFO entries are left out (PRoot binds
    the host /dev). Ownership is ignored; setuid, setgid and sticky bits are
    dropped and the owner always keeps read/write (and search) access.
    """

    DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def __init__(self, root: Path, limits: Limits = Limits()):
        os.mkdir(root, 0o700)
        self.root = root
        self.limits = limits
        self.root_fd = os.open(root, self.DIR_FLAGS)
        self.files: set[tuple[str, ...]] = set()
        self.members = 0
        self.bytes = 0
        self.skipped = {'devices': 0, 'fifos': 0}
        self.hardlinks = 0
        self.symlinks = 0
        self.root_mode = 0o755

    def close(self) -> None:
        if self.root_fd >= 0:
            os.fchmod(self.root_fd, (self.root_mode & 0o777) | 0o700)
            os.close(self.root_fd)
            self.root_fd = -1

    def _open_dir(self, parts: list[str], create: bool) -> int:
        fd = os.dup(self.root_fd)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, self.DIR_FLAGS, dir_fd=fd)
                except OSError as error:
                    if error.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise DebianError('Archive path passes through a non-directory: '
                                          + '/'.join(parts)) from None
                    raise
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _charge(self, size: int, name: str) -> None:
        if size > self.limits.file_bytes:
            raise DebianError(f'Archive file is too large: {name}')
        self.bytes += size
        if self.bytes > self.limits.total_bytes:
            raise DebianError('Archive content exceeds the size limit')

    def _write(self, parent: int, leaf: str, source, size: int, mode: int, mtime: float) -> None:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        try:
            remaining = size
            while remaining:
                block = source.read(min(remaining, 1 << 20))
                if not block:
                    raise DebianError('Archive ended inside a file')
                write_all(fd, block)
                remaining -= len(block)
            os.fchmod(fd, (mode & 0o777) | 0o600)
            os.utime(fd, (mtime, mtime))
        finally:
            os.close(fd)

    def add(self, archive: tarfile.TarFile, member: tarfile.TarInfo) -> None:
        self.members += 1
        if self.members > self.limits.members:
            raise DebianError('Archive has too many entries')
        parts = member_parts(member.name, self.limits)
        if member.isdir():
            if not parts:
                self.root_mode = member.mode
                return
            fd = self._open_dir(parts, create=True)
            try:
                os.fchmod(fd, (member.mode & 0o777) | 0o700)
            finally:
                os.close(fd)
            return
        if member.ischr() or member.isblk():
            self.skipped['devices'] += 1
            return
        if member.isfifo():
            self.skipped['fifos'] += 1
            return
        if not parts:
            raise DebianError('Archive replaces the rootfs directory')
        if not (member.isreg() or member.issym() or member.islnk()):
            raise DebianError(f'Unsupported archive entry type for {member.name}')
        parent = self._open_dir(parts[:-1], create=True)
        try:
            leaf = parts[-1]
            if member.issym():
                target = member.linkname
                if not target or '\0' in target or len(target.encode('utf-8', 'surrogateescape')) > self.limits.name_bytes:
                    raise DebianError(f'Unsafe symlink target for {member.name}')
                os.symlink(target, leaf, dir_fd=parent)
                self.symlinks += 1
            elif member.islnk():
                target = tuple(member_parts(member.linkname, self.limits))
                if target not in self.files:
                    raise DebianError(f'Hard link {member.name} does not name an earlier regular file')
                source_parent = self._open_dir(list(target[:-1]), create=False)
                try:
                    source_fd = os.open(target[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=source_parent)
                finally:
                    os.close(source_parent)
                with os.fdopen(source_fd, 'rb') as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise DebianError(f'Hard link {member.name} does not name a regular file')
                    self._charge(info.st_size, member.name)
                    self._write(parent, leaf, handle, info.st_size, info.st_mode, info.st_mtime)
                self.files.add(tuple(parts))
                self.hardlinks += 1
            else:
                self._charge(member.size, member.name)
                stream = archive.extractfile(member)
                self._write(parent, leaf, stream, member.size, member.mode, member.mtime)
                self.files.add(tuple(parts))
        except FileExistsError:
            raise DebianError(f'Archive contains {member.name} more than once') from None
        finally:
            os.close(parent)


def extract_stream(stream, destination: Path, limits: Limits = Limits()) -> Extractor:
    """Extract a tar byte stream into a directory that must not exist yet."""
    extractor = Extractor(destination, limits)
    try:
        with tarfile.open(fileobj=stream, mode='r|') as archive:
            for member in archive:
                extractor.add(archive, member)
    except tarfile.TarError as error:
        raise DebianError(f'The rootfs archive is damaged: {error}') from None
    except EOFError:
        raise DebianError('The rootfs archive is truncated') from None
    finally:
        extractor.close()
    return extractor


def free_bytes(path: Path) -> int:
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def required_bytes() -> int:
    # Each entry costs at least one filesystem block beyond its contents.
    return ROOTFS_FILE_BYTES + ROOTFS_MEMBERS * 4096 + FREE_SPACE_MARGIN


def open_asset(apk: Path) -> tuple[zipfile.ZipFile, zipfile.ZipInfo]:
    try:
        bundle = zipfile.ZipFile(apk)
    except (OSError, zipfile.BadZipFile) as error:
        raise DebianError(f'Cannot read the installed APK: {error}') from None
    try:
        info = bundle.getinfo(ASSET_MEMBER)
    except KeyError:
        bundle.close()
        raise DebianError('This build does not include the Debian rootfs asset') from None
    if info.file_size != ROOTFS_XZ_BYTES:
        bundle.close()
        raise DebianError('The packaged Debian rootfs has the wrong size')
    return bundle, info


def verify_asset(bundle: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    digest = hashlib.sha256()
    with bundle.open(info) as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    if digest.hexdigest() != ROOTFS_XZ_SHA256:
        raise DebianError('The packaged Debian rootfs does not match its pinned SHA-256')


def remove_tree(path: Path) -> None:
    """Remove an app-created staging tree without following links."""
    # Extraction keeps owner write on every directory, so no chmod is needed.
    if shutil.rmtree.avoids_symlink_attacks:
        shutil.rmtree(path)
    else:  # pragma: no cover - CPython on Linux always has the fd variant
        raise DebianError('Cannot remove an incomplete install safely on this platform')


def seed_rootfs(root: Path) -> None:
    """Make sure the Documents mount point exists in a fresh staging tree."""
    fd = os.open(root, Extractor.DIR_FLAGS)
    try:
        home = os.open(GUEST_HOME.lstrip('/'), Extractor.DIR_FLAGS, dir_fd=fd)
        try:
            os.mkdir('Documents', 0o700, dir_fd=home)
        except FileExistsError:
            pass
        finally:
            os.close(home)
    finally:
        os.close(fd)


def write_record(layout: Layout, **fields) -> None:
    record = {'format': 1, 'snapshot': ROOTFS_SNAPSHOT, 'xz_sha256': ROOTFS_XZ_SHA256,
              'tar_sha256': ROOTFS_TAR_SHA256} | fields
    atomic_write(layout.record, (json.dumps(record, indent=2, sort_keys=True) + '\n').encode())
    fsync_directory(layout.debian)


def stale_staging(layout: Layout, keep: str | None = None) -> list[Path]:
    found = []
    for entry in os.scandir(layout.debian):
        if entry.name.startswith(STAGING_PREFIX) and entry.name != keep:
            if entry.is_dir(follow_symlinks=False):
                found.append(Path(entry.path))
            else:
                raise DebianError(f'Unexpected non-directory {entry.name} in the Debian directory')
    return sorted(found)


def installed_notice(layout: Layout) -> str | None:
    try:
        record = read_json(layout.record)
    except (OSError, ValueError, DebianError):
        return 'Debian is installed, but its install record is missing; the system is left as it is.'
    if record.get('xz_sha256') != ROOTFS_XZ_SHA256:
        return (f'Debian was installed from snapshot {record.get("snapshot", "unknown")}. This app '
                f'carries {ROOTFS_SNAPSHOT}; your installed system and its changes are kept. '
                'Use apt to update it.')
    return None


def ensure_installed(layout: Layout, source: Source | None, *, wait: bool = True,
                     limits: Limits = Limits(), out=None) -> str:
    """Install once. Returns 'present', 'completed' or 'installed'."""
    out = out or sys.stdout
    private_directory(layout.debian)
    with InstallLock(layout.lock, wait):
        try:
            info = os.lstat(layout.rootfs)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISDIR(info.st_mode):
                raise DebianError(f'{layout.rootfs} is not a directory; it was left untouched')
            return 'present'
        try:
            record = read_json(layout.record)
        except FileNotFoundError:
            record = None
        except (OSError, ValueError, DebianError):
            record = None  # A record without a rootfs carries no user data.
        staged = record.get('staging') if record else None
        if isinstance(staged, str) and staged.startswith(STAGING_PREFIX) and '/' not in staged:
            candidate = layout.debian / staged
            if candidate.is_dir() and not candidate.is_symlink():
                # The install finished and its record was written; only the rename was lost.
                os.rename(candidate, layout.rootfs)
                fsync_directory(layout.debian)
                write_record(layout, installed_at=record.get('installed_at'))
                for leftover in stale_staging(layout):
                    remove_tree(leftover)
                out.write('Finished an interrupted Debian installation.\n')
                return 'completed'
        for leftover in stale_staging(layout):
            out.write(f'Removing an incomplete installation ({leftover.name}).\n')
            remove_tree(leftover)
        if record is not None:
            os.unlink(layout.record)
        if source is None:
            raise DebianError('Debian is not installed and the app did not provide its rootfs')
        available = free_bytes(layout.debian)
        if available < required_bytes():
            raise DebianError(f'Installing Debian needs {human(required_bytes())} free; '
                              f'{human(available)} is available. Free some space and try again.')
        bundle, asset = open_asset(source.apk)
        with bundle:
            out.write('Checking the packaged Debian rootfs...\n')
            out.flush()
            verify_asset(bundle, asset)
            staging = layout.debian / f'{STAGING_PREFIX}{os.getpid()}-{time.monotonic_ns()}'
            progress = Progress(asset.file_size, out)
            try:
                with bundle.open(asset) as raw:
                    hashed = HashingReader(lzma.LZMAFile(CountingReader(raw, progress)))
                    result = extract_stream(hashed, staging, limits)
                    hashed.drain()
                progress.finish()
                if hashed.count != ROOTFS_TAR_BYTES or hashed.digest.hexdigest() != ROOTFS_TAR_SHA256:
                    raise DebianError('The extracted Debian archive does not match its pinned SHA-256')
                seed_rootfs(staging)
                os.sync()
                write_record(layout, staging=staging.name, installed_at=int(time.time()),
                             skipped=result.skipped, hardlinks_copied=result.hardlinks)
                os.rename(staging, layout.rootfs)
                fsync_directory(layout.debian)
                write_record(layout, installed_at=int(time.time()), skipped=result.skipped,
                             hardlinks_copied=result.hardlinks)
            except BaseException:
                if staging.exists() and not layout.rootfs.exists():
                    remove_tree(staging)
                    try:
                        os.unlink(layout.record)
                    except FileNotFoundError:
                        pass
                raise
        out.write(f'Debian {ROOTFS_SNAPSHOT} installed ({result.members} entries, '
                  f'{result.hardlinks} hard link copied, {result.skipped["devices"]} device entries '
                  'left to the host /dev).\n')
        return 'installed'


# --- launch ----------------------------------------------------------------

def resolv_conf(servers: tuple[str, ...]) -> str:
    lines = ['# Written by Plebdroid from Android\'s active network at each Debian launch.']
    lines += [f'nameserver {server}' for server in servers[:3]]
    return '\n'.join(lines) + '\n'


def boot_seconds() -> float:
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


def proc_stubs(cpus: int) -> dict[str, tuple[str, str]]:
    """Stand-ins for /proc files Android hides from apps (sepolicy dontaudit list)."""
    up = boot_seconds()
    ticks = int(up * 100)
    cpu_lines = ''.join(f'cpu{index} {ticks // 10} 0 {ticks // 20} {ticks} 0 0 0 0 0 0\n'
                        for index in range(cpus))
    total = ticks * cpus
    return {
        'loadavg': ('/proc/loadavg', '0.10 0.10 0.10 1/100 1\n'),
        'stat': ('/proc/stat', f'cpu  {total // 10} 0 {total // 20} {total} 0 0 0 0 0 0\n{cpu_lines}'
                 f'intr 0\nctxt 0\nbtime {int(time.time() - up)}\nprocesses 1\n'
                 'procs_running 1\nprocs_blocked 0\nsoftirq 0 0 0 0 0 0 0 0 0 0 0\n'),
        'uptime': ('/proc/uptime', f'{up:.2f} {up * cpus * 0.9:.2f}\n'),
        'vmstat': ('/proc/vmstat', 'nr_free_pages 0\npgpgin 0\npgpgout 0\npswpin 0\npswpout 0\n'),
    }


def readable(path: str) -> bool:
    try:
        with open(path, 'rb') as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def guest_term(rootfs: Path, term: str) -> str:
    """Keep the tab's TERM when Debian has its terminfo entry, else a safe one.

    The minimal rootfs lacks xterm-kitty (ncurses-term adds it). Entries are
    checked with lstat only, since guest symlinks are guest paths."""
    if term and term.isprintable() and '/' not in term and not term.startswith('.'):
        for directory in ('etc/terminfo', 'usr/lib/terminfo', 'usr/share/terminfo'):
            for bucket in (term[0], f'{ord(term[0]):x}'):
                if os.path.lexists(rootfs / directory / bucket / term):
                    return term
    return 'xterm-256color'


@dataclass(frozen=True)
class Launch:
    argv: list[str]
    env: dict[str, str]
    executable: str


def prepare_launch(layout: Layout, source: Source, *, term: str = 'xterm-256color',
                   command: list[str] | None = None, readable_check=readable,
                   cpus: int | None = None, extra_proot_args: tuple[str, ...] = ()) -> Launch:
    proot, loader = packaged_helpers(source)
    term = guest_term(layout.rootfs, term)
    for directory in (layout.tmp, layout.shm, layout.proc):
        private_directory(directory)
    private_directory(layout.home)
    private_directory(layout.documents)
    atomic_write(layout.resolv, resolv_conf(source.dns_servers).encode(), 0o644)
    binds = []
    for name, (target, content) in sorted(proc_stubs(cpus or os.cpu_count() or 1).items()):
        if not readable_check(target):
            stub = layout.proc / name
            atomic_write(stub, content.encode(), 0o444)
            binds.append(f'--bind={stub}:{target}')
    argv = [str(proot), '--kill-on-exit', '--link2symlink', '--sysvipc', '-L', '-0',
            f'--kernel-release=\\Linux\\{HOSTNAME}\\6.1.0-plebdroid\\#1 SMP PREEMPT\\aarch64\\localdomain\\-1\\',
            *extra_proot_args,
            f'--rootfs={layout.rootfs}', f'--cwd={GUEST_HOME}',
            '--bind=/dev', '--bind=/proc', '--bind=/sys',
            '--bind=/dev/urandom:/dev/random', '--bind=/proc/self/fd:/dev/fd',
            '--bind=/proc/self/fd/0:/dev/stdin', '--bind=/proc/self/fd/1:/dev/stdout',
            '--bind=/proc/self/fd/2:/dev/stderr',
            f'--bind={layout.shm}:/dev/shm', *binds,
            f'--bind={layout.documents}:{GUEST_DOCUMENTS}',
            f'--bind={layout.resolv}:/etc/resolv.conf',
            # The guest gets a clean environment: no host LD_*, Python or
            # Android variables reach Debian programs.
            '/usr/bin/env', '-i', f'HOME={GUEST_HOME}', f'TERM={term}', 'COLORTERM=truecolor',
            'LANG=C.UTF-8', f'PATH={GUEST_PATH}', 'USER=root', 'LOGNAME=root', 'SHELL=/bin/bash',
            *(command or ['/bin/bash', '-l'])]
    env = {'PATH': '/system/bin', 'TERM': term, 'PROOT_LOADER': str(loader),
           'PROOT_TMP_DIR': str(layout.tmp), 'TMPDIR': str(layout.tmp)}
    return Launch(argv, env, str(proot))


def _controlling_tty() -> int | None:
    """Return stdin when this process can hand the terminal to the guest."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd) or os.getsid(0) != os.getpid():
            return None
        os.tcgetpgrp(fd)
    except (AttributeError, OSError, ValueError):
        return None
    return fd


def _reap(process: subprocess.Popen, group: int | None) -> int:
    """Wait for the guest, continuing PRoot if Ctrl-Z stopped it."""
    while True:
        try:
            _pid, raw = os.waitpid(process.pid, os.WUNTRACED)
        except InterruptedError:
            continue
        if os.WIFSTOPPED(raw):
            # The stopped process is PRoot, not a guest job. Leave it stopped
            # and the launcher waits forever because wait() ignores stops.
            try:
                if group:
                    os.killpg(group, signal.SIGCONT)
                else:
                    os.kill(process.pid, signal.SIGCONT)
            except OSError:
                pass
            continue
        if os.WIFEXITED(raw):
            status = os.WEXITSTATUS(raw)
        elif os.WIFSIGNALED(raw):
            status = -os.WTERMSIG(raw)
        else:
            continue
        process.returncode = status
        return status


def run_session(launch: Launch) -> tuple[int, float]:
    """Run PRoot in the tab and return (exit status, seconds)."""
    started = time.monotonic()
    previous = {}
    # A Python handler (unlike SIG_IGN) resets to the default in the child at
    # exec, so ^C and ^\ reach Debian while this launcher keeps waiting.
    # SIGTSTP is caught only so the launcher itself does not stop. The reap
    # loop continues a stopped child; ignoring that stop wedges the tab.
    for number in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
        previous[number] = signal.signal(number, lambda *_: None)
    tty = _controlling_tty()
    group = None
    try:
        process = subprocess.Popen(launch.argv, executable=launch.executable, env=launch.env,
                                   close_fds=True, preexec_fn=(os.setpgrp if tty is not None else None))
        if tty is not None:
            try:
                os.setpgid(process.pid, process.pid)
                os.tcsetpgrp(tty, process.pid)
                group = process.pid
            except OSError:
                try:
                    os.setpgid(process.pid, os.getpgrp())
                except OSError:
                    pass
        status = _reap(process, group)
    finally:
        if group is not None and tty is not None:
            try:
                os.tcsetpgrp(tty, os.getpgrp())
            except OSError:
                pass
        for number, handler in previous.items():
            signal.signal(number, handler)
    return status, time.monotonic() - started


def record_error(layout: Layout, message: str, detail: str = '') -> None:
    try:
        private_directory(layout.debian)
        text = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + '\n' + message + '\n'
        atomic_write(layout.last_error, (text + detail).encode())
    except (OSError, DebianError):
        pass


def hold(prompt: str = 'Press Enter to close this tab.') -> None:
    say(prompt)
    try:
        sys.stdin.readline()
    except (OSError, ValueError, KeyboardInterrupt):
        pass


def environment_layout(environ) -> Layout:
    files, home = environ.get('PLEBDROID_FILES'), environ.get('HOME')
    if not files or not files.startswith('/') or not home or not home.startswith('/'):
        raise DebianError('PLEBDROID_FILES and HOME must be absolute paths')
    return Layout(Path(files), Path(home))


def main(environ=os.environ, executable: str | None = None, machine: str | None = None,
         primary_abi: str | None = None, run=run_session, wait_for_close=hold) -> int:
    layout = None
    try:
        layout = environment_layout(environ)
        problem = abi_problem(machine or os.uname().machine,
                              primary_abi if primary_abi is not None else getprop('ro.product.cpu.abi'))
        if problem:
            raise DebianError(problem)
        executable = executable or sys.executable
        source = read_source(layout, Path(executable).parent if executable else None)
        state = ensure_installed(layout, source)
        if state == 'present':
            notice = installed_notice(layout)
            if notice:
                say(notice)
        if not source.dns_servers:
            say('Android reported no DNS servers for the active network; apt cannot reach the '
                'mirrors until a network is connected. Reopen Debian after connecting.')
        launch = prepare_launch(layout, source, term=environ.get('TERM', 'xterm-256color'))
        status, seconds = run(launch)
        if status == 0:
            return 0
        if seconds < QUICK_EXIT_SECONDS:
            message = (f'Debian stopped {seconds:.1f} s after starting (status {status}). '
                       'PRoot\'s messages are shown above.')
            record_error(layout, message, ' '.join(launch.argv) + '\n')
            say(message)
            wait_for_close()
        else:
            say(f'Debian exited with status {status}.')
            wait_for_close()
        return status if status > 0 else 128 - status
    except DebianError as error:
        message = f'Debian could not start: {error}'
        detail = ''
    except Exception as error:  # noqa: BLE001 - a visible report beats a vanished tab
        message = f'Debian could not start: unexpected {type(error).__name__}: {error}'
        detail = traceback.format_exc()
    print(message, file=sys.stderr, flush=True)
    if layout is not None:
        record_error(layout, message, detail)
        print(f'Details were saved in {layout.last_error}.', file=sys.stderr, flush=True)
    wait_for_close()
    return 1


if __name__ == '__main__':
    sys.exit(main())
