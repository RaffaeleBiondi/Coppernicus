"""Numbers with their units in the app's fields: every numeric field shows its unit ("50 mΩ",
"500 kHz", "3 %") and takes a number typed with it or without it.

A number typed alone is in the unit the field's label names: "500" in a "fsw (kHz)" field is
500 kHz, "0.05" in a "(Ω)" cell is 0.05 Ω. Typed with an SI prefix or its unit it means what it
says, so "50m", "50 mΩ" and "0.05" are the same 0.05 Ω, and "0.5 MHz" in the kHz field is
500 kHz. A comma reads as the decimal sign. The units without SI prefixes (%, A/mm², °C, mm,
µm) take the number with or without the unit.

:class:`Unit` describes a field (the unit, the scale it shows, the scale the project holds),
:class:`UnitEdit` is a line edit for one, :func:`parse_si` and :func:`si_text` are a cell's
number with an SI prefix picked for its size (the Impedance tab's cells).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QLineEdit

_SI = {"p": -12, "n": -9, "u": -6, "µ": -6, "μ": -6, "m": -3, "k": 3, "K": 3, "M": 6, "G": 9}
_PREFIX_OF = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}
_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
# A unit as it may be typed after the prefix: Ω also as ohm (Ω is awkward on a keyboard).
UNIT_SPELLINGS = {"Ω": ("Ω", "Ω", "ohm", "ohms", "Ohm", "Ohms"), "Hz": ("Hz", "hz"), "F": ("F",),
                  "H": ("H",), "V": ("V",), "A": ("A",), "s": ("s",), "C": ("C",)}
# Units that take no SI prefix: the number with or without the unit.
PLAIN_SPELLINGS = {"%": ("%",), "A/mm²": ("A/mm²", "A/mm2", "A/mm^2"), "°C": ("°C", "degC", "C"),
                   "mm": ("mm",), "µm": ("µm", "μm", "um")}
_ANY_UNIT = sorted({u for us in UNIT_SPELLINGS.values() for u in us}, key=len, reverse=True)
_SI_RE = re.compile(rf"^({_NUMBER})\s*([pnuµμmkKMG]?)\s*({'|'.join(map(re.escape, _ANY_UNIT))})?$")
_PREFIXES = ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"),
             (1e-12, "p"))
SI_TIP = ("With or without its unit, SI prefixes work: p n u (µ) m k M G, so 50 mΩ = 50m = 0.05 and "
          "10 MHz = 10M = 1e7 (m is milli, M is mega; ohm for Ω)")
UNIT_TIP = ("A number alone is in the unit the label names; with an SI prefix or the unit it means "
            "what it says (p n u (µ) m k M G: 0.5 MHz = 500 kHz; m is milli, M is mega)")


def _unit_of(spelled: Optional[str]) -> Optional[str]:
    return next((u for u, spellings in UNIT_SPELLINGS.items() if spelled in spellings), None)


def _scaled(number: str, exp: int) -> float:
    """``number`` × 10^exp, exactly as typed ("33" at −3 is 0.033, not 0.033000000000000004)."""
    if exp == 0:
        return float(number)
    if "e" in number.lower():
        return float(number) * 10.0 ** exp
    return float(f"{number}e{exp}")


def _clean(text: Optional[str]) -> str:
    return (text or "").strip().replace(",", ".").replace("−", "-")


def _si_parts(text: Optional[str], unit: str):
    """(number, prefix, unit typed) of an SI field's text; None when empty. ValueError for
    anything else, a unit that is not the field's included."""
    t = _clean(text)
    if not t:
        return None
    m = _SI_RE.match(t)
    if not m:
        raise ValueError(f"{text.strip()!r} is not a number ({SI_TIP})")
    number, prefix, spelled = m.groups()
    if spelled is not None and _unit_of(spelled) != unit:
        want = f"in {unit}" if unit else "a plain number"
        raise ValueError(f"{text.strip()!r}: this field is {want}, not in {_unit_of(spelled)}")
    return number, prefix, spelled


def parse_si(text: Optional[str], unit: str = "") -> Optional[float]:
    """A number typed in a cell, with its unit or without: ``"50 mΩ"``, ``"50m"`` and
    ``"0.05"`` are all 0.05 for a cell in ohm; ``"10 MHz"`` → 1e7, ``"3,3"`` → 3.3; ``None``
    when empty. Raises ValueError for anything else, a unit that is not the cell's included."""
    parts = _si_parts(text, unit)
    if parts is None:
        return None
    number, prefix, _spelled = parts
    return _scaled(number, _SI[prefix] if prefix else 0)


def si_text(x: Optional[float], unit: str = "", digits: int = 4) -> str:
    """A number as a cell shows it, ``digits`` significant digits with an SI prefix and its
    unit ("50 mΩ"; "50m" without a unit; "" for None)."""
    if x is None:
        return ""
    if x == 0:
        return f"0 {unit}" if unit else "0"
    scale, prefix = _PREFIXES[-1]
    for sc, p in _PREFIXES:
        if abs(x) >= sc * 0.9995:
            scale, prefix = sc, p
            break
    number = f"{x / scale:.{digits}g}"
    return f"{number} {prefix}{unit}" if unit else f"{number}{prefix}"


