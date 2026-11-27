"""The licence texts of every Rust crate coppermesh is built from, for the packaged app's notices.

    python tools/mesher_rs/crate_licences.py            # write packaging/licenses/coppermesh-crates.txt
    python tools/mesher_rs/crate_licences.py --check    # regenerate and compare; exit 1 if it differs

The crates: those ``cargo metadata --locked`` resolves for coppermesh with the ``python``
feature (the one maturin builds) on the platforms CI and the app build for (TARGETS), following
normal and build dependencies, never dev-dependencies. Each is marked "in the binary" (reached
through normal dependencies, not through a proc-macro) or "build time only" (build scripts,
proc-macros and what only they use: their code is not in the extension, but cargo-about lists
them too, and so do we).

The texts: every licence file each crate ships in its source (LICENSE*, COPYING*, NOTICE*,
COPYRIGHT*, UNLICENSE*), read from cargo's registry, with their copyright lines. A crate under
"MIT OR Apache-2.0" ships both and both are included. Identical texts are printed once, with the
crates that ship them. A crate shipping no licence file stops the generation: its text must then
be added by hand.

The header records the SHA-256 of Cargo.lock and Cargo.toml: tests/test_crate_licences.py fails
when either changes and this file was not regenerated, without needing cargo; ``--check`` (run in
CI, where cargo is) regenerates the whole file and compares. packaging/notices.py appends the file
to THIRD_PARTY_NOTICES.txt, and packaging/check_bundle.py fails a bundle whose notices lack it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

CRATE = Path(__file__).resolve().parent
ROOT = CRATE.parents[1]
OUT = ROOT / "packaging" / "licenses" / "coppermesh-crates.txt"
FEATURES = "python"
TARGETS = ("aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-pc-windows-msvc",
           "x86_64-unknown-linux-gnu")
LICENCE_PREFIXES = ("LICEN", "COPYING", "NOTICE", "COPYRIGHT", "UNLICENSE")
#: The line each crate gets in the list; check_bundle.py looks for it in the bundled notices.
MARKER = "Rust crate: {name} {version}"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def recorded_hashes(text: str) -> dict:
    """{"Cargo.lock": sha, "Cargo.toml": sha} as the header of a generated file records them."""
    out = {}
    for line in text.splitlines():
        if line.startswith("SHA-256 of tools/mesher_rs/"):
            name, _, digest = line[len("SHA-256 of tools/mesher_rs/"):].partition(": ")
            out[name] = digest.strip()
    return out


def listed_crates(text: str) -> list:
    """The (name, version) pairs a generated file lists."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Rust crate: "):
            name, version = line[len("Rust crate: "):].split()[:2]
            out.append((name, version))
    return out


def metadata() -> dict:
    cargo = shutil.which("cargo")
    if not cargo:
        raise SystemExit("error: cargo is not on PATH")
    cmd = [cargo, "metadata", "--format-version", "1", "--locked", "--features", FEATURES,
           "--manifest-path", str(CRATE / "Cargo.toml")]
    for target in TARGETS:
        cmd += ["--filter-platform", target]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)


def crates(meta: dict) -> list:
    """[(package, in_binary)] of every crate coppermesh is built from, by name and version."""
    packages = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    root = meta["resolve"]["root"]

    def proc_macro(pid):
        return any("proc-macro" in t["kind"] for t in packages[pid]["targets"])

    def walk(kinds, through_proc_macros):
        seen, stack = set(), [root]
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            for dep in nodes[pid]["deps"]:
                if not any(k["kind"] in kinds for k in dep["dep_kinds"]):
                    continue
                if not through_proc_macros and proc_macro(dep["pkg"]):
                    continue
                stack.append(dep["pkg"])
        return seen - {root}

    every = walk((None, "build"), True)
    runtime = walk((None,), False)
    return sorted(((packages[pid], pid in runtime) for pid in every),
                  key=lambda e: (e[0]["name"], e[0]["version"]))


def licence_files(package: dict) -> list:
    folder = Path(package["manifest_path"]).parent
    files = sorted(f for f in folder.iterdir()
                   if f.is_file() and f.name.upper().startswith(LICENCE_PREFIXES))
    if package.get("license_file"):
        extra = folder / package["license_file"]
        if extra.is_file() and extra not in files:
            files.append(extra)
    return files


def _text(path: Path) -> str:
    text = path.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.strip("\n").splitlines())


def generate(meta: dict) -> str:
    entries = crates(meta)
    missing = [f"{p['name']} {p['version']}" for p, _ in entries if not licence_files(p)]
    if missing:
        raise SystemExit(f"error: no licence file in the source of: {', '.join(missing)}")
    title = "coppermesh: the Rust crates it is built from, and their licences"
    lines = [
        title, "=" * len(title), "",
        "Generated by tools/mesher_rs/crate_licences.py; do not edit by hand.",
        f"Feature: {FEATURES}. Targets: {', '.join(TARGETS)}.",
        f"SHA-256 of tools/mesher_rs/Cargo.lock: {sha256(CRATE / 'Cargo.lock')}",
        f"SHA-256 of tools/mesher_rs/Cargo.toml: {sha256(CRATE / 'Cargo.toml')}",
        "",
        "coppermesh (GPL-3.0-or-later, part of Coppernicus) is a Python extension written in Rust.",
        "The crates below are compiled into it (\"in the binary\") or run only while it is built",
        "(\"build time only\": build scripts and procedural macros). Each crate's licence files",
        "follow the list, as the crate ships them; identical texts are given once.",
        "",
    ]
    for package, in_binary in entries:
        where = "in the binary" if in_binary else "build time only"
        lines.append(f"  {MARKER.format(name=package['name'], version=package['version']):44s} "
                     f"{package.get('license') or 'see its licence file':40s} {where}")
    lines += ["", ""]
    texts: dict = {}
    for package, _ in entries:
        for path in licence_files(package):
            texts.setdefault(_text(path), []).append(f"{package['name']} {package['version']} ({path.name})")
    for text, users in texts.items():
        lines += ["=" * 78, "Licence text shipped by:"]
        lines += [f"  {u}" for u in users]
        lines += ["=" * 78, "", text, "", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="compare with the committed file instead of writing it")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    text = generate(metadata())
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if current != text:
            print(f"error: {args.out} is not what Cargo.lock gives: run tools/mesher_rs/crate_licences.py",
                  file=sys.stderr)
            return 1
        print(f"OK: {args.out.name} lists the {len(listed_crates(text))} crates Cargo.lock gives, "
              "with their licence texts")
        return 0
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out} ({len(listed_crates(text))} crates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
