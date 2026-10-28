#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Add the existing desktop and its pinned Python dependencies to the runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tracked_files(root):
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
    for name in sorted(filter(None, names)):
        path = root / name
        if path.is_file() and not path.is_symlink():
            yield name, path


def verify_native_output(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') != 1 or manifest.get('checks_passed') is not True:
        raise ValueError('The native Python application build did not pass its checks')
    if manifest.get('target', {}).get('abi') != 'arm64-v8a':
        raise ValueError('The native Python application ABI is not arm64-v8a')
    if manifest.get('drift'):
        raise ValueError('Native output differs from its reviewed pins')
    declared = set()
    for item in manifest['artifacts']:
        name = item['path']
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or name in declared:
            raise ValueError('Invalid native artifact path')
        declared.add(name)
        artifact = root / path
        if artifact.is_symlink() or not artifact.is_file() or not artifact.resolve().is_relative_to(root.resolve()):
            raise ValueError('Native artifact is not a regular file: ' + name)
        if artifact.stat().st_size != item['bytes'] or sha256(artifact) != item['sha256']:
            raise ValueError('Native artifact changed after its build: ' + name)
    actual = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()}
    if actual != declared | {'manifest.json'}:
        raise ValueError('Native output has missing or unrecorded files')
    return manifest


def build(args):
    output = args.output
    assets = output / 'assets'
    assets.mkdir(parents=True, exist_ok=True)
    natives = output / 'jniLibs/arm64-v8a'
    natives.mkdir(parents=True, exist_ok=True)
    python_site = args.python_apps / 'python-site'
    if not (python_site / 'PIL/__init__.py').is_file():
        raise ValueError('The Android Pillow build is missing')
    native_input = args.python_apps / 'jniLibs/arm64-v8a'
    for required in ('libplebdroid_python.so', 'libkilix-state.so'):
        if not (native_input / required).is_file():
            raise ValueError(f'Missing Android Python application helper: {required}')
    verify_native_output(args.python_apps)
    debian_manifest = verify_native_output(args.debian_runtime)
    expected_debian = {'jniLibs/arm64-v8a/libproot.so', 'jniLibs/arm64-v8a/libproot_loader.so',
                       'assets/debian/rootfs.tar.xz'}
    if {item['path'] for item in debian_manifest['artifacts']} != expected_debian:
        raise ValueError('The Debian build has unexpected artifacts')
    for library in ('libproot.so', 'libproot_loader.so'):
        shutil.copy2(args.debian_runtime / 'jniLibs/arm64-v8a' / library, natives / library)
    (assets / 'debian').mkdir(exist_ok=True)
    shutil.copy2(args.debian_runtime / 'assets/debian/rootfs.tar.xz', assets / 'debian/rootfs.tar.xz')
    # Only these additional helpers belong to this layer. The shared runtime
    # already owns Python and its OpenSSL/SQLite libraries.
    for path in sorted(native_input.glob('*.so')):
        if path.name in {'libpython3.14.so', 'libcrypto_python.so', 'libssl_python.so', 'libsqlite3_python.so'}:
            peer = args.python / 'lib' / path.name
            if not peer.is_file() or sha256(peer) != sha256(path):
                raise ValueError(f'Python library differs from the shared runtime: {path.name}')
        else:
            shutil.copy2(path, natives / path.name)
    payload = {}
    with zipfile.ZipFile(args.base_runtime) as base:
        for item in base.infolist():
            if item.filename in payload:
                raise ValueError('Duplicate base-runtime archive member')
            payload[item.filename] = base.read(item)
    catalog = json.loads(payload['apps/catalog.json'])
    catalog['kilix-95'] = {
        'title': 'Desktop', 'executable': 'libplebdroid_python.so',
        'arguments': ['${RUNTIME}/apps/kilix-95/main.py'], 'single_instance': True,
        'environment': {
            'PLEBDROID_RUNTIME_ROOT': '${RUNTIME}/python',
            'KILIX_ANDROID_MODE': '1',
            'KILIX95_PLATFORM': 'android',
            'KILIX95_FONT_DIR': '${RUNTIME}/apps/kilix-95/assets/fonts',
            'TERM': 'xterm-kitty',
            'KILIX_HOME': '${RUNTIME}/kilix',
            'KILIX95_DIR': '${RUNTIME}/apps/kilix-95',
            'KILIX95_DOCUMENTS_HOME': '${HOME}/Documents',
            'HOME': '${HOME}',
            'KILIX_STATE_LIBRARY': '${NATIVE}/libkilix-state.so',
            'KILIX_ANDROID_REQUEST_DIR': '${FILES}/activity-requests',
            'PYTHONPATH': '${RUNTIME}/kilix/config',
        },
    }
    catalog['debian'] = {
        'title': 'Debian', 'executable': 'libplebdroid_python.so',
        'arguments': ['${RUNTIME}/apps/debian/main.py'], 'single_instance': False,
        'environment': {
            'PLEBDROID_RUNTIME_ROOT': '${RUNTIME}/python',
            'PLEBDROID_FILES': '${FILES}', 'HOME': '${HOME}', 'TERM': 'xterm-kitty',
        },
    }
    payload['apps/catalog.json'] = json.dumps(catalog, sort_keys=True).encode()
    payload['apps/startup.json'] = b'{"program":"kilix-95"}\n'
    for name, path in tracked_files(args.desktop):
        if name.endswith('.py') and not name.startswith(('tests/', 'tools/')) or name.startswith('assets/'):
            payload['apps/kilix-95/' + name] = path.read_bytes()
    for name, path in tracked_files(args.host):
        if name.startswith('config/') and (name.endswith(('.py', '.conf', '.json', '.ttf', '.otf'))):
            payload['kilix/' + name] = path.read_bytes()
    for root, prefix, source_prefix in (
        (args.state, 'kilix/third_party/kilix-state/python/src/', 'python/src/'),
        (args.content, 'kilix/third_party/kilix-content/src/', 'src/'),
        (args.presenter, 'kilix/third_party/kitty-frame-presenter/src/', 'src/'),
    ):
        for name, path in tracked_files(root):
            if name.startswith(source_prefix):
                payload[prefix + name[len(source_prefix):]] = path.read_bytes()
    # Content imports its vendored license package even when Android only uses
    # the catalog. Keep that package's data and notices in its original layout.
    for name, path in tracked_files(args.content):
        if (name.startswith(('catalog/', 'third_party/kilix-license/src/'))
                or name in {'third_party/kilix-license/LICENSE', 'third_party/kilix-license/NOTICE',
                            'third_party/kilix-license/VERSION', 'third_party/kilix-license.pin'}):
            payload['kilix/third_party/kilix-content/' + name] = path.read_bytes()
    for path in sorted(python_site.rglob('*')):
        if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts:
            payload['python-site/' + path.relative_to(python_site).as_posix()] = path.read_bytes()
    for name, root in (('kilix-95', args.desktop), ('kilix-host', args.host),
                       ('kilix-state', args.state), ('kilix-content', args.content),
                       ('kitty-frame-presenter', args.presenter)):
        payload[f'notices/{name}.txt'] = (root / 'LICENSE').read_bytes()
    for directory in ('notices', 'licenses'):
        for path in sorted((args.python_apps / directory).rglob('*')):
            if path.is_file() and not path.is_symlink():
                payload['notices/python-apps/' + path.relative_to(args.python_apps).as_posix()] = path.read_bytes()
    project = Path(__file__).resolve().parent.parent
    payload['apps/debian/main.py'] = (project / 'runtime/debian/main.py').read_bytes()
    payload['plebdroid/debian-build.json'] = json.dumps(debian_manifest, sort_keys=True).encode()
    for path in sorted((project / 'assets/fonts').glob('*')):
        if path.is_file():
            payload['apps/kilix-95/assets/fonts/' + path.name] = path.read_bytes()
    for path in sorted((project / 'notices').rglob('*')):
        if path.is_file():
            payload['notices/plebdroid/' + path.relative_to(project / 'notices').as_posix()] = path.read_bytes()
    payload['notices/PLEBDROID_LICENSE.txt'] = (project / 'LICENSE').read_bytes()
    payload['notices/PLEBDROID_THIRD_PARTY.md'] = (project / 'THIRD_PARTY_NOTICES.md').read_bytes()
    components = {name: {
        'revision': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
        'dirty': bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain=v1'], text=True).strip()),
        }
        for name, root in (('desktop', args.desktop), ('host-sdk', args.host), ('state', args.state),
                           ('content', args.content), ('frame-presenter', args.presenter))}
    payload['plebdroid/components.json'] = json.dumps(components, sort_keys=True).encode()
    staging = assets / 'runtime.new.zip'
    with zipfile.ZipFile(staging, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(payload.items()):
            if name.startswith('/') or '..' in Path(name).parts:
                raise ValueError('Invalid runtime member path')
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compresslevel=6)
    staging.replace(assets / 'runtime.zip')
    (assets / 'runtime.sha256').write_text(sha256(assets / 'runtime.zip'))
    (output / 'manifest.json').write_text(json.dumps({
        'components': components, 'runtime_sha256': sha256(assets / 'runtime.zip'),
        'python_apps_manifest_sha256': sha256(args.python_apps / 'manifest.json'),
        'debian_manifest_sha256': sha256(args.debian_runtime / 'manifest.json'),
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payload.items())},
    }, indent=2) + '\n')
    print(assets / 'runtime.zip')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base-runtime', 'desktop', 'host', 'state', 'content', 'presenter', 'python-apps', 'debian-runtime', 'python', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    build(parser.parse_args())


if __name__ == '__main__':
    main()
