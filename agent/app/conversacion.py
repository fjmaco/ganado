"""One message in, one Spanish reply out.

This is where the rules live that keep bad data out of his records without
making him feel interrogated:

* an unknown cow number is **asked about**, never auto-created — a fat-fingered
  digit would otherwise become a phantom cow that quietly skews every future
  average;
* a weight that jumps more than `SALTO_SOSPECHOSO_PCT` against her last one is
  **asked about** too;
* a voice note is read back for confirmation while `VOZ_REQUIERE_CONFIRMACION`
  is on, because Whisper can mishear a spelled-out number;
* and everything that *is* written gets echoed back with the change since last
  time, so a mistake is visible immediately and correctable in plain Spanish.

When several cows come in one message the suspicious-weight prompt is relaxed
to a ⚠️ marker in the summary — stopping a five-cow batch to interrogate one
entry costs more than it saves, and the echo still surfaces it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from . import messages as M
from . import reportes
from .config import cfg
from .db import db
from .entender import Consulta, Entendido, RegistroDetectado, entender
from .nombres import buscar_por_nombre
from .sheets import Pesaje, Vaca, hato, numero_canonico
from .texto import es_afirmativo, es_negativo, solo_digitos
from .transcribe import ErrorTranscripcion, transcribir_nota

log = logging.getLogger(__name__)


@dataclass
class Contexto:
    chat_id: str
    remitente: str
    msg_id: str
    texto: str = ""
    es_voz: bool = False
    nota: str = ""


def _hoy() -> date:
    return datetime.now(cfg.tz).date()


# --------------------------------------------------------------------------
# Escritura de un pesaje
# --------------------------------------------------------------------------

async def _escribir(
    ctx: Contexto, vaca: str, peso: float, nombre: str | None, sufijo: str = ""
) -> tuple[str, dict | None]:
    """Record one weighing and build its confirmation. Returns (texto, previo)."""
    hoy = _hoy()
    # Compare against the last *earlier* day, never against another reading
    # from today — same-day readings are one weighing session, so the gap
    # between them is a correction, not growth.
    previo = await hato.pesaje_anterior_a(vaca, hoy)

    # Already weighed today? Then this replaces it rather than stacking a
    # second row. He re-weighed her, or mistyped the first one; either way
    # there is exactly one true weight for this cow on this day.
    if (de_hoy := await hato.pesaje_del_dia(vaca, hoy)) is not None:
        anterior = float(de_hoy["peso"])
        if abs(anterior - peso) < 0.01:
            return M.registro_repetido(vaca, nombre, peso, hoy), previo

        await hato.actualizar_peso(int(de_hoy["fila"]), peso)
        await db.guardar_reciente(
            ctx.chat_id, ctx.msg_id, vaca, peso, int(de_hoy["fila"]), hoy.isoformat()
        )
        return M.registro_actualizado(vaca, nombre, anterior, peso, hoy), previo

    fila = await hato.registrar(
        Pesaje(
            fecha=hoy,
            vaca=vaca,
            peso=peso,
            origen="voz" if ctx.es_voz else "texto",
            # Suffixed when one message carries several cows, so each row keeps
            # a distinct id while still tracing back to the message it came from.
            msg_id=f"{ctx.msg_id}{sufijo}",
            nota=ctx.nota,
        )
    )
    await db.guardar_reciente(ctx.chat_id, ctx.msg_id, vaca, peso, fila, hoy.isoformat())

    gramos = None
    if previo:
        dias = max((hoy - previo["fecha"]).days, 0)
        if dias > 0:
            gramos = (peso - previo["peso"]) * 1000.0 / dias

    texto = M.registro_ok(
        vaca, nombre, peso, hoy,
        peso_previo=previo["peso"] if previo else None,
        fecha_previa=previo["fecha"] if previo else None,
        gramos_dia=gramos,
    )
    return texto, previo


def _sospechoso(peso: float, previo: dict | None) -> bool:
    if not previo or not previo.get("peso"):
        return False
    base = float(previo["peso"])
    return abs(peso - base) / base * 100.0 > cfg.salto_pct


# --------------------------------------------------------------------------
# Registrar (una o varias vacas)
# --------------------------------------------------------------------------

async def _registrar(
    ctx: Contexto, registros: list[RegistroDetectado], vacas: dict[str, Vaca],
    saltar_guardas: bool = False,
) -> str:
    if not registros:
        return M.NO_ENTENDI

    # Hard bounds first: a number outside these isn't a cow, it's a typo.
    for reg in registros:
        if not (cfg.peso_min <= reg.peso <= cfg.peso_max):
            return M.peso_fuera_de_rango(reg.peso, cfg.peso_min, cfg.peso_max)

    # Voice is read back before anything is written, while the flag is on.
    if ctx.es_voz and cfg.voz_requiere_confirmacion and not saltar_guardas:
        detalle = "\n".join(
            f"  • {M.etiqueta(r.vaca, vacas[r.vaca].nombre if r.vaca in vacas else None)}"
            f" — {M.fmt_kg(r.peso)} kg"
            for r in registros
        )
        await db.guardar_pendiente(
            ctx.chat_id, "confirmar_registros",
            {"registros": [{"vaca": r.vaca, "peso": r.peso} for r in registros],
             "msg_id": ctx.msg_id, "nota": ctx.nota, "es_voz": True},
        )
        return (
            f"🎤 Escuché:\n\n{detalle}\n\n"
            f"¿Está bien? Responde *SÍ* y lo anoto, o mándame el dato correcto."
        )

    conocidas = [r for r in registros if r.vaca in vacas]
    desconocidas = [r for r in registros if r.vaca not in vacas]

    # A single unknown cow and nothing else: ask, don't guess.
    if desconocidas and not conocidas:
        primera = desconocidas[0]
        await db.guardar_pendiente(
            ctx.chat_id, "crear_vaca",
            {"vaca": primera.vaca, "peso": primera.peso,
             "msg_id": ctx.msg_id, "nota": ctx.nota, "es_voz": ctx.es_voz},
        )
        return M.vaca_desconocida(primera.vaca, primera.peso)

    # One known cow: full guard, including the suspicious-jump question.
    if len(conocidas) == 1 and not desconocidas:
        reg = conocidas[0]
        vaca = vacas[reg.vaca]
        # Against the previous *day*: a second reading today is a re-weigh,
        # and flagging it as a "big change" is both wrong and confusing.
        previo = await hato.pesaje_anterior_a(reg.vaca, _hoy())
        if not saltar_guardas and _sospechoso(reg.peso, previo):
            await db.guardar_pendiente(
                ctx.chat_id, "peso_sospechoso",
                {"vaca": reg.vaca, "peso": reg.peso,
                 "msg_id": ctx.msg_id, "nota": ctx.nota, "es_voz": ctx.es_voz},
            )
            return M.peso_sospechoso(
                reg.vaca, vaca.nombre, reg.peso, previo["peso"], previo["fecha"]
            )
        texto, _ = await _escribir(ctx, reg.vaca, reg.peso, vaca.nombre)
        return texto

    # A batch: write everything known, flag the odd ones, then ask about the rest.
    lineas: list[str] = []
    for i, reg in enumerate(conocidas):
        vaca = vacas[reg.vaca]
        previo = await hato.pesaje_anterior_a(reg.vaca, _hoy())
        await _escribir(ctx, reg.vaca, reg.peso, vaca.nombre, sufijo=f"#{i}" if i else "")
        marca = " ⚠️ (cambio grande)" if _sospechoso(reg.peso, previo) else ""
        delta = ""
        if previo:
            delta = f"  (antes {M.fmt_kg(previo['peso'])} kg)"
        lineas.append(
            f"  • {M.etiqueta(reg.vaca, vaca.nombre)} — {M.fmt_kg(reg.peso)} kg{delta}{marca}"
        )

    respuesta = M.registro_multiple(lineas)

    if desconocidas:
        pendiente = desconocidas[0]
        await db.guardar_pendiente(
            ctx.chat_id, "crear_vaca",
            {"vaca": pendiente.vaca, "peso": pendiente.peso,
             "msg_id": ctx.msg_id, "nota": ctx.nota, "es_voz": ctx.es_voz},
        )
        respuesta += "\n\n" + M.vaca_desconocida(pendiente.vaca, pendiente.peso)

    return respuesta


# --------------------------------------------------------------------------
# Resolver una pregunta pendiente
# --------------------------------------------------------------------------

async def _resolver_pendiente(ctx: Contexto, pendiente: dict) -> str | None:
    """Handle a bare SÍ/NO answering our last question. None = not an answer."""
    tipo, datos = pendiente["tipo"], pendiente["datos"]

    if es_negativo(ctx.texto):
        await db.borrar_pendiente(ctx.chat_id)
        return M.CANCELADO

    if not es_afirmativo(ctx.texto):
        # He said something with actual content — let it be understood normally.
        return None

    await db.borrar_pendiente(ctx.chat_id)
    vacas = await hato.vacas()

    if tipo == "crear_vaca":
        numero, peso = datos["vaca"], float(datos["peso"])
        vaca = await hato.crear_vaca(numero, _hoy())
        ctx.es_voz = bool(datos.get("es_voz"))
        ctx.nota = datos.get("nota", "")
        hoy = _hoy()
        fila = await hato.registrar(
            Pesaje(
                fecha=hoy, vaca=numero, peso=peso,
                origen="voz" if ctx.es_voz else "texto",
                msg_id=f"{datos.get('msg_id', ctx.msg_id)}#nueva",
                nota=ctx.nota,
            )
        )
        await db.guardar_reciente(
            ctx.chat_id, ctx.msg_id, numero, peso, fila, hoy.isoformat()
        )
        return M.vaca_creada(numero, vaca.nombre, peso, hoy)

    if tipo == "peso_sospechoso":
        numero, peso = datos["vaca"], float(datos["peso"])
        ctx.es_voz = bool(datos.get("es_voz"))
        ctx.nota = datos.get("nota", "")
        nombre = vacas[numero].nombre if numero in vacas else None
        texto, _ = await _escribir(ctx, numero, peso, nombre)
        return texto

    if tipo == "baja_vaca":
        numero, motivo = datos["vaca"], datos.get("motivo", "otro")
        hoy = _hoy()
        await hato.retirar_vaca(numero, motivo, hoy)
        nombre = vacas[numero].nombre if numero in vacas else None
        return M.baja_hecha(numero, nombre, motivo, hoy)

    if tipo == "confirmar_registros":
        ctx.es_voz = bool(datos.get("es_voz", True))
        ctx.nota = datos.get("nota", "")
        registros = [
            RegistroDetectado(vaca=numero_canonico(d["vaca"]), peso=float(d["peso"]))
            for d in datos.get("registros", [])
        ]
        return await _registrar(ctx, registros, vacas, saltar_guardas=True)

    return None


# --------------------------------------------------------------------------
# Consultas (v2)
# --------------------------------------------------------------------------

async def _consultar(ctx: Contexto, consulta: Consulta, vacas: dict[str, Vaca]) -> str:
    registros = await hato.registros()
    nombres = {n: (v.nombre or "") for n, v in vacas.items()}
    activas = {n: v.nombre for n, v in vacas.items() if v.activa}
    # Las de baja conservan su historial pero no cuentan en el hato.
    activas_set = set(activas)

    if not registros:
        return M.SIN_DATOS

    tipo = consulta.tipo

    if tipo == "vaca":
        numero = consulta.vaca or buscar_por_nombre(ctx.texto, {k: v for k, v in nombres.items() if v})
        if not numero:
            return "🤔 ¿De cuál vaca quieres saber? Dime su número o su nombre."
        nombre = vacas[numero].nombre if numero in vacas else None
        cuerpo = reportes.texto_vaca(numero, nombre, reportes.historia(registros, numero))

    elif tipo in {"mejor_ganancia", "peor_ganancia"}:
        marco = reportes.solo_activas(reportes._marco(registros), activas_set)
        gs = reportes.ganancias(marco, nombres, consulta.periodo)
        cuerpo = reportes.texto_ranking(gs, mejor=(tipo == "mejor_ganancia"))

    elif tipo == "sin_pesar":
        marco = reportes._marco(registros)
        cuerpo = reportes.texto_sin_pesar(reportes.sin_pesar(marco, activas))

    elif tipo == "alertas":
        marco = reportes.solo_activas(reportes._marco(registros), activas_set)
        cuerpo = reportes.texto_alertas(reportes.alertas(marco, nombres))

    else:  # hato, total, promedio, minimo, maximo, conteo
        cuerpo = reportes.texto_hato(
            reportes.resumen(
                registros, nombres, consulta.periodo or "mes", activas=activas_set
            )
        )

    if frase := await reportes.comentario(cuerpo):
        cuerpo += f"\n\n_{frase}_"
    return cuerpo


# --------------------------------------------------------------------------
# Correcciones
# --------------------------------------------------------------------------

async def _corregir(ctx: Contexto, peso: float | None, vacas: dict[str, Vaca]) -> str:
    ultimo = await db.ultimo_reciente(ctx.chat_id)
    if not ultimo:
        return M.NADA_QUE_CORREGIR
    if peso is None:
        return "🤔 ¿Cuál es el peso correcto?"
    if not (cfg.peso_min <= peso <= cfg.peso_max):
        return M.peso_fuera_de_rango(peso, cfg.peso_min, cfg.peso_max)

    await hato.actualizar_peso(int(ultimo["fila"]), peso)
    await db.actualizar_reciente(int(ultimo["id"]), peso=peso)

    numero = ultimo["vaca"]
    nombre = vacas[numero].nombre if numero in vacas else None
    return M.corregido(numero, nombre, float(ultimo["peso"]), peso)


async def _borrar(ctx: Contexto, vacas: dict[str, Vaca]) -> str:
    ultimo = await db.ultimo_reciente(ctx.chat_id)
    if not ultimo:
        return M.NADA_QUE_CORREGIR

    await hato.anular(int(ultimo["fila"]))
    await db.actualizar_reciente(int(ultimo["id"]), anulado=True)

    numero = ultimo["vaca"]
    nombre = vacas[numero].nombre if numero in vacas else None
    fecha = date.fromisoformat(ultimo["fecha"]) if ultimo.get("fecha") else _hoy()
    return M.borrado(numero, nombre, float(ultimo["peso"]), fecha)


async def _retirar(ctx: Contexto, ent: Entendido, vacas: dict[str, Vaca]) -> str:
    """Ask before taking a cow out of the herd — it's not a weight you can retype."""
    numero = ent.vaca_referida
    if not numero or numero not in vacas:
        if numero and numero not in vacas:
            return f"🤔 No tengo ninguna vaca con el número {numero}. ¿Cuál fue?"
        return M.cual_vaca_baja()

    vaca = vacas[numero]
    if not vaca.activa:
        return M.vaca_ya_de_baja(numero, vaca.nombre, vaca.baja or "antes")

    ultimo = await hato.ultimo_pesaje(numero)
    await db.guardar_pendiente(
        ctx.chat_id, "baja_vaca", {"vaca": numero, "motivo": ent.motivo_baja}
    )
    return M.confirmar_baja(
        numero, vaca.nombre, ent.motivo_baja,
        ultimo_peso=ultimo["peso"] if ultimo else None,
        fecha_ultimo=ultimo["fecha"] if ultimo else None,
    )


