#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the Debian terminal launcher (runtime/debian/main.py).

Always run: extraction safety against hostile archives, the install state
machine, launch arguments, DNS and error reporting.

Optional, from environment variables:
  PLEBDROID_DEBIAN_ROOTFS   the pinned rootfs .tar.xz; installs the real archive
  PLEBDROID_TEST_HOST_PROOT and PLEBDROID_TEST_HOST_LOADER
                            an x86-64 build of the pinned PRoot and its loader;
                            with qemu-aarch64 they run the installed rootfs on
                            this host (not Android evidence)
  PLEBDROID_TEST_NETWORK=1  also run apt-get update/install over the network
  PLEBDROID_DEBIAN_RUNTIME_OUTPUT
                            an output directory of tools/build_debian_runtime.py
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import io
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
import tempfile
import time
import unittest
from unittest import mock
import zipfile

PROJECT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('plebdroid_debian', PROJECT / 'runtime/debian/main.py')
debian = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = debian  # dataclasses resolve annotations through it
SPEC.loader.exec_module(debian)

BUILDER_SPEC = importlib.util.spec_from_file_location('plebdroid_debian_builder',
                                                      PROJECT / 'tools/build_debian_runtime.py')
builder = importlib.util.module_from_spec(BUILDER_SPEC)
sys.modules[BUILDER_SPEC.name] = builder
BUILDER_SPEC.loader.exec_module(builder)

ROOTFS = os.environ.get('PLEBDROID_DEBIAN_ROOTFS')
RUNTIME_OUTPUT = os.environ.get('PLEBDROID_DEBIAN_RUNTIME_OUTPUT')
HOST_PROOT = os.environ.get('PLEBDROID_TEST_HOST_PROOT')
HOST_LOADER = os.environ.get('PLEBDROID_TEST_HOST_LOADER')
QEMU = shutil.which('qemu-aarch64')


# --- fixtures --------------------------------------------------------------

def entry(name, kind='file', data=b'', mode=0o644, target='', size=None):
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.mtime = 1_700_000_000
    info.uid = info.gid = 0
    if kind == 'file':
        info.size = len(data) if size is None else size
    elif kind == 'dir':
        info.type = tarfile.DIRTYPE
    elif kind == 'symlink':
        info.type, info.linkname = tarfile.SYMTYPE, target
    elif kind == 'hardlink':
        info.type, info.linkname = tarfile.LNKTYPE, target
    elif kind == 'char':
        info.type, info.devmajor, info.devminor = tarfile.CHRTYPE, 1, 3
    elif kind == 'fifo':
        info.type = tarfile.FIFOTYPE
    return info, data


def make_tar(entries) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.GNU_FORMAT) as archive:
        for info, data in entries:
            archive.addfile(info, io.BytesIO(data) if info.isreg() else None)
    return buffer.getvalue()


MINIMAL_ROOTFS = [
    entry('./', 'dir', mode=0o755), entry('./etc/', 'dir', mode=0o755),
    entry('./etc/hostname', data=b'plebdroid\n'), entry('./root/', 'dir', mode=0o700),
    entry('./usr/', 'dir'), entry('./usr/bin/', 'dir', mode=0o755),
    entry('./usr/bin/tool', data=b'#!/bin/sh\necho tool\n', mode=0o4755),
    entry('./usr/bin/tool-link', 'hardlink', target='./usr/bin/tool'),
    entry('./bin', 'symlink', target='usr/bin'),
    entry('./dev/', 'dir'), entry('./dev/null', 'char', mode=0o666),
]


def extract_bytes(data: bytes, destination: Path, limits=None):
    return debian.extract_stream(io.BytesIO(data), destination, limits or debian.Limits())


@contextlib.contextmanager
def pinned_to(tar: bytes):
    """Point the launcher's pins at a fixture archive for state-machine tests."""
    xz = lzma.compress(tar, preset=1)
    values = {'ROOTFS_XZ_SHA256': hashlib.sha256(xz).hexdigest(), 'ROOTFS_XZ_BYTES': len(xz),
              'ROOTFS_TAR_SHA256': hashlib.sha256(tar).hexdigest(), 'ROOTFS_TAR_BYTES': len(tar),
              'ROOTFS_FILE_BYTES': len(tar), 'ROOTFS_MEMBERS': 16}
    with mock.patch.multiple(debian, **values):
        yield xz


def write_apk(path: Path, asset: bytes | Path | None) -> Path:
    with zipfile.ZipFile(path, 'w') as bundle:
        bundle.writestr('AndroidManifest.xml', b'fixture')
        if isinstance(asset, Path):
            bundle.write(asset, debian.ASSET_MEMBER, compress_type=zipfile.ZIP_STORED)
        elif asset is not None:
            bundle.writestr(debian.ASSET_MEMBER, asset, compress_type=zipfile.ZIP_STORED)
    return path


