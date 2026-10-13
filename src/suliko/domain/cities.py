"""The Georgian cities a translator can say they certify in.

A fixed list: codes are stored, names are shown by the app in each language.
"""

from __future__ import annotations

CITIES: tuple[str, ...] = (
    "tbilisi",
    "batumi",
    "kutaisi",
    "rustavi",
    "gori",
    "zugdidi",
    "poti",
    "telavi",
    "akhaltsikhe",
    "ozurgeti",
    "kobuleti",
    "mtskheta",
    "sighnaghi",
    "borjomi",
    "marneuli",
    "gardabani",
    "samtredia",
    "senaki",
    "khashuri",
    "sachkhere",
)


def pack(codes: list[str]) -> str | None:
    """Codes as the stored string, or None for none."""
    return "," + ",".join(codes) + "," if codes else None


def unpack(stored: str | None) -> list[str]:
    return [c for c in (stored or "").split(",") if c]
