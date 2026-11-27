"""Count the fused multiply-add instructions in a built coppermesh extension; fail if any.

    python tools/mesher_rs/check_fma.py [BINARY ...] [--disassembler PATH] [--expect-fma]

coppermesh's meshes are bit-identical across architectures only while no a*b+c is fused
(DESIGN.md, determinism): Rust never contracts one unless ``mul_add`` is written, and this
check proves it on the binary each platform actually ships. Without BINARY, the extension of
the ``coppermesh`` package this Python finds is checked (``uv sync --group rust`` installs it).

Counted, per architecture (every operand form and width):
- x86_64: vfmadd*, vfmsub*, vfnmadd*, vfnmsub* (with vfmaddsub*, vfmsubadd*; FMA3, FMA4, AVX-512);
- arm64: fmadd, fmsub, fnmadd, fnmsub (scalar), and the vector fused forms fmla, fmls (NEON) and
  fmad, fmsb, fnmad, fnmsb, fnmla, fnmls (SVE).

The disassembler: ``--disassembler``, else ``llvm-objdump`` from the Rust toolchain (``rustup
component add llvm-tools``), else ``llvm-objdump`` or ``objdump`` on PATH, else ``otool`` (macOS).
It must yield at least MIN_INSTRUCTIONS instructions, or the check fails (an empty or unreadable
disassembly must not pass as "no FMA"). ``--expect-fma`` inverts the verdict, for a positive
control on a binary known to contain FMAs (SciPy's arm64 ``_ckdtree``, DESIGN.md §5).

Exit code 0: no FMA (or, with --expect-fma, some); 1: the opposite; 2: no binary or no disassembler.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MIN_INSTRUCTIONS = 1000
FMA = re.compile(
    r"^(?:vfn?m(?:add|sub)\w*"                    # x86_64: vfmadd231sd, vfnmsub132ps, vfmaddsubpd...
    r"|fn?m(?:add|sub)|fn?ml[as]|fn?mad|fn?msb)$"  # arm64: fmadd, fnmsub, fmla, fmls, fmad...
)
# One instruction line: an address (objdump: "1234:", otool: "0000000000001234"), then the
# mnemonic; with --no-show-raw-insn no bytes come between. Symbol lines ("... <name>:") start
# with "<" and are not instructions.
LINE = re.compile(r"^\s*[0-9a-fA-F]{4,}:?\s+([{A-Za-z][\w.{}]*)(?:\s+([A-Za-z][\w.]*))?")
PREFIXES = {"lock", "rep", "repe", "repz", "repne", "repnz", "notrack", "bnd", "data16", "{vex}",
            "{vex3}", "{evex}", "cs", "ds", "es", "fs", "gs", "ss"}
EXTENSIONS = ("*.so", "*.pyd", "*.dylib")


def mnemonic(line: str):
    """The mnemonic of one disassembly line (lower case, without an arrangement suffix such as
    Apple's ``fmla.2d``), or None for a line that is not an instruction."""
    m = LINE.match(line)
    if not m:
        return None
    first = m.group(1).lower()
    if first in PREFIXES and m.group(2):
        first = m.group(2).lower()
    return first.split(".")[0]


def count(lines):
    """(instructions, the FMA lines) of a disassembly."""
    total, fma = 0, []
    for line in lines:
        name = mnemonic(line)
        if name is None:
            continue
        total += 1
        if FMA.match(name):
            fma.append(line.strip())
    return total, fma


def _rust_llvm_objdump():
    rustc = shutil.which("rustc")
    if not rustc:
        return None
    try:
        sysroot = subprocess.run([rustc, "--print", "sysroot"], capture_output=True, text=True,
                                 check=True).stdout.strip()
        info = subprocess.run([rustc, "-vV"], capture_output=True, text=True, check=True).stdout
        host = next(line.split(":", 1)[1].strip() for line in info.splitlines() if line.startswith("host:"))
    except (OSError, subprocess.CalledProcessError, StopIteration):
        return None
    name = "llvm-objdump.exe" if os.name == "nt" else "llvm-objdump"
    exe = Path(sysroot) / "lib" / "rustlib" / host / "bin" / name
    return str(exe) if exe.is_file() else None


def find_disassembler(explicit=None):
    """The disassembler's command, as a list ending where the binary's path goes."""
    tool = explicit or _rust_llvm_objdump() or shutil.which("llvm-objdump") or shutil.which("objdump")
    if tool:
        if Path(tool).stem.lower() == "otool":
            return [tool, "-tv"]
        return [tool, "-d", "--no-show-raw-insn"]
    if shutil.which("otool"):
        return [shutil.which("otool"), "-tv"]
    return None


def default_binaries():
    """The extension file(s) of the installed coppermesh package."""
    spec = importlib.util.find_spec("coppermesh")
    if spec is None:
        return []
    dirs = [Path(p) for p in (spec.submodule_search_locations or [])] or [Path(spec.origin).parent]
    found = []
    for d in dirs:
        for pattern in EXTENSIONS:
            found += sorted(d.glob(f"coppermesh*{pattern[1:]}"))
    if not found and spec.origin and Path(spec.origin).suffix in (".so", ".pyd"):
        found = [Path(spec.origin)]
    return found


def check(binary: Path, command) -> tuple:
    run = subprocess.run([*command, str(binary)], capture_output=True, text=True, errors="replace")
    if run.returncode != 0:
        raise RuntimeError(f"{Path(command[0]).name} failed on {binary} ({run.returncode}): "
                           f"{run.stderr[-500:]}")
    return count(run.stdout.splitlines())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("binary", nargs="*", type=Path,
                    help="extension files (default: the installed coppermesh)")
    ap.add_argument("--disassembler", help="path of llvm-objdump, objdump or otool")
    ap.add_argument("--expect-fma", action="store_true",
                    help="positive control: pass only if FMAs are found")
    args = ap.parse_args(argv)
    binaries = args.binary or default_binaries()
    if not binaries:
        print("error: no binary given and no installed coppermesh extension found", file=sys.stderr)
        return 2
    command = find_disassembler(args.disassembler)
    if command is None:
        print("error: no disassembler (llvm-objdump, objdump or otool) found", file=sys.stderr)
        return 2
    print(f"disassembler: {' '.join(command)}")
    bad = False
    for binary in binaries:
        try:
            total, fma = check(binary, command)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"{binary}: {total} instructions, {len(fma)} fused multiply-add")
        for line in fma[:10]:
            print(f"  {line}")
        if total < MIN_INSTRUCTIONS:
            print(f"error: fewer than {MIN_INSTRUCTIONS} instructions: the disassembly is not usable",
                  file=sys.stderr)
            bad = True
        elif bool(fma) != args.expect_fma:
            print("error: " + ("fused multiply-add found: the meshes would differ across architectures"
                               if fma else "no fused multiply-add found where some were expected"),
                  file=sys.stderr)
            bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
