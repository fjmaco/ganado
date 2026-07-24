"""FastAPI service: receive WhatsApp webhooks, queue them, answer in Spanish.

The endpoint's contract is deliberately narrow — authenticate, enqueue, return
200 — because anything it does inline is work that can fail while OpenWA is
waiting, and a slow webhook gets retried and eventually dropped upstream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Header, Request, Response, status

from . import agenda, worker
from .config import cfg
from .db import db
from .openwa import openwa
from .security import firma_valida, remitente_permitido
from .sheets import hato
from .texto import solo_digitos

logging.basicConfig(
    level=getattr(logging, cfg.log_level, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("pericos")

# Created inside the lifespan, not at import: an asyncio.Event binds to the
# first loop that awaits it, so a module-level one would break on any restart
# that runs in a fresh loop.
_parar: asyncio.Event | None = None
_tarea: asyncio.Task | None = None
_agenda: asyncio.Task | None = None


@contextlib.asynccontextmanager
async def ciclo_vida(app: FastAPI) -> AsyncIterator[None]:
    global _tarea, _parar, _agenda

    if faltantes := cfg.validar():
        # Fail loudly at boot rather than at 6am with a cow on the scale.
        raise RuntimeError(
            "Faltan variables de entorno obligatorias: " + ", ".join(faltantes)
        )

    log.info("arrancando pericos · sesión=%s · openwa=%s", cfg.openwa_session, cfg.openwa_url)

    try:
        await hato.asegurar_estructura()
        log.info("estructura de la hoja verificada")
    except Exception as e:  # noqa: BLE001 — retried on first real write
        log.error("no se pudo verificar la hoja al arrancar: %s", e)

    # A redeploy mid-message would otherwise strand it as 'procesando'.
    if recuperados := await db.recuperar_huerfanos():
        log.warning("se re-encolaron %d mensajes que quedaron a medias", recuperados)

    _parar = asyncio.Event()
    _tarea = asyncio.create_task(worker.bucle(_parar), name="worker")
    _agenda = asyncio.create_task(agenda.bucle(_parar), name="agenda")

    yield

    log.info("apagando…")
    _parar.set()
    for tarea in (_tarea, _agenda):
        if tarea:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(tarea, timeout=20)
    await openwa.cerrar()
    db.cerrar()


app = FastAPI(title="Pericos — registro de ganado", version="1.0", lifespan=ciclo_vida)


@app.get("/health")
async def health() -> dict:
    cola = await db.profundidad()
    return {
        "estado": "ok",
        "cola": cola,
        # A growing 'fallido' count is the signal that something needs a human.
        "fallidos": cola.get("fallido", 0),
        "worker": bool(_tarea and not _tarea.done()),
    }


@app.post("/webhook/openwa")
async def webhook(
    request: Request,
    x_openwa_signature: str | None = Header(default=None),
) -> Response:
    crudo = await request.body()

    if not firma_valida(crudo, x_openwa_signature):
        log.warning("firma inválida desde %s", request.client.host if request.client else "?")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        evento = await request.json()
    except Exception:  # noqa: BLE001
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    if evento.get("event") != "message.received":
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    datos = evento.get("data") or {}

    # Ignore our own echoes, groups, and status broadcasts.
    if datos.get("fromMe") or datos.get("isGroup") or datos.get("kind") not in (None, "individual"):
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    origen = datos.get("from") or ""
    remitente = solo_digitos(datos.get("senderPhone") or origen)
    if not remitente_permitido(remitente):
        log.info("mensaje ignorado de un remitente no autorizado (%s)", remitente[-4:])
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    msg_id = datos.get("id")
    chat_id = datos.get("chatId") or origen
    if not msg_id or not chat_id:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Persist, then return. If this insert fails we answer non-2xx so OpenWA
    # redelivers — that retry is the only thing standing between a transient
    # disk error and a lost weighing.
    try:
        nuevo = await db.encolar(
            msg_id=msg_id,
            chat_id=chat_id,
            remitente=remitente,
            tipo=datos.get("type") or "text",
            cuerpo=datos.get("body") or "",
            payload=datos,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("no se pudo encolar %s: %s", msg_id, e)
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    if not nuevo:
        log.info("mensaje %s ya estaba encolado; ignorado", msg_id)

    return Response(status_code=status.HTTP_202_ACCEPTED)