@dataclass(frozen=True)
class Unit:
    """A numeric field: ``symbol`` its unit ("Hz", "%"; "" for a plain number); ``exp`` the
    power of ten it shows and a number typed alone is in (kHz: 3; None: an SI prefix picked
    for each value, a number alone in the unit itself); ``stored`` the power of ten the project
    holds it in (Qrr in nC: −9; 0 for the unit itself)."""

    symbol: str = ""
    exp: Optional[int] = 0
    stored: int = 0

    @property
    def plain(self) -> bool:
        """A unit without SI prefixes (%, A/mm², °C, mm, µm)."""
        return self.symbol in PLAIN_SPELLINGS

    @property
    def shown(self) -> str:
        """The unit as the field shows it ("kHz", "mV", "%")."""
        if self.plain or self.exp is None:
            return self.symbol
        return _PREFIX_OF[self.exp] + self.symbol

    def parse(self, text: Optional[str]) -> Optional[float]:
        """The value the project holds for ``text``; None when empty. ValueError for anything
        else."""
        if self.plain:
            t = _clean(text)
            if not t:
                return None
            for spelled in sorted(PLAIN_SPELLINGS[self.symbol], key=len, reverse=True):
                if t.endswith(spelled):
                    t = t[: -len(spelled)].strip()
                    break
            if not re.fullmatch(_NUMBER, t):
                raise ValueError(f"{text.strip()!r} is not a number (in {self.symbol})")
            return float(t)
        parts = _si_parts(text, self.symbol)
        if parts is None:
            return None
        number, prefix, spelled = parts
        if prefix:
            return _scaled(number, _SI[prefix] - self.stored)
        if spelled is not None:
            return _scaled(number, -self.stored)
        return _scaled(number, (self.exp or 0) - self.stored)

    def text(self, x: Optional[float], digits: int = 6) -> str:
        """``x`` (as the project holds it) as the field shows it, with its unit, in ``digits``
        significant digits ("" for None)."""
        if x is None:
            return ""
        if self.plain:
            return f"{x:.{digits}g} {self.symbol}"
        if self.exp is None:
            return si_text(x * 10.0 ** self.stored, self.symbol, digits)
        t = f"{x * 10.0 ** (self.stored - self.exp):.{digits}g}"
        return f"{t} {self.shown}" if self.shown else t

    def exact(self, x: Optional[float]) -> str:
        """:meth:`text` in as few digits as :meth:`parse` reads back exactly."""
        for digits in range(6, 18):
            t = self.text(x, digits)
            try:
                if self.parse(t) == x:
                    return t
            except ValueError:
                pass
        return t


class UnitValidator(QValidator):
    """Accepts what :meth:`Unit.parse` reads within ``lo``..``hi`` (as the project holds it),
    or an empty field when ``optional`` (each field says what empty means). Anything else is
    unfinished: the field then never emits editingFinished and keeps its text."""

    def __init__(self, unit: Unit, lo: Optional[float] = None, hi: Optional[float] = None,
                 optional: bool = True, parent=None):
        super().__init__(parent)
        self.unit, self.lo, self.hi, self.optional = unit, lo, hi, optional

    def validate(self, text, pos):
        try:
            value = self.unit.parse(text)
        except ValueError:
            return QValidator.Intermediate, text, pos
        if value is None:
            return (QValidator.Acceptable if self.optional else QValidator.Intermediate), text, pos
        if (self.lo is not None and value < self.lo) or (self.hi is not None and value > self.hi):
            return QValidator.Intermediate, text, pos
        return QValidator.Acceptable, text, pos


class UnitEdit(QLineEdit):
    """A line edit for a number with its unit: :meth:`value` reads it, :meth:`set_value` shows
    it (every digit: what is shown is what an untouched field commits), and a finished edit is
    shown again with its unit ("500" → "500 kHz") before any other slot reads it."""

    def __init__(self, unit: Unit, lo: Optional[float] = None, hi: Optional[float] = None,
                 optional: bool = True, placeholderText: str = "", parent=None):
        super().__init__(parent)
        self.unit = unit
        self.setValidator(UnitValidator(unit, lo, hi, optional, self))
        self.setPlaceholderText(placeholderText)
        self.editingFinished.connect(self._tidy)        # first: later slots read the tidy text

    def value(self) -> Optional[float]:
        return self.unit.parse(self.text())

    def set_value(self, x: Optional[float]) -> None:
        self.setText(self.unit.exact(x))

    def _tidy(self):
        try:
            t = self.unit.exact(self.value())
        except ValueError:
            return
        if t != self.text():
            self.setText(t)
