"""Watching the WhatsApp link itself.

The failure that costs the most is the quiet one: the Baileys session drops —
phone off, WhatsApp update, a ban — and the bot simply stops answering. He
sends weights into the void, assumes they landed, and finds out a month later
that nothing was recorded.

**There is an unavoidable catch: when WhatsApp is down, WhatsApp cannot be
used to report it.** So this does three things instead of pretending otherwise:

1. records the state in SQLite, so `/health` reports it and anything watching
   from outside can see it;
2. tries to notify anyway — a `qr_ready` or a reconnect often still allows
   sending;
3. tells you *on reconnect* how long it was down, which is the one message
   guaranteed to be deliverable.
"""

from __future__ import annotations

import logging
from datetime import datetime

from . import messages as M
from .config import cfg
from .db import db
from .openwa import openwa

log = logging.getLogger(__name__)

CLAVE_ESTADO = "estado_sesion"
CLAVE_DESDE = "sesion_caida_desde"

# OpenWA's own wire values.
SANOS = {"ready"}
CAIDOS = {"disconnected", "failed", "qr_ready", "authenticating"}

EVENTOS = (
    "message.received",
    "session.status",
    "session.disconnected",
    "session.qr",
    "session.authenticated",
)


def _describir(estado: str) -> str:
    return {
        "disconnected": "se desconectó",
        "failed": "falló",
        "qr_ready": "pide escanear el QR otra vez",
        "authenticating": "está reconectando",
        "ready": "volvió a estar lista",
    }.get(estado, f"cambió a «{estado}»")


async def registrar_evento(evento: str, datos: dict) -> None:
    """Handle a session lifecycle event from OpenWA."""
    estado = str(
        datos.get("status") or datos.get("state") or
        ("qr_ready" if evento == "session.qr" else
         "disconnected" if evento == "session.disconnected" else
         "ready" if evento == "session.authenticated" else "")
    ).lower()
    if not estado:
        return

    anterior = await db.leer_ajuste(CLAVE_ESTADO)
    if estado == anterior:
        return

    await db.guardar_ajuste(CLAVE_ESTADO, estado)
    ahora = datetime.now(cfg.tz)
    log.warning("la sesión de WhatsApp %s (antes: %s)", _describir(estado), anterior)

    if estado in CAIDOS:
        if not await db.leer_ajuste(CLAVE_DESDE):
            await db.guardar_ajuste(CLAVE_DESDE, ahora.isoformat())
        detalle = f"La sesión {_describir(estado)}."
        if estado == "qr_ready":
            detalle += "\n\n⚠️ Hay que volver a emparejar el teléfono en el dashboard."
        # Best-effort: often impossible, precisely because the link is down.
        await _intentar_avisar("WhatsApp caído", detalle)
        return

    if estado in SANOS:
        desde = await db.leer_ajuste(CLAVE_DESDE)
        await db.guardar_ajuste(CLAVE_DESDE, "")
        if desde:
            try:
                caida = datetime.fromisoformat(desde)
                minutos = int((ahora - caida).total_seconds() // 60)
                await _intentar_avisar(
                    "WhatsApp volvió",
                    f"Estuvo caído {minutos} min (desde {caida:%d/%m %H:%M}).\n\n"
                    f"Vale la pena preguntarle a mi papá si mandó algo mientras tanto.",
                )
            except ValueError:
                pass


async def _intentar_avisar(asunto: str, detalle: str) -> None:
    if not cfg.admin_whatsapp:
        return
    try:
        await openwa.enviar_texto(
            f"{cfg.admin_whatsapp}@c.us", M.alerta_admin(asunto, detalle)
        )
    except Exception as e:  # noqa: BLE001 — esperable: el enlace es justo lo que falló
        log.error("no se pudo avisar del estado de la sesión (era de esperarse): %s", e)


async def estado_actual() -> dict:
    """What /health reports about the WhatsApp link."""
    estado = await db.leer_ajuste(CLAVE_ESTADO) or "desconocido"
    desde = await db.leer_ajuste(CLAVE_DESDE)
    return {
        "estado": estado,
        "conectada": estado in SANOS or estado == "desconocido",
        "caida_desde": desde or None,
    }
