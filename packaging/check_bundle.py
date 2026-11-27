"""Check a built bundle: it must not contain the ``triangle`` package, in any form, and it must
contain coppermesh, the rust mesher.

    python packaging/check_bundle.py BUNDLE_DIR [--list FILE]

``BUNDLE_DIR`` is ``dist/Coppernicus.app`` or ``dist/Coppernicus``. Looked at: every file on
disk (a folder or file named ``triangle``, ``triangle-*.dist-info``, an extension
``triangle/core*``), and every module in the PYZ archives inside the executables (pure-Python
modules live there, not on disk), matched by their exact top-level name, so ``matplotlib.tri``
is not mistaken for it. coppermesh must be there twice: its extension on disk
(``coppermesh/coppermesh*.so`` or ``.pyd``; on Windows with ``python3.dll``, which an abi3
extension loads) and its package in a PYZ archive; and the bundle's THIRD_PARTY_NOTICES.txt must
hold licenses/coppermesh-crates.txt whole, with every Rust crate it lists (the licences of the
crates compiled into coppermesh, from its Cargo.lock), and the licence of the app's icons
(Phosphor Icons, MIT) whole. Prints the counts and the size; ``--list``
writes the full listing (files, then modules). Exit code 1 if triangle is found or coppermesh is
missing. Needs PyInstaller (its archive readers): run it with the build environment's Python.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

NAME = "triangle"
RUST = "coppermesh"
NOTICES = "THIRD_PARTY_NOTICES.txt"
RUST_CRATES = Path(__file__).resolve().parent / "licenses" / "coppermesh-crates.txt"
PHOSPHOR = (Path(__file__).resolve().parents[1] / "src" / "coppernicus" / "app" / "resources" / "icons"
            / "LICENSE-phosphor.txt")


def icon_notice_problems(notices_text, licence_text):
    """Why the bundled notices do not cover the app's icons (Phosphor Icons, MIT; empty: they
    do): ``licence_text`` (the committed LICENSE-phosphor.txt) must be in them whole."""
    if notices_text is None:
        return [f"no {NOTICES} in the bundle"]
    if licence_text.strip() not in notices_text.replace("\r\n", "\n"):
        return [f"{NOTICES} lacks the licence of the app's icons (Phosphor Icons, MIT)"]
    return []


def _is_triangle_path(rel: Path) -> bool:
    for part in rel.parts:
        low = part.lower()
        if low == NAME or low.startswith(f"{NAME}-") and low.endswith((".dist-info", ".egg-info")):
            return True
    return False


def _is_triangle_module(name: str) -> bool:
    return name.split(".")[0].lower() == NAME


def rust_extensions(files):
    """The coppermesh extension files among ``files`` (paths relative to the bundle)."""
    return [f for f in files if f.parent.name == RUST and f.name.startswith(f"{RUST}.")
            and f.suffix.lower() in (".so", ".pyd")]


def rust_problems(files, modules):
    """Why the bundle's coppermesh would not load, as messages (empty: it is there)."""
    out = []
    extensions = rust_extensions(files)
    if not extensions:
        out.append(f"no {RUST} extension ({RUST}/{RUST}*.so or .pyd) in the bundle")
    if RUST not in {m for _e, m in modules}:
        out.append(f"no {RUST} package in the PYZ archives")
    if any(f.suffix.lower() == ".pyd" for f in extensions) and not any(
            f.name.lower() == "python3.dll" for f in files):
        out.append(f"python3.dll, which the abi3 {RUST} extension loads, is not in the bundle")
    return out


