"""The queue — the piece that makes "nothing gets lost" true.

Idempotency, retry backoff, dead-lettering and crash recovery all live here,
because every one of them is the difference between a delayed confirmation and
a weight your dad thinks he recorded but didn't.
"""

from __future__ import annotations

import time

from app.db import FALLIDO, HECHO, PENDIENTE, BaseDatos


async def _encolar(bd: BaseDatos, msg_id: str = "m1") -> bool:
    return await bd.encolar(
        msg_id=msg_id, chat_id="573001112233@c.us", remitente="573001112233",
        tipo="text", cuerpo="477 327", payload={"id": msg_id},
    )


async def test_encolar_y_tomar(base):
    assert await _encolar(base) is True

    mensaje = await base.tomar()
    assert mensaje is not None
    assert mensaje["msg_id"] == "m1"
    assert mensaje["cuerpo"] == "477 327"
    assert mensaje["payload"] == {"id": "m1"}

    # Ya está en vuelo: nadie más se lo puede llevar.
    assert await base.tomar() is None


async def test_mismo_msg_id_no_se_duplica(base):
    """Un webhook reentregado no puede convertirse en dos pesajes."""
    assert await _encolar(base, "m1") is True
    assert await _encolar(base, "m1") is False

    await base.tomar()
    assert await base.tomar() is None


async def test_marcar_hecho(base):
    await _encolar(base)
    await base.tomar()
    await base.marcar_hecho("m1")

    profundidad = await base.profundidad()
    assert profundidad.get(HECHO) == 1
    assert await base.tomar() is None


async def test_reintento_programa_hacia_el_futuro(base):
    """Tras fallar, el mensaje no se reintenta de inmediato."""
    await _encolar(base)
    await base.tomar()

    se_rindio = await base.reintentar("m1", "Sheets 500")
    assert se_rindio is False

    # Vuelve a 'pendiente' pero con espera, así que aún no lo toma.
    assert (await base.profundidad()).get(PENDIENTE) == 1
    assert await base.tomar() is None


async def test_se_rinde_tras_el_maximo_de_intentos(base):
    from app.config import cfg

    await _encolar(base)
    resultados = []
    for _ in range(cfg.max_intentos):
        resultados.append(await base.reintentar("m1", "error"))

    assert resultados[-1] is True, "debió rendirse en el último intento"
    assert resultados[:-1] == [False] * (cfg.max_intentos - 1)
    assert (await base.profundidad()).get(FALLIDO) == 1


async def test_recupera_mensajes_a_medio_procesar(base):
    """Un redeploy en medio de un mensaje no puede dejarlo colgado para siempre."""
    await _encolar(base)
    await base.tomar()          # queda 'procesando'
    assert await base.tomar() is None

    assert await base.recuperar_huerfanos() == 1
    mensaje = await base.tomar()
    assert mensaje is not None and mensaje["msg_id"] == "m1"


async def test_orden_fifo(base):
    for i in range(3):
        await _encolar(base, f"m{i}")
        time.sleep(0.001)

    tomados = []
    for _ in range(3):
        mensaje = await base.tomar()
        tomados.append(mensaje["msg_id"])
        await base.marcar_hecho(mensaje["msg_id"])

    assert tomados == ["m0", "m1", "m2"]


# --- preguntas pendientes --------------------------------------------------

async def test_pendiente_se_guarda_y_se_lee(base):
    await base.guardar_pendiente("chat1", "crear_vaca", {"vaca": "34", "peso": 450})

    pendiente = await base.leer_pendiente("chat1")
    assert pendiente["tipo"] == "crear_vaca"
    assert pendiente["datos"]["vaca"] == "34"


async def test_solo_una_pregunta_pendiente_por_chat(base):
    await base.guardar_pendiente("chat1", "crear_vaca", {"vaca": "34"})
    await base.guardar_pendiente("chat1", "peso_sospechoso", {"vaca": "477"})

    pendiente = await base.leer_pendiente("chat1")
    assert pendiente["tipo"] == "peso_sospechoso"


async def test_pendiente_se_borra(base):
    await base.guardar_pendiente("chat1", "crear_vaca", {"vaca": "34"})
    await base.borrar_pendiente("chat1")
    assert await base.leer_pendiente("chat1") is None


async def test_pendiente_vencido_no_se_devuelve(base):
    """Un 'SÍ' de mañana no debe contestar la pregunta de hoy."""
    await base.guardar_pendiente("chat1", "crear_vaca", {"vaca": "34"})
    con = base._conexion()
    con.execute("UPDATE pendientes SET expira = ? WHERE chat_id = ?", (time.time() - 1, "chat1"))
    con.commit()

    assert await base.leer_pendiente("chat1") is None


# --- pesajes recientes -----------------------------------------------------

async def test_ultimo_reciente_para_corregir(base):
    await base.guardar_reciente("chat1", "m1", "477", 327, 5, "2026-07-24")
    await base.guardar_reciente("chat1", "m2", "348", 512, 6, "2026-07-24")

    ultimo = await base.ultimo_reciente("chat1")
    assert ultimo["vaca"] == "348"
    assert ultimo["fila"] == 6


async def test_reciente_anulado_deja_de_ser_el_ultimo(base):
    await base.guardar_reciente("chat1", "m1", "477", 327, 5, "2026-07-24")
    await base.guardar_reciente("chat1", "m2", "348", 512, 6, "2026-07-24")

    ultimo = await base.ultimo_reciente("chat1")
    await base.actualizar_reciente(ultimo["id"], anulado=True)

    anterior = await base.ultimo_reciente("chat1")
    assert anterior["vaca"] == "477"


async def test_recientes_no_se_cruzan_entre_chats(base):
    await base.guardar_reciente("chat1", "m1", "477", 327, 5, "2026-07-24")
    assert await base.ultimo_reciente("chat2") is None
