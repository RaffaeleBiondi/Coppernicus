"""tools/mesher_rs/check_fma.py reads every disassembler's lines CI uses (GNU objdump on Linux,
llvm-objdump on Windows and macOS, otool) and counts every fused multiply-add form, nothing else."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_fma", ROOT / "tools" / "mesher_rs" / "check_fma.py")
check_fma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_fma)

LINES = [
    # GNU objdump, AT&T syntax, --no-show-raw-insn (Linux)
    "0000000000012340 <_ZN10coppermesh4geom6orient17h0123456789abcdefE>:",
    "   12340:\tvfmadd231sd %xmm2,%xmm1,%xmm0",
    "   12345:\tvfnmsub132pd %ymm2,%ymm1,%ymm0",
    "   1234a:\tvfmaddsub213ps %xmm2,%xmm1,%xmm0",
    "   1234f:\tvmulsd %xmm2,%xmm1,%xmm0",
    "   12354:\tlock cmpxchg %rcx,(%rdx)",
    "   12359:\tret",
    # llvm-objdump (Windows .pyd, x86_64; macOS arm64)
    "   180001000: \tvfmsub213sd\txmm0, xmm1, xmm2",
    "   180001005: \taddsd\txmm0, xmm1",
    "    302a4:      \tfmadd\td0, d4, d4, d0",
    "    302a8:      \tfmul\td1, d5, d5",
    "    302ac:      \tfmla.2d\tv0, v1, v2",
    # otool -tv (macOS arm64)
    "0000000000001000\tfnmsub\td0, d1, d2, d3",
    "0000000000001004\tfmls\tv0.2d, v1.2d, v2.2d",
    "0000000000001008\tfmov\td0, #1.00000000",
    "Disassembly of section .text:",
    "(__TEXT,__text) section",
]


def test_every_fma_form_counts_and_nothing_else():
    total, fma = check_fma.count(LINES)
    names = [check_fma.mnemonic(line) for line in fma]
    assert names == ["vfmadd231sd", "vfnmsub132pd", "vfmaddsub213ps", "vfmsub213sd", "fmadd", "fmla",
                     "fnmsub", "fmls"]
    assert total == 14                 # the symbol, section and header lines are not instructions


def test_a_prefix_is_skipped_to_the_mnemonic():
    assert check_fma.mnemonic("   12354:\tlock cmpxchg %rcx,(%rdx)") == "cmpxchg"
    assert check_fma.mnemonic("   12354:\t{vex} vfmadd231sd %xmm2,%xmm1,%xmm0") == "vfmadd231sd"
