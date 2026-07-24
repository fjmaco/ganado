"""Webhook authentication and the sender allowlist.

OpenWA already filters deliveries to the allowed senders server-side, so this
is the second of two independent checks. That redundancy is deliberate: the
filter lives in OpenWA's database and a careless edit in its dashboard would
silently open this endpoint to anyone who guessed the URL.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from .config import cfg
from .texto import solo_digitos

log = logging.getLogger(__name__)


def firma_valida(cuerpo: bytes, cabecera: str | None) -> bool:
    """Verify `X-OpenWA-Signature: sha256=<hex>` over the RAW request body.

    The HMAC must be recomputed over the exact bytes received — re-serialising
    a parsed dict reorders keys and changes whitespace, which would make every
    signature fail. Comparison is constant-time.
    """
    if not cfg.webhook_secret:
        # Refuse rather than run unauthenticated: an unsigned endpoint that
        # writes to his records is worse than one that is down.
        log.error("OPENWA_WEBHOOK_SECRET no está configurado; rechazando la petición")
        return False

    if not cabecera:
        return False

    recibida = cabecera.strip()
    if recibida.lower().startswith("sha256="):
        recibida = recibida[7:]

    esperada = hmac.new(
        cfg.webhook_secret.encode("utf-8"), cuerpo, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(esperada, recibida.lower())


def remitente_permitido(numero: str) -> bool:
    """Is this number on the allowlist? Compared on digits only."""
    if not cfg.remitentes_permitidos:
        return False
    objetivo = solo_digitos(numero)
    if not objetivo:
        return False
    return any(
        objetivo == solo_digitos(p) or objetivo.endswith(solo_digitos(p)[-10:])
        for p in cfg.remitentes_permitidos
        if solo_digitos(p)
    )
