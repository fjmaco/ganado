"""Small text helpers shared by the parser and the name registry.

Everything your dad types arrives with inconsistent accents and casing —
`Lucía`, `lucia`, `LUCIA` all have to resolve to the same cow — so matching is
always done on a normalised form while the original is what gets displayed.
"""

from __future__ import annotations

import re
import unicodedata

_ESPACIOS = re.compile(r"\s+")


def normalizar(s: str) -> str:
    """Lowercase, strip accents and collapse whitespace, for matching only."""
    if not s:
        return ""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )
    return _ESPACIOS.sub(" ", sin_tildes.lower()).strip()


def solo_digitos(s: str) -> str:
    """Keep digits only — used to normalise phone numbers and WhatsApp ids."""
    return re.sub(r"\D", "", s or "")


_NO_PALABRA = re.compile(r"[^a-z0-9ñ]+")


def tokenizar(s: str) -> str:
    """Normalise and turn every separator into a space.

    Needed so a name can be found in "¿cómo va Carmen?" — matching on raw
    normalised text would look for " carmen " and miss it against "carmen?".
    """
    return _ESPACIOS.sub(" ", _NO_PALABRA.sub(" ", normalizar(s))).strip()


_PUNTUACION = re.compile(r"[.!¡?¿,;:\-–—\s]+$")

# Deliberately exact-match only. A prefix rule would read "no, eran 445" as a
# plain "no" and cancel the entry instead of correcting it — so anything with
# more content than a bare yes/no goes to the model to be understood properly.
_AFIRMATIVOS = {
    "si", "s", "sii", "siii", "sisi", "ok", "oka", "okey", "okay", "dale",
    "listo", "claro", "correcto", "exacto", "asi es", "eso es", "confirmo",
    "confirmado", "de una", "obvio", "sip", "1", "yes", "si senor", "si señor",
    "claro que si", "si claro", "esta bien", "asi mismo", "afirmativo",
}
_NEGATIVOS = {
    "no", "n", "nop", "nope", "nel", "negativo", "cancela", "cancelar",
    "cancelalo", "olvidalo", "olvida", "dejalo", "no gracias", "2", "nada",
    "no no", "para nada", "asi no",
}


def _limpio(s: str) -> str:
    return _PUNTUACION.sub("", normalizar(s))


def es_afirmativo(s: str) -> bool:
    """A bare yes — nothing else in the message."""
    return _limpio(s) in _AFIRMATIVOS


def es_negativo(s: str) -> bool:
    """A bare no — nothing else in the message."""
    return _limpio(s) in _NEGATIVOS
