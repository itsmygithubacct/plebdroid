# Third-party notices

Plebdroid source is GPL-3.0-only. It builds on the separately owned Kilix Android
runtime and engine and packages the existing Kilix 95 desktop. Their source
revisions are recorded by the build; development source pins are local review
candidates until published by their owners.

The runtime retains the Kilix Android notice set for CPython, its native
libraries, the terminal engine, touch adapter, PTY broker, KMX client, Lights,
and the game kit. Additional packaged components retain their license texts:

- Kilix 95, Kilix host SDK, Kilix state and its Python bindings.
- Kilix content, its vendored Kilix license package, and the Kitty frame presenter Python package.
- Pillow and the native image-codec libraries selected by its pinned build recipe.
- Unmodified DejaVu Sans 2.37 regular and bold fonts, with their Bitstream Vera notice.
- PRoot (GPL-2.0-or-later) and its statically linked talloc (LGPL-3.0-or-later),
  with source pins, build recipe, patch and license texts under `native/debian/`
  and `notices/debian/`.
- The pinned Debian userspace. Each installed package's copyright file is
  retained under `/usr/share/doc` in the rootfs. Its snapshot and package
  versions are recorded in `native/debian/pins.json`.

The launcher uses the unchanged Plebian-OS angular-P mark, under GPL-2.0-or-later.
Its editable SVG and complete original attribution are retained in `assets/branding/`
and `notices/artwork/`.

All texts are included in `runtime.zip` under `notices/`. Native Python app
build manifests record downloaded source hashes and produced library hashes.
Build inputs and generated binaries are not replacements for the corresponding
source repositories and their license obligations.

Public distribution of the Debian image requires the corresponding source
packages for its exact installed versions to be supplied as required by their
licenses. A build against a Debian mirror is not itself a source offer.
See `notices/debian/README.md` for the source-archive requirement.
