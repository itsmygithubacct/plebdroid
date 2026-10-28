#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""A native build's manifest must cover the exact bytes entering the APK."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from prepare_desktop import verify_native_output


class NativeOutputTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='plebdroid-package-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.artifact = self.root / 'python-site/PIL/example.so'
        self.artifact.parent.mkdir(parents=True)
        self.artifact.write_bytes(b'exact native build output')
        self.manifest = {'format': 1, 'checks_passed': True, 'target': {'abi': 'arm64-v8a'},
            'artifacts': [{'path': self.artifact.relative_to(self.root).as_posix(),
                'bytes': self.artifact.stat().st_size,
                'sha256': hashlib.sha256(self.artifact.read_bytes()).hexdigest()}]}
        self.save()

    def save(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_exact_build(self):
        verify_native_output(self.root)

    def test_changed_and_unrecorded_binary(self):
        self.artifact.write_bytes(b'different bytes')
        with self.assertRaisesRegex(ValueError, 'changed after'):
            verify_native_output(self.root)
        self.artifact.write_bytes(b'exact native build output')
        (self.root / 'stale.so').write_bytes(b'stale')
        with self.assertRaisesRegex(ValueError, 'unrecorded'):
            verify_native_output(self.root)

    def test_failure_wrong_abi_and_missing_artifact(self):
        self.manifest['checks_passed'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, 'did not pass'):
            verify_native_output(self.root)
        self.manifest['checks_passed'] = True
        self.manifest['target']['abi'] = 'x86_64'
        self.save()
        with self.assertRaisesRegex(ValueError, 'ABI'):
            verify_native_output(self.root)
        self.manifest['target']['abi'] = 'arm64-v8a'
        self.save()
        self.artifact.unlink()
        with self.assertRaisesRegex(ValueError, 'regular file'):
            verify_native_output(self.root)

    def test_symlink_replacement(self):
        saved = self.root / 'saved.so'
        self.artifact.rename(saved)
        self.artifact.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            verify_native_output(self.root)


if __name__ == '__main__':
    unittest.main()