class Sandbox(unittest.TestCase):
    """An app-like directory: files/, files/home, a native dir and an APK."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='plebdroid-debian-')
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.files = self.base / 'files'
        self.files.mkdir()
        self.layout = debian.Layout(self.files, self.files / 'home')
        self.native = self.base / 'lib/arm64'
        self.native.mkdir(parents=True)
        for name in (debian.PROOT, debian.LOADER, 'libplebdroid_python.so'):
            path = self.native / name
            path.write_bytes(b'\x7fELF fixture')
            path.chmod(0o755)
        self.apk = self.base / 'base.apk'
        self.output = io.StringIO()

    def source(self, dns=('192.0.2.53',)):
        return debian.Source(self.apk, self.native, tuple(dns))

    def write_source(self, **overrides):
        self.layout.debian.mkdir(exist_ok=True)
        payload = {'format': 1, 'apk': str(self.apk), 'native_dir': str(self.native),
                   'dns_servers': ['192.0.2.53', '2001:db8::53']} | overrides
        self.layout.source.write_text(json.dumps(payload))

    def install(self, **kwargs):
        return debian.ensure_installed(self.layout, self.source(), out=self.output, **kwargs)

    def assert_clean_failure(self):
        self.assertFalse(self.layout.rootfs.exists())
        self.assertFalse(self.layout.record.exists())
        self.assertEqual(debian.stale_staging(self.layout), [])


# --- tests -----------------------------------------------------------------

class PinTest(unittest.TestCase):
    def test_runtime_constants_match_the_pins(self):
        pins = json.loads((PROJECT / 'native/debian/pins.json').read_text())['rootfs']
        self.assertEqual(debian.ROOTFS_SNAPSHOT, pins['snapshot'])
        self.assertEqual(debian.ROOTFS_XZ_SHA256, pins['xz_sha256'])
        self.assertEqual(debian.ROOTFS_XZ_BYTES, pins['xz_bytes'])
        self.assertEqual(debian.ROOTFS_TAR_SHA256, pins['tar_sha256'])
        self.assertEqual(debian.ROOTFS_TAR_BYTES, pins['tar_bytes'])
        self.assertEqual(debian.ROOTFS_FILE_BYTES, pins['regular_file_bytes'])
        self.assertEqual(debian.ROOTFS_MEMBERS, pins['members'])
        self.assertEqual(debian.ASSET_MEMBER, 'assets/' + pins['asset'])

    def test_patch_and_notice_files_exist(self):
        pins = json.loads((PROJECT / 'native/debian/pins.json').read_text())
        for patch in pins['proot']['patches']:
            self.assertTrue((PROJECT / 'native/debian' / patch).is_file())
        for name in ('proot', 'talloc'):
            self.assertTrue((PROJECT / pins[name]['notice']).is_file())


class BuilderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='plebdroid-builder-')
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def test_output_directory_is_only_replaced_when_owned(self):
        output = self.base / 'out'
        output.mkdir()
        (output / 'unrelated').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'not produced by this tool'):
            builder.prepare_output(output)
        (output / 'unrelated').unlink()
        (output / 'manifest.json').write_text(json.dumps({'generator': builder.GENERATOR}))
        staging = builder.prepare_output(output)
        self.assertEqual(staging, self.base / 'out.new')
        self.assertEqual(list(staging.iterdir()), [])

    def test_rootfs_must_match_the_pins(self):
        pins = builder.load_pins()
        fake = self.base / 'rootfs.tar.xz'
        fake.write_bytes(lzma.compress(b'not the rootfs'))
        with self.assertRaisesRegex(ValueError, 'not the pinned rootfs'):
            builder.verify_rootfs(fake, pins)
        fake.unlink()
        fake.symlink_to(self.base)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            builder.verify_rootfs(fake, pins)

    def test_elf_checks_reject_host_binaries(self):
        with self.assertRaisesRegex(ValueError, 'not AArch64'):
            builder.check_elf(Path(sys.executable).resolve(), 'proot', 16384)
        text = self.base / 'text'
        text.write_text('#!/bin/sh\n')
        with self.assertRaisesRegex(ValueError, 'not a little-endian ELF64'):
            builder.check_elf(text, 'loader', 16384)

    @unittest.skipUnless(RUNTIME_OUTPUT, 'set PLEBDROID_DEBIAN_RUNTIME_OUTPUT to a build output')
    def test_build_output(self):
        output = Path(RUNTIME_OUTPUT)
        manifest = json.loads((output / 'manifest.json').read_text())
        self.assertEqual(manifest['drift'], [])
        self.assertEqual(sorted(a['path'] for a in manifest['artifacts']), sorted(builder.OUTPUT_FILES))
        actual = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
        self.assertEqual(actual, set(builder.OUTPUT_FILES) | {'manifest.json'})
        for artifact in manifest['artifacts']:
            path = output / artifact['path']
            self.assertEqual(builder.sha256_file(path), artifact['sha256'])
        natives = output / 'jniLibs/arm64-v8a'
        builder.check_elf(natives / 'libproot.so', 'proot', 16384)
        builder.check_elf(natives / 'libproot_loader.so', 'loader', 16384)
        rootfs = output / 'assets/debian/rootfs.tar.xz'
        self.assertEqual(builder.sha256_file(rootfs), debian.ROOTFS_XZ_SHA256)
        sys.path.insert(0, str(PROJECT / 'tools'))
        try:
            from prepare_desktop import verify_native_output
        finally:
            sys.path.pop(0)
        verify_native_output(output)


class ExtractionSafetyTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='plebdroid-extract-')
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        (self.outside / 'victim').write_text('original')
        self.root = self.base / 'root'

    def assert_rejected(self, entries, pattern, limits=None):
        shutil.rmtree(self.root, ignore_errors=True)
        with self.assertRaisesRegex(debian.DebianError, pattern):
            extract_bytes(make_tar(entries), self.root, limits)
        self.assertEqual((self.outside / 'victim').read_text(), 'original')
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ['victim'])

    def test_parent_traversal_and_absolute_paths(self):
        self.assert_rejected([entry('../outside/victim', data=b'x')], 'leaves the rootfs')
        self.assert_rejected([entry('a/../../outside/victim', data=b'x')], 'leaves the rootfs')
        self.assert_rejected([entry(str(self.outside / 'victim'), data=b'x')], 'Absolute')

    def test_writes_never_follow_an_archive_symlink(self):
        for target in (str(self.outside), '../outside', '/'):
            with self.subTest(target=target):
                self.assert_rejected([entry('escape', 'symlink', target=target),
                                      entry('escape/victim', data=b'overwritten')],
                                     'passes through a non-directory')

    def test_directory_entry_over_a_symlink(self):
        self.assert_rejected([entry('d', 'symlink', target=str(self.outside)), entry('d/', 'dir')],
                             'non-directory')

    def test_hard_links_only_copy_earlier_regular_files(self):
        self.assert_rejected([entry('link', 'symlink', target=str(self.outside / 'victim')),
                              entry('copy', 'hardlink', target='link')], 'earlier regular file')
        self.assert_rejected([entry('copy', 'hardlink', target='later'),
                              entry('later', data=b'x')], 'earlier regular file')
        self.assert_rejected([entry('copy', 'hardlink', target='../outside/victim')], 'leaves the rootfs')
        self.assert_rejected([entry('d', 'symlink', target=str(self.outside)),
                              entry('copy', 'hardlink', target='d/victim')], 'earlier regular file')

    def test_duplicates_and_replacing_the_root(self):
        self.assert_rejected([entry('f', data=b'1'), entry('f', data=b'2')], 'more than once')
        self.assert_rejected([entry('f', data=b'1'), entry('f', 'symlink', target='/etc')], 'more than once')
        self.assert_rejected([entry('./', 'symlink', target='/')], 'replaces the rootfs')

    def test_limits(self):
        self.assert_rejected([entry(f'f{i}', data=b'') for i in range(5)], 'too many',
                             debian.Limits(members=4))
        self.assert_rejected([entry('big', data=b'x' * 100)], 'too large', debian.Limits(file_bytes=99))
        self.assert_rejected([entry('a', data=b'x' * 60), entry('b', 'hardlink', target='a')],
                             'size limit', debian.Limits(total_bytes=100))
        self.assert_rejected([entry('/'.join('d' * 10), data=b'')], 'too deep', debian.Limits(depth=5))

    def test_truncated_archive(self):
        data = make_tar([entry('f', data=b'x' * 5000)])[:3000]
        with self.assertRaisesRegex(debian.DebianError, 'truncated|damaged|ended inside'):
            extract_bytes(data, self.root)

    def test_short_writes_are_completed(self):
        real_write = os.write
        calls = []

        def short_write(fd, data):
            calls.append(len(data))
            return real_write(fd, bytes(data[:7]))  # store at most 7 bytes per call

        payload = bytes(range(256)) * 40
        with mock.patch.object(debian.os, 'write', short_write):
            extract_bytes(make_tar([entry('f', data=payload), entry('g', 'hardlink', target='f')]), self.root)
            debian.atomic_write(self.base / 'record.json', payload)
        self.assertEqual((self.root / 'f').read_bytes(), payload)
        self.assertEqual((self.root / 'g').read_bytes(), payload)
        self.assertEqual((self.base / 'record.json').read_bytes(), payload)
        self.assertGreater(len(calls), 3 * len(payload) // 7)

    def test_a_write_that_stores_nothing_fails(self):
        with mock.patch.object(debian.os, 'write', return_value=0):
            with self.assertRaises(OSError):
                extract_bytes(make_tar([entry('f', data=b'data')]), self.root)

    def test_destination_must_be_fresh(self):
        self.root.mkdir()
        with self.assertRaises(FileExistsError):
            extract_bytes(make_tar([entry('f')]), self.root)

    def test_modes_links_and_special_entries(self):
        result = extract_bytes(make_tar(MINIMAL_ROOTFS + [
            entry('./usr/bin/setgid', data=b'g', mode=0o2711),
            entry('./ro-dir/', 'dir', mode=0o555), entry('./ro-dir/f', data=b'r', mode=0o444),
            entry('./fifo', 'fifo'), entry('./abs', 'symlink', target='/etc/hostname'),
        ]), self.root)
        tool = self.root / 'usr/bin/tool'
        copy = self.root / 'usr/bin/tool-link'
        self.assertEqual(stat.S_IMODE(tool.stat().st_mode), 0o755)  # setuid dropped
        self.assertEqual(stat.S_IMODE((self.root / 'usr/bin/setgid').stat().st_mode), 0o711 | 0o600)
        self.assertEqual(stat.S_IMODE((self.root / 'ro-dir').stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.root / 'ro-dir/f').stat().st_mode), 0o644)
        self.assertEqual(copy.read_bytes(), tool.read_bytes())
        self.assertNotEqual(copy.stat().st_ino, tool.stat().st_ino)
        self.assertEqual(copy.stat().st_nlink, 1)
        self.assertEqual(os.readlink(self.root / 'bin'), 'usr/bin')
        self.assertEqual(os.readlink(self.root / 'abs'), '/etc/hostname')  # a guest path, kept
        self.assertFalse((self.root / 'dev/null').exists())
        self.assertFalse((self.root / 'fifo').exists())
        self.assertEqual(result.skipped, {'devices': 1, 'fifos': 1})
        self.assertEqual(result.hardlinks, 1)
        self.assertEqual(tool.stat().st_mtime, 1_700_000_000)


class InstallTest(Sandbox):
    def test_fresh_install(self):
        tar = make_tar(MINIMAL_ROOTFS)
        with pinned_to(tar) as xz:
            write_apk(self.apk, xz)
            self.assertEqual(self.install(), 'installed')
        self.assertEqual((self.layout.rootfs / 'etc/hostname').read_text(), 'plebdroid\n')
        self.assertTrue((self.layout.rootfs / 'root/Documents').is_dir())
        record = json.loads(self.layout.record.read_text())
        self.assertNotIn('staging', record)
        self.assertEqual(record['skipped'], {'devices': 1, 'fifos': 0})
        self.assertIn('100%', self.output.getvalue())
        self.assertEqual(debian.stale_staging(self.layout), [])

    def test_asset_digest_mismatch_leaves_nothing(self):
        tar = make_tar(MINIMAL_ROOTFS)
        with pinned_to(tar) as xz:
            damaged = bytearray(xz)
            damaged[len(damaged) // 2] ^= 0xff
            write_apk(self.apk, bytes(damaged))
            with self.assertRaisesRegex(debian.DebianError, 'pinned SHA-256'):
                self.install()
        self.assert_clean_failure()

    def test_missing_asset_and_unreadable_apk(self):
        with pinned_to(make_tar(MINIMAL_ROOTFS)):
            write_apk(self.apk, None)
            with self.assertRaisesRegex(debian.DebianError, 'does not include'):
                self.install()
            self.apk.write_bytes(b'not a zip')
            with self.assertRaisesRegex(debian.DebianError, 'Cannot read the installed APK'):
                self.install()
        self.assert_clean_failure()

    def test_extracted_tar_digest_mismatch_cleans_staging(self):
        tar = make_tar(MINIMAL_ROOTFS)
        with pinned_to(tar) as xz:
            write_apk(self.apk, xz)
            with mock.patch.object(debian, 'ROOTFS_TAR_SHA256', '0' * 64):
                with self.assertRaisesRegex(debian.DebianError, 'extracted Debian archive'):
                    self.install()
        self.assert_clean_failure()

    def test_hostile_archive_with_matching_pin_is_still_refused(self):
        tar = make_tar([entry('./', 'dir'), entry('../escape', data=b'x')])
        with pinned_to(tar) as xz:
            write_apk(self.apk, xz)
            with self.assertRaisesRegex(debian.DebianError, 'leaves the rootfs'):
                self.install()
        self.assert_clean_failure()
        self.assertFalse((self.files / 'escape').exists())

    def test_interrupted_extraction_is_rolled_back(self):
        tar = make_tar(MINIMAL_ROOTFS)
        original = debian.Extractor.add
        calls = []

        def interrupted(extractor, archive, member):
            calls.append(member.name)
            if len(calls) == 5:
                raise KeyboardInterrupt
            return original(extractor, archive, member)

        with pinned_to(tar) as xz, mock.patch.object(debian.Extractor, 'add', interrupted):
            write_apk(self.apk, xz)
            with self.assertRaises(KeyboardInterrupt):
                self.install()
        self.assert_clean_failure()

    def test_killed_install_leaves_staging_that_the_next_launch_removes(self):
        tar = make_tar(MINIMAL_ROOTFS)
        self.layout.debian.mkdir()
        stale = self.layout.debian / (debian.STAGING_PREFIX + '123-456')
        stale.mkdir()
        (stale / 'partial').write_text('half written')
        with pinned_to(tar) as xz:
            write_apk(self.apk, xz)
            self.assertEqual(self.install(), 'installed')
        self.assertFalse(stale.exists())
        self.assertIn('incomplete installation', self.output.getvalue())

    def test_killed_between_record_and_rename_is_completed(self):
        self.layout.debian.mkdir()
        staged = self.layout.debian / (debian.STAGING_PREFIX + '9-9')
        staged.mkdir()
        (staged / 'complete-marker').write_text('extracted')
        other = self.layout.debian / (debian.STAGING_PREFIX + '8-8')
        other.mkdir()
        debian.write_record(self.layout, staging=staged.name, installed_at=1)
        self.assertEqual(debian.ensure_installed(self.layout, None, out=self.output), 'completed')
        self.assertEqual((self.layout.rootfs / 'complete-marker').read_text(), 'extracted')
        self.assertNotIn('staging', json.loads(self.layout.record.read_text()))
        self.assertFalse(other.exists())

    def test_record_without_staging_or_rootfs_reinstalls(self):
        self.layout.debian.mkdir()
        debian.write_record(self.layout, staging=debian.STAGING_PREFIX + 'gone', installed_at=1)
        tar = make_tar(MINIMAL_ROOTFS)
        with pinned_to(tar) as xz:
            write_apk(self.apk, xz)
            self.assertEqual(self.install(), 'installed')

    def test_existing_changed_rootfs_is_never_replaced(self):
        self.layout.debian.mkdir()
        self.layout.rootfs.mkdir()
        (self.layout.rootfs / 'user-installed').write_text('apt and user changes')
        debian.write_record(self.layout, installed_at=1)
        self.layout.record.write_text(json.dumps({'format': 1, 'snapshot': '20250101T000000Z',
                                                  'xz_sha256': 'f' * 64}))
        tar = make_tar(MINIMAL_ROOTFS)
        with pinned_to(tar) as xz:
            write_apk(self.apk, xz)
            self.assertEqual(self.install(), 'present')
            self.assertEqual(debian.ensure_installed(self.layout, None, out=self.output), 'present')
        self.assertEqual(sorted(p.name for p in self.layout.rootfs.iterdir()), ['user-installed'])
        notice = debian.installed_notice(self.layout)
        self.assertIn('20250101T000000Z', notice)
        self.assertIn('kept', notice)
        self.layout.record.unlink()
        self.assertIn('left as it is', debian.installed_notice(self.layout))
        self.assertTrue((self.layout.rootfs / 'user-installed').exists())

    def test_rootfs_replaced_by_a_link_is_left_alone(self):
        self.layout.debian.mkdir()
        self.layout.rootfs.symlink_to(self.base)
        with self.assertRaisesRegex(debian.DebianError, 'left untouched'):
            self.install()
        self.assertTrue(self.layout.rootfs.is_symlink())

    def test_disk_space_is_checked_before_extraction(self):
        tar = make_tar(MINIMAL_ROOTFS)
        with pinned_to(tar) as xz, mock.patch.object(debian, 'free_bytes', return_value=1 << 20):
            write_apk(self.apk, xz)
            with self.assertRaisesRegex(debian.DebianError, 'free; 1 MiB is available'):
                self.install()
        self.assert_clean_failure()

    def test_install_lock_is_exclusive(self):
        self.layout.debian.mkdir()
        fd = os.open(self.layout.lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaisesRegex(debian.DebianError, 'Another Debian tab'):
            self.install(wait=False)
        fcntl.flock(fd, fcntl.LOCK_UN)
        tar = make_tar(MINIMAL_ROOTFS)
        with pinned_to(tar) as xz:
            write_apk(self.apk, xz)
            self.assertEqual(self.install(wait=False), 'installed')


class LaunchTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.layout.debian.mkdir()
        self.layout.rootfs.mkdir()

    def test_source_json(self):
        self.write_source(dns_servers=['192.0.2.53', '192.0.2.53', 'fe80::1%wlan0', '2001:db8::1'])
        source = debian.read_source(self.layout, self.native)
        self.assertEqual(source.dns_servers, ('192.0.2.53', 'fe80::1%wlan0', '2001:db8::1'))
        self.assertEqual(source.native_dir, self.native)
        for bad, pattern in ((['8.8.8.8; rm -rf /'], 'Invalid DNS'), (['example.com'], 'Invalid DNS'),
                             ([53], 'Invalid DNS'), ('192.0.2.1', 'must be a list')):
            self.write_source(dns_servers=bad)
            with self.assertRaisesRegex(debian.DebianError, pattern):
                debian.read_source(self.layout, self.native)
        self.write_source(native_dir='relative/lib')
        with self.assertRaisesRegex(debian.DebianError, 'absolute'):
            debian.read_source(self.layout, self.native)
        self.write_source(native_dir='/data/app/old-install/lib/arm64')
        with self.assertRaisesRegex(debian.DebianError, 'was updated'):
            debian.read_source(self.layout, self.native)
        self.layout.source.unlink()
        with self.assertRaisesRegex(debian.DebianError, 'did not provide'):
            debian.read_source(self.layout, self.native)

    def test_resolv_conf_uses_only_android_servers(self):
        self.assertEqual(debian.resolv_conf(('192.0.2.53', '2001:db8::53')),
                         "# Written by Plebdroid from Android's active network at each Debian launch.\n"
                         'nameserver 192.0.2.53\nnameserver 2001:db8::53\n')
        self.assertNotIn('nameserver', debian.resolv_conf(()))
        self.assertEqual(debian.resolv_conf(tuple(f'192.0.2.{i}' for i in range(5))).count('nameserver'), 3)

    def test_launch_arguments(self):
        with mock.patch.dict(os.environ, {'LD_LIBRARY_PATH': '/data/app/lib', 'LD_PRELOAD': 'x.so',
                                          'PYTHONPATH': '/runtime'}):
            launch = debian.prepare_launch(self.layout, self.source(), term='xterm-kitty',
                                           readable_check=lambda path: path != '/proc/stat', cpus=2)
        argv = launch.argv
        self.assertEqual(argv[0], str(self.native / debian.PROOT))
        self.assertEqual(launch.executable, argv[0])
        for flag in ('--kill-on-exit', '--link2symlink', '--sysvipc', '-L', '-0',
                     f'--rootfs={self.layout.rootfs}', '--cwd=/root', '--bind=/dev', '--bind=/proc',
                     '--bind=/sys', f'--bind={self.layout.shm}:/dev/shm',
                     f'--bind={self.layout.proc / "stat"}:/proc/stat',
                     f'--bind={self.layout.documents}:/root/Documents',
                     f'--bind={self.layout.resolv}:/etc/resolv.conf'):
            self.assertIn(flag, argv)
        self.assertFalse(any(a.endswith(':/proc/uptime') for a in argv))
        guest = argv[argv.index('/usr/bin/env'):]
        self.assertEqual(guest[:2], ['/usr/bin/env', '-i'])
        self.assertEqual(guest[-2:], ['/bin/bash', '-l'])
        self.assertIn('TERM=xterm-256color', guest)  # no xterm-kitty terminfo in this rootfs
        self.assertFalse([a for a in argv if 'LD_' in a or 'PYTHON' in a])
        self.assertEqual(launch.env, {'PATH': '/system/bin', 'TERM': 'xterm-256color',
                                      'PROOT_LOADER': str(self.native / debian.LOADER),
                                      'PROOT_TMP_DIR': str(self.layout.tmp),
                                      'TMPDIR': str(self.layout.tmp)})
        self.assertIn('nameserver 192.0.2.53', self.layout.resolv.read_text())
        self.assertIn('procs_running', (self.layout.proc / 'stat').read_text())
        self.assertIn('cpu1 ', (self.layout.proc / 'stat').read_text())
        self.assertTrue(self.layout.documents.is_dir())

    def test_launch_refuses_linked_documents_and_missing_helpers(self):
        self.layout.home.mkdir()
        self.layout.documents.symlink_to(self.base)
        with self.assertRaisesRegex(debian.DebianError, 'must be a directory'):
            debian.prepare_launch(self.layout, self.source())
        self.layout.documents.unlink()
        (self.native / debian.LOADER).unlink()
        with self.assertRaisesRegex(debian.DebianError, 'not packaged'):
            debian.prepare_launch(self.layout, self.source())
        (self.native / debian.LOADER).write_bytes(b'x')
        (self.native / debian.LOADER).chmod(0o644)
        with self.assertRaisesRegex(debian.DebianError, 'not an executable'):
            debian.prepare_launch(self.layout, self.source())

    def test_guest_term(self):
        info = self.layout.rootfs / 'usr/share/terminfo/x'
        info.mkdir(parents=True)
        (info / 'xterm-256color').write_bytes(b'')
        self.assertEqual(debian.guest_term(self.layout.rootfs, 'xterm-kitty'), 'xterm-256color')
        (info / 'xterm-kitty').write_bytes(b'')
        self.assertEqual(debian.guest_term(self.layout.rootfs, 'xterm-kitty'), 'xterm-kitty')
        for hostile in ('../../x', '', '.hidden', 'a\nb'):
            self.assertEqual(debian.guest_term(self.layout.rootfs, hostile), 'xterm-256color')
        launch = debian.prepare_launch(self.layout, self.source(), term='xterm-kitty')
        self.assertIn('TERM=xterm-kitty', launch.argv)

    def test_abi(self):
        self.assertIsNone(debian.abi_problem('aarch64', 'arm64-v8a'))
        self.assertIsNone(debian.abi_problem('aarch64', None))
        self.assertIn('ARM translation', debian.abi_problem('aarch64', 'x86_64'))
        self.assertIn('ARM64 kernel', debian.abi_problem('x86_64', None))


class SessionTest(unittest.TestCase):
    def test_child_gets_default_interrupt_handling_and_parent_handlers_return(self):
        before = signal.getsignal(signal.SIGINT)
        launch = debian.Launch(['/bin/sh', '-c', 'kill -INT $$; exit 0'], {'PATH': '/usr/bin:/bin'}, '/bin/sh')
        status, seconds = debian.run_session(launch)
        self.assertEqual(status, -signal.SIGINT)  # an inherited SIG_IGN would have exited 0
        self.assertLess(seconds, 10)
        self.assertIs(signal.getsignal(signal.SIGINT), before)
        status, _ = debian.run_session(debian.Launch(['/bin/sh', '-c', 'exit 7'], {}, '/bin/sh'))
        self.assertEqual(status, 7)


class MainTest(Sandbox):
    def run_main(self, run=None, **kwargs):
        closed = []
        env = {'PLEBDROID_FILES': str(self.files), 'HOME': str(self.layout.home), 'TERM': 'xterm-kitty'}
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = debian.main(env, executable=str(self.native / 'libplebdroid_python.so'),
                                 run=run or (lambda launch: (0, 30.0)),
                                 wait_for_close=lambda: closed.append(True), **kwargs)
        return status, stdout.getvalue() + stderr.getvalue(), closed

    def test_missing_source_is_reported_and_held(self):
        status, text, closed = self.run_main(machine='aarch64', primary_abi='arm64-v8a')
        self.assertEqual(status, 1)
        self.assertIn('did not provide debian/source.json', text)
        self.assertEqual(closed, [True])
        self.assertIn('did not provide', self.layout.last_error.read_text())

    def test_translation_is_reported(self):
        status, text, closed = self.run_main(machine='aarch64', primary_abi='x86_64')
        self.assertEqual(status, 1)
        self.assertIn('ARM translation', text)
        self.assertEqual(closed, [True])

    def test_session_outcomes(self):
        tar = make_tar(MINIMAL_ROOTFS)
        with pinned_to(tar) as xz:
            write_apk(self.apk, xz)
            self.write_source()
            seen = []
            status, text, closed = self.run_main(run=lambda launch: seen.append(launch) or (0, 40.0),
                                                 machine='aarch64', primary_abi='arm64-v8a')
            self.assertEqual((status, closed), (0, []))
            self.assertIn('installed', text)
            self.assertEqual(seen[0].env['TERM'], 'xterm-256color')  # fixture lacks kitty terminfo
            status, text, closed = self.run_main(run=lambda launch: (1, 0.4),
                                                 machine='aarch64', primary_abi='arm64-v8a')
            self.assertEqual((status, closed), (1, [True]))
            self.assertIn('stopped 0.4 s after starting (status 1)', text)
            self.assertIn('libproot.so', self.layout.last_error.read_text())
            status, text, closed = self.run_main(run=lambda launch: (-9, 50.0),
                                                 machine='aarch64', primary_abi='arm64-v8a')
            self.assertEqual((status, closed), (137, [True]))
            self.write_source(dns_servers=[])
            status, text, closed = self.run_main(machine='aarch64', primary_abi='arm64-v8a')
            self.assertIn('no DNS servers', text)

    def test_unexpected_errors_keep_a_traceback(self):
        self.write_source()
        with mock.patch.object(debian, 'ensure_installed', side_effect=RuntimeError('boom')):
            status, text, closed = self.run_main(machine='aarch64', primary_abi='arm64-v8a')
        self.assertEqual((status, closed), (1, [True]))
        self.assertIn('unexpected RuntimeError: boom', text)
        self.assertIn('Traceback', self.layout.last_error.read_text())


@unittest.skipUnless(ROOTFS and Path(ROOTFS).is_file(), 'set PLEBDROID_DEBIAN_ROOTFS to the pinned rootfs')
class RealRootfsTest(unittest.TestCase):
    """Install the actual pinned archive through the launcher's install path."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='plebdroid-real-rootfs-')
        base = Path(cls.directory.name)
        cls.files = base / 'files'
        cls.files.mkdir()
        cls.layout = debian.Layout(cls.files, cls.files / 'home')
        cls.apk = write_apk(base / 'base.apk', Path(ROOTFS))
        cls.output = io.StringIO()
        started = time.monotonic()
        cls.state = debian.ensure_installed(cls.layout, debian.Source(cls.apk, base, ()), out=cls.output)
        cls.seconds = time.monotonic() - started

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_install_result(self):
        self.assertEqual(self.state, 'installed')
        record = json.loads(self.layout.record.read_text())
        self.assertEqual(record['tar_sha256'], debian.ROOTFS_TAR_SHA256)
        self.assertEqual(record['skipped'], {'devices': 8, 'fifos': 0})
        self.assertEqual(record['hardlinks_copied'], 1)
        print(f'\nreal rootfs installed in {self.seconds:.1f} s', file=sys.stderr)

    def test_tree(self):
        root = self.layout.rootfs
        for name, target in (('bin', 'usr/bin'), ('lib', 'usr/lib'), ('sbin', 'usr/sbin'),
                             ('usr/bin/awk', '/etc/alternatives/awk')):
            self.assertEqual(os.readlink(root / name), target)
        perl, copy = root / 'usr/bin/perl', root / 'usr/bin/perl5.40.1'
        self.assertTrue(stat.S_ISREG(os.lstat(copy).st_mode))
        self.assertNotEqual(perl.stat().st_ino, copy.stat().st_ino)
        self.assertEqual(hashlib.sha256(perl.read_bytes()).digest(), hashlib.sha256(copy.read_bytes()).digest())
        dev = {p.name: p for p in (root / 'dev').iterdir()}
        self.assertEqual(sorted(name for name, path in dev.items() if not path.is_symlink()), ['pts', 'shm'])
        self.assertEqual(os.readlink(dev['fd']), '/proc/self/fd')
        self.assertNotIn('null', dev)  # the eight device nodes come from the host /dev
        self.assertEqual((root / 'etc/hostname').read_text(), 'plebdroid\n')
        self.assertTrue((root / 'root/Documents').is_dir())
        counts = {'files': 0, 'links': 0, 'dirs': 0}
        for directory, dirs, files in os.walk(root):
            for name in files + dirs:
                info = os.lstat(os.path.join(directory, name))
                self.assertFalse(info.st_mode & 0o7000, name)
                kind = 'links' if stat.S_ISLNK(info.st_mode) else 'dirs' if stat.S_ISDIR(info.st_mode) else 'files'
                counts[kind] += 1
                if kind != 'links':
                    self.assertTrue(info.st_mode & 0o600 == 0o600, name)
        self.assertEqual(counts['links'], 1251)
        self.assertEqual(counts['files'], 5903)  # 5902 regular files plus the copied hard link

    @unittest.skipUnless(HOST_PROOT and HOST_LOADER and QEMU,
                         'set PLEBDROID_TEST_HOST_PROOT/LOADER and install qemu-user')
    def test_zz_host_proot_session(self):
        """Run the launcher's own arguments with an x86-64 PRoot and qemu (not Android).

        Named to run last: the session installs packages into the shared tree."""
        native = Path(self.directory.name) / 'host-native'
        native.mkdir(exist_ok=True)
        for source, name in ((HOST_PROOT, debian.PROOT), (HOST_LOADER, debian.LOADER)):
            shutil.copy2(source, native / name)
        self.layout.home.mkdir(exist_ok=True)
        self.layout.documents.mkdir(exist_ok=True)
        (self.layout.documents / 'from-android.txt').write_text('made by the Android document picker\n')
        network = os.environ.get('PLEBDROID_TEST_NETWORK') == '1'
        nameservers = [line.split()[1] for line in Path('/etc/resolv.conf').read_text().splitlines()
                       if line.startswith('nameserver')] if network else ['192.0.2.53']
        script = ('set -e; uname -m; hostname; cat /etc/resolv.conf; cat /proc/uptime >/dev/null; '
                  'grep -c procs_running /proc/stat; cat /root/Documents/from-android.txt; '
                  'echo from-debian > /root/Documents/from-debian.txt; dpkg --print-architecture; id -u')
        if network:
            script += '; apt-get update -qq; apt-get install -y -qq --no-install-recommends hello >/dev/null; hello'
        launch = debian.prepare_launch(self.layout, debian.Source(self.apk, native, tuple(nameservers)),
                                       command=['/bin/sh', '-c', script], readable_check=lambda path: False,
                                       extra_proot_args=('-q', QEMU))
        result = subprocess.run(launch.argv, env=launch.env, capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:2], ['aarch64', 'plebdroid'])
        self.assertIn(f'nameserver {nameservers[0]}', lines)
        self.assertIn('made by the Android document picker', lines)
        self.assertEqual(lines[-1], 'Hello, world!' if network else '0')
        self.assertIn('arm64', lines)
        self.assertEqual((self.layout.documents / 'from-debian.txt').read_text(), 'from-debian\n')
        self.assertTrue(stat.S_ISREG(os.lstat(self.layout.documents / 'from-debian.txt').st_mode))


if __name__ == '__main__':
    unittest.main()
