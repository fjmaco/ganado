"""The background worker that actually does the work.

The webhook only enqueues; everything downstream happens here, so a Sheets
outage or a rate-limited free model delays a confirmation instead of losing a
weight. Each message is retried with exponential backoff, and when it finally
gives up two people hear about it: you get the technical alert, and your dad
gets a plain Spanish note telling him it did not save and to send it again —
because the one unacceptable outcome is him believing a weight was recorded
when it wasn't.
"""

from __future__ import annotations

import asyncio
import logging

from . import messages as M
from .config import cfg
from .conversacion import atender
from .db import db
from .openwa import openwa

log = logging.getLogger(__name__)

INTERVALO_VACIO = 2.0  # seconds to sleep when the queue is empty


async def _avisar_admin(asunto: str, detalle: str) -> None:
    """Best-effort operational alert to you. Never derails the pipeline."""
    if not cfg.admin_whatsapp:
        return
    try:
        await openwa.enviar_texto(
            f"{cfg.admin_whatsapp}@c.us", M.alerta_admin(asunto, detalle)
        )
    except Exception as e:  # noqa: BLE001
        log.error("no se pudo avisar al admin: %s", e)


async def procesar_uno(mensaje: dict) -> None:
    """Understand one queued message, act on it, and reply."""
    chat_id = mensaje["chat_id"]
    await openwa.marcar_escribiendo(chat_id)

    respuesta = await atender(mensaje)
    if respuesta:
        await openwa.enviar_texto(chat_id, respuesta)


async def bucle(parar: asyncio.Event) -> None:
    """Drain the queue until asked to stop."""
    log.info("worker iniciado")
    while not parar.is_set():
        try:
            mensaje = await db.tomar()
        except Exception as e:  # noqa: BLE001
            log.exception("no se pudo leer la cola: %s", e)
            await asyncio.sleep(5)
            continue

        if mensaje is None:
            try:
                await asyncio.wait_for(parar.wait(), timeout=INTERVALO_VACIO)
            except asyncio.TimeoutError:
                pass
            continue

        msg_id = mensaje["msg_id"]
        try:
            await procesar_uno(mensaje)
            await db.marcar_hecho(msg_id)
            log.info("mensaje %s procesado", msg_id)

        except Exception as e:  # noqa: BLE001 — every failure is retryable here
            log.exception("fallo procesando %s: %s", msg_id, e)
            se_rindio = await db.reintentar(msg_id, f"{type(e).__name__}: {e}")

            if se_rindio:
                log.error("mensaje %s agotó los reintentos", msg_id)
                # Tell him it didn't save — silence would read as success.
                try:
                    await openwa.enviar_texto(mensaje["chat_id"], M.ERROR_GUARDANDO)
                except Exception:  # noqa: BLE001
                    log.error("tampoco se pudo avisar del fallo a %s", mensaje["chat_id"])
                await _avisar_admin(
                    "un mensaje no se pudo procesar",
                    f"De: {mensaje.get('remitente')}\n"
                    f"Texto: {(mensaje.get('cuerpo') or '(nota de voz)')[:200]}\n"
                    f"Error: {type(e).__name__}: {e}",
                )

    log.info("worker detenido")
