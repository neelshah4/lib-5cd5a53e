"""Canonical section-name mapping for the critical-care reading library."""
import re

SECTIONS = [
    "ECMO",
    "Respiratory/ARDS",
    "Shock & Sepsis",
    "Neurocritical Care",
    "Cardiac CC",
    "Renal",
    "Misc",
]

RIS_SAFE = {
    "ECMO": "ECMO",
    "Respiratory/ARDS": "Respiratory-ARDS",
    "Shock & Sepsis": "Shock_&_Sepsis",
    "Neurocritical Care": "Neurocritical_Care",
    "Cardiac CC": "Cardiac_CC",
    "Renal": "Renal",
    "Misc": "Misc",
}

# Map of normalized (lowercased, whitespace-collapsed) alias -> canonical short name.
_ALIASES = {
    "ecmo": "ECMO",
    "ecmo / extracorporeal membrane oxygenation": "ECMO",
    "respiratory/ards": "Respiratory/ARDS",
    "respiratory / ards": "Respiratory/ARDS",
    "respiratory / pulmonary & ards": "Respiratory/ARDS",
    "respiratory/pulmonary & ards": "Respiratory/ARDS",
    "respiratory & ards": "Respiratory/ARDS",
    "shock & sepsis": "Shock & Sepsis",
    "shock and sepsis": "Shock & Sepsis",
    "neurocritical care": "Neurocritical Care",
    "neurocrit": "Neurocritical Care",
    "cardiac cc": "Cardiac CC",
    "cardiac critical care": "Cardiac CC",
    "renal": "Renal",
    "misc": "Misc",
    "miscellaneous": "Misc",
}


def _normalize(name: str) -> str:
    s = name.strip()
    # strip leading markdown heading markers like "## "
    s = re.sub(r"^#+\s*", "", s)
    # strip leading emoji/symbol decoration like "⚡ "
    s = re.sub(r"^[^\w\[]+", "", s)
    # strip a trailing "(N)" paper-count annotation, e.g. "ECMO (5)"
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)
    # strip trailing markdown dash/em-dash annotations like " — notes"
    s = re.sub(r"\s*[—–-]\s*.*$", "", s)
    s = s.strip()
    # collapse internal whitespace
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def canonical(name: str) -> str:
    """Map any known spelling of a section name to its canonical short form.

    Raises ValueError on anything not in the known alias table (fail loud,
    never silently default).
    """
    if name is None:
        raise ValueError("section name is None")
    key = _normalize(str(name))
    if key in _ALIASES:
        return _ALIASES[key]
    raise ValueError(f"unrecognized section name: {name!r}")
