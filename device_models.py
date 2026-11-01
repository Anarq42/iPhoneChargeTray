"""Map Apple hardware identifiers (e.g. "iPhone15,2") to marketing names and icon hints.

Pure data and helpers: no Tk, no pystray, so it can be imported and tested anywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    identifier: str = ""
    name: str = "iPhone"
    lenses: int = 2          # rear cameras drawn on the icon
    pro: bool = False
    layout: str = "square"   # camera layout: "corner", "pill", "square" or "bar"
    generation: int = 0      # the number before the comma in "iPhone15,2"
    known: bool = False      # False when the identifier is not in the table


# identifier -> (marketing name, rear camera count)
_TABLE: dict[str, tuple[str, int]] = {
    "iPhone1,1": ("iPhone", 1),
    "iPhone1,2": ("iPhone 3G", 1),
    "iPhone2,1": ("iPhone 3GS", 1),
    "iPhone3,1": ("iPhone 4", 1), "iPhone3,2": ("iPhone 4", 1), "iPhone3,3": ("iPhone 4", 1),
    "iPhone4,1": ("iPhone 4s", 1),
    "iPhone5,1": ("iPhone 5", 1), "iPhone5,2": ("iPhone 5", 1),
    "iPhone5,3": ("iPhone 5c", 1), "iPhone5,4": ("iPhone 5c", 1),
    "iPhone6,1": ("iPhone 5s", 1), "iPhone6,2": ("iPhone 5s", 1),
    "iPhone7,2": ("iPhone 6", 1), "iPhone7,1": ("iPhone 6 Plus", 1),
    "iPhone8,1": ("iPhone 6s", 1), "iPhone8,2": ("iPhone 6s Plus", 1),
    "iPhone8,4": ("iPhone SE (1st generation)", 1),
    "iPhone9,1": ("iPhone 7", 1), "iPhone9,3": ("iPhone 7", 1),
    "iPhone9,2": ("iPhone 7 Plus", 2), "iPhone9,4": ("iPhone 7 Plus", 2),
    "iPhone10,1": ("iPhone 8", 1), "iPhone10,4": ("iPhone 8", 1),
    "iPhone10,2": ("iPhone 8 Plus", 2), "iPhone10,5": ("iPhone 8 Plus", 2),
    "iPhone10,3": ("iPhone X", 2), "iPhone10,6": ("iPhone X", 2),
    "iPhone11,2": ("iPhone XS", 2),
    "iPhone11,4": ("iPhone XS Max", 2), "iPhone11,6": ("iPhone XS Max", 2),
    "iPhone11,8": ("iPhone XR", 1),
    "iPhone12,1": ("iPhone 11", 2),
    "iPhone12,3": ("iPhone 11 Pro", 3),
    "iPhone12,5": ("iPhone 11 Pro Max", 3),
    "iPhone12,8": ("iPhone SE (2nd generation)", 1),
    "iPhone13,1": ("iPhone 12 mini", 2),
    "iPhone13,2": ("iPhone 12", 2),
    "iPhone13,3": ("iPhone 12 Pro", 3),
    "iPhone13,4": ("iPhone 12 Pro Max", 3),
    "iPhone14,4": ("iPhone 13 mini", 2),
    "iPhone14,5": ("iPhone 13", 2),
    "iPhone14,2": ("iPhone 13 Pro", 3),
    "iPhone14,3": ("iPhone 13 Pro Max", 3),
    "iPhone14,6": ("iPhone SE (3rd generation)", 1),
    "iPhone14,7": ("iPhone 14", 2),
    "iPhone14,8": ("iPhone 14 Plus", 2),
    "iPhone15,2": ("iPhone 14 Pro", 3),
    "iPhone15,3": ("iPhone 14 Pro Max", 3),
    "iPhone15,4": ("iPhone 15", 2),
    "iPhone15,5": ("iPhone 15 Plus", 2),
    "iPhone16,1": ("iPhone 15 Pro", 3),
    "iPhone16,2": ("iPhone 15 Pro Max", 3),
    "iPhone17,1": ("iPhone 16 Pro", 3),
    "iPhone17,2": ("iPhone 16 Pro Max", 3),
    "iPhone17,3": ("iPhone 16", 2),
    "iPhone17,4": ("iPhone 16 Plus", 2),
    "iPhone17,5": ("iPhone 16e", 1),
    "iPhone18,1": ("iPhone 17 Pro", 3),
    "iPhone18,2": ("iPhone 17 Pro Max", 3),
    "iPhone18,3": ("iPhone 17", 2),
    "iPhone18,4": ("iPhone Air", 1),
    "iPhone18,5": ("iPhone 17e", 1),
}

_ID_RE = re.compile(r"^iPhone(\d+),(\d+)$")


def _layout_for(name: str, lenses: int, generation: int) -> str:
    if name == "iPhone Air":
        return "bar"
    if lenses <= 1:
        return "corner"
    if lenses == 2 and generation < 12:
        return "pill"
    return "square"


def lookup_model(product_type: str | None) -> ModelInfo:
    """Return model details for an identifier such as "iPhone15,2".

    Unknown identifiers (newer than this table, or non-iPhone devices) still get a
    sensible generic icon; the identifier itself is shown so nothing is invented.
    """
    ident = (product_type or "").strip()
    match = _ID_RE.match(ident)
    generation = int(match.group(1)) if match else 0

    if ident in _TABLE:
        name, lenses = _TABLE[ident]
        return ModelInfo(
            identifier=ident,
            name=name,
            lenses=lenses,
            pro=" Pro" in name,
            layout=_layout_for(name, lenses, generation),
            generation=generation,
            known=True,
        )
    return ModelInfo(
        identifier=ident,
        name="iPhone",
        lenses=2,
        pro=False,
        layout="square",
        generation=generation,
        known=False,
    )
