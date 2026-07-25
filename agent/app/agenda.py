"""The monthly summary he never has to ask for.

A notebook can't tell you the herd gained 240 kg last month, or that three cows
went unweighed. Once a month this pushes that on its own — which is the whole
point of the thing being automated rather than just digital.

Deliberately simple: no cron, no scheduler dependency. The loop wakes up now
and then, and a "have I already sent this month?" flag in SQLite makes a
duplicate impossible no matter how often it checks or how many times the
container restarts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from . import reportes
from .config import cfg
from .db import db
from .openwa import openwa
from .sheets import hato

log = logging.getLogger(__name__)

CLAVE = "ultimo_resumen_mensual"
INTERVALO = 900  # revisar cada 15 minutos


async def _construir() -> str:
    vacas = await hato.vacas(refrescar=True)
    registros = await hato.registros(refrescar=True)
    if not registros:
        return ""

    nombres = {n: (v.nombre or "") for n, v in vacas.items()}
    activas = {n for n, v in vacas.items() if v.activa}
    cuerpo = reportes.texto_hato(
        reportes.resumen(registros, nombres, periodo="mes", activas=activas)
    )

    marco = reportes.solo_activas(reportes._marco(registros), activas)
    if bajaron := reportes.alertas(marco, nombres):
        cuerpo += "\n\n" + reportes.texto_alertas(bajaron)

    encabezado = "📅 *Resumen del mes*\n\n"
    if frase := await reportes.comentario(cuerpo):
        cuerpo += f"\n\n_{frase}_"
    return encabezado + cuerpo


async def _toca_ahora(ahora: datetime) -> bool:
    if not cfg.resumen_activo:
        return False
    if ahora.day != cfg.resumen_dia or ahora.hour < cfg.resumen_hora:
        return False
    # Un solo envío por mes, pase lo que pase con los reinicios.
    return await db.leer_ajuste(CLAVE) != ahora.strftime("%Y-%m")


async def enviar_resumen(ahora: datetime) -> bool:
    """Build and push the summary to every allowed number. True if it went out."""
    cuerpo = await _construir()
    if not cuerpo:
        log.info("resumen mensual omitido: todavía no hay pesajes")
        await db.guardar_ajuste(CLAVE, ahora.strftime("%Y-%m"))
        return False

    enviados = 0
    for numero in cfg.remitentes_permitidos:
        try:
            await openwa.enviar_texto(f"{numero}@c.us", cuerpo)
            enviados += 1
        except Exception as e:  # noqa: BLE001 — un número caído no bloquea al otro
            log.error("no se pudo mandar el resumen a %s: %s", numero[-4:], e)

    if enviados:
        # Sólo se marca como hecho si alguien lo recibió; si no, se reintenta
        # en el siguiente ciclo en vez de perderse el mes entero.
        await db.guardar_ajuste(CLAVE, ahora.strftime("%Y-%m"))
    return bool(enviados)


async def bucle(parar: asyncio.Event) -> None:
    log.info(
        "agenda iniciada · resumen el día %d a las %02d:00 (%s)",
        cfg.resumen_dia, cfg.resumen_hora, cfg.tz_nombre,
    )
    while not parar.is_set():
        try:
            ahora = datetime.now(cfg.tz)
            if await _toca_ahora(ahora):
                log.info("mandando el resumen mensual")
                await enviar_resumen(ahora)
        except Exception as e:  # noqa: BLE001 — nunca tumbar el bucle
            log.exception("fallo en la agenda: %s", e)

        try:
            await asyncio.wait_for(parar.wait(), timeout=INTERVALO)
        except asyncio.TimeoutError:
            pass

    log.info("agenda detenida")