async def _reactivar(ctx: Contexto, ent: Entendido, vacas: dict[str, Vaca]) -> str:
    numero = ent.vaca_referida
    if not numero:
        # La más probable es la última que dimos de baja.
        bajas = [v for v in vacas.values() if not v.activa and v.baja]
        if len(bajas) == 1:
            numero = bajas[0].numero
        else:
            return "🤔 ¿Cuál vaca quieres que vuelva al hato? Dime su número o su nombre."

    if numero not in vacas:
        return f"🤔 No tengo ninguna vaca con el número {numero}."
    if vacas[numero].activa:
        return f"ℹ️ {M.etiqueta(numero, vacas[numero].nombre)} ya está activa en el hato."

    await hato.reactivar_vaca(numero)
    return M.vaca_revivida(numero, vacas[numero].nombre)


async def _renombrar(ctx: Contexto, ent: Entendido, vacas: dict[str, Vaca]) -> str:
    numero = ent.vaca_referida
    if not numero:
        ultimo = await db.ultimo_reciente(ctx.chat_id)
        numero = ultimo["vaca"] if ultimo else None
    if not numero or numero not in vacas:
        return "🤔 ¿A cuál vaca le quieres cambiar el nombre? Dime su número."
    if not ent.nombre_nuevo:
        return "🤔 ¿Cómo la quieres llamar?"

    antes = vacas[numero].nombre or numero
    await hato.renombrar(numero, ent.nombre_nuevo)
    return M.vaca_renombrada(numero, antes, ent.nombre_nuevo)