def crate_notice_problems(notices_text, crates_text):
    """Why the bundled notices do not cover the Rust crates in coppermesh (empty: they do).
    ``notices_text`` is the bundle's THIRD_PARTY_NOTICES.txt (None: not found), ``crates_text``
    the committed licenses/coppermesh-crates.txt."""
    if notices_text is None:
        return [f"no {NOTICES} in the bundle"]
    notices_text = notices_text.replace("\r\n", "\n")
    out = []
    for line in crates_text.splitlines():
        if line.strip().startswith("Rust crate: "):
            marker = " ".join(line.strip().split()[:4])          # "Rust crate: NAME VERSION"
            if marker not in notices_text:
                out.append(f"{NOTICES} lacks the licences of {marker}")
    if not out and crates_text.rstrip() not in notices_text:
        out.append(f"{NOTICES} does not hold licenses/{RUST_CRATES.name} whole (a licence text differs)")
    return out


def _executables(bundle: Path):
    for path in bundle.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in ("", ".exe"):
            try:
                with open(path, "rb") as f:
                    head = f.read(4)
            except OSError:
                continue
            # Mach-O (64-bit, either byte order, or fat) or a Windows PE file.
            if head[:2] == b"MZ" or head in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
                yield path


def pyz_modules(exe: Path):
    """The module names in the PYZ archive the PyInstaller executable ``exe`` carries (empty
    for a binary that is not a PyInstaller executable)."""
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    try:
        package = CArchiveReader(str(exe))
    except Exception:                        # noqa: BLE001 - not a PyInstaller executable
        return []
    names = []
    for entry, (_offset, _length, _ulength, _compress, typecode) in package.toc.items():
        if typecode != "z":                  # 'z': the PYZ archive
            continue
        data = package.extract(entry)
        tmp = exe.parent / f".{exe.name}.{entry}.pyz-check"
        try:
            tmp.write_bytes(data)
            names += list(ZlibArchiveReader(str(tmp)).toc)
        finally:
            tmp.unlink(missing_ok=True)
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--list", type=Path, help="write the full listing to this file")
    args = ap.parse_args(argv)
    bundle = args.bundle.resolve()
    files = sorted(p.relative_to(bundle) for p in bundle.rglob("*") if p.is_file() or p.is_symlink())
    size = sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file() and not p.is_symlink())
    modules = []
    for exe in _executables(bundle):
        modules += [(exe.relative_to(bundle), m) for m in pyz_modules(exe)]
    bad_files = [f for f in files if _is_triangle_path(f)]
    bad_modules = [(e, m) for e, m in modules if _is_triangle_module(m)]
    if args.list:
        args.list.write_text("\n".join([*(f.as_posix() for f in files), "",
                                        *(f"{e.as_posix()}: {m}" for e, m in modules)]) + "\n",
                             encoding="utf-8")
    print(f"{bundle.name}: {len(files)} files, {size / 1e6:.1f} MB, "
          f"{len(modules)} modules in PYZ archives")
    if not modules:
        print("error: no PYZ archive found: is this a PyInstaller bundle?", file=sys.stderr)
        return 1
    missing = rust_problems(files, modules)
    found = [bundle / f for f in files if f.name == NOTICES and (bundle / f).is_file()]
    notices_text = found[0].read_text(encoding="utf-8", errors="replace") if found else None
    if rust_extensions(files):
        missing += crate_notice_problems(notices_text, RUST_CRATES.read_text(encoding="utf-8"))
    missing += icon_notice_problems(notices_text, PHOSPHOR.read_text(encoding="utf-8"))
    if bad_files or bad_modules or missing:
        for f in bad_files:
            print(f"error: triangle in the bundle: {f.as_posix()}", file=sys.stderr)
        for e, m in bad_modules:
            print(f"error: triangle module in {e.as_posix()}: {m}", file=sys.stderr)
        for message in missing:
            print(f"error: {message}", file=sys.stderr)
        return 1
    print(f"OK: no '{NAME}' file, folder, distribution or module in the bundle")
    print(f"OK: {RUST} is in the bundle: {', '.join(f.as_posix() for f in rust_extensions(files))}, "
          f"with the licences of the Rust crates it is built from in {NOTICES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