# --------------------------------------------------------------------------
# Punto de entrada
# --------------------------------------------------------------------------

async def atender(mensaje: dict) -> str:
    """Process one inbound message and return the Spanish reply to send."""
    ctx = Contexto(
        chat_id=mensaje.get("chat_id") or "",
        remitente=solo_digitos(mensaje.get("remitente") or ""),
        msg_id=mensaje.get("msg_id") or "",
        texto=(mensaje.get("cuerpo") or "").strip(),
        es_voz=mensaje.get("tipo") in {"voice", "audio", "ptt"},
    )

    # 1. A voice note becomes text before anything else can look at it.
    if ctx.es_voz:
        try:
            ctx.texto = await transcribir_nota(ctx.chat_id, ctx.msg_id)
            ctx.nota = ctx.texto
        except ErrorTranscripcion as e:
            log.warning("transcripción fallida de %s: %s", ctx.msg_id, e)
            return M.ERROR_TRANSCRIBIENDO

    if not ctx.texto:
        return M.NO_ENTENDI

    vacas = await hato.vacas()

    # 2. Is this a plain yes/no answering our last question?
    if pendiente := await db.leer_pendiente(ctx.chat_id):
        if (respuesta := await _resolver_pendiente(ctx, pendiente)) is not None:
            return respuesta

    # 3. Understand it, however he phrased it.
    nombres = {n: v.nombre for n, v in vacas.items() if v.nombre}
    ent = await entender(ctx.texto, nombres, es_voz=ctx.es_voz)
    log.info(
        "mensaje %s vía %s -> %s (conf %.2f)",
        ctx.msg_id, ent.via, ent.intencion, ent.confianza,
    )

    # 4. Act on it.
    if ent.intencion == "registrar" and ent.registros:
        # A low-confidence reading of something that looks like a weight is
        # worth a question rather than a wrong row.
        if ent.via == "llm" and ent.confianza < 0.45:
            return M.NO_ENTENDI
        return await _registrar(ctx, ent.registros, vacas)

    if ent.intencion == "consultar":
        return await _consultar(ctx, ent.consulta or Consulta(), vacas)

    if ent.intencion == "corregir":
        return await _corregir(ctx, ent.peso_correccion, vacas)

    if ent.intencion == "borrar":
        return await _borrar(ctx, vacas)

    if ent.intencion == "retirar":
        return await _retirar(ctx, ent, vacas)

    if ent.intencion == "reactivar":
        return await _reactivar(ctx, ent, vacas)

    if ent.intencion == "renombrar":
        return await _renombrar(ctx, ent, vacas)

    if ent.intencion == "ayuda":
        return M.AYUDA

    if ent.intencion == "saludo":
        return M.SALUDO

    return M.NO_ENTENDI
