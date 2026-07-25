"""The queue under stress: bursts, duplicates, failures, crashes, restarts.

The other tests exercise the queue's methods. These run the *real* worker loop
against a *real* file-backed SQLite database and check the promise the whole
design rests on: a weight his dad sent is either recorded and confirmed, or he
is told it wasn't. Never silently lost, never silently doubled.

File-backed rather than in-memory on purpose — restarts and crash recovery
can't be tested against a database that dies with the process.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app import worker
from app.config import cfg
from app.db import BaseDatos


@pytest.fixture
def cola(tmp_path):
    bd = BaseDatos(str(tmp_path / "cola.db"))
    yield bd
    bd.cerrar()


class Espia:
    """Stands in for the whole downstream pipeline, and counts what happened."""

    def __init__(self) -> None:
        self.procesados: list[str] = []
        self.enviados: list[tuple[str, str]] = []
        self.fallar_veces: dict[str, int] = {}
        self.fallar_siempre: set[str] = set()

    async def atender(self, mensaje: dict) -> str:
        msg_id = mensaje["msg_id"]
        if msg_id in self.fallar_siempre:
            raise RuntimeError("Sheets caído")
        if self.fallar_veces.get(msg_id, 0) > 0:
            self.fallar_veces[msg_id] -= 1
            raise RuntimeError("fallo transitorio")
        self.procesados.append(msg_id)
        return f"ok {msg_id}"

    async def enviar_texto(self, chat_id: str, texto: str) -> str:
        self.enviados.append((chat_id, texto))
        return "m"

    async def marcar_escribiendo(self, chat_id: str) -> None:
        return None


@pytest.fixture
def espia(monkeypatch, cola):
    e = Espia()
    monkeypatch.setattr("app.worker.db", cola)
    monkeypatch.setattr("app.worker.atender", e.atender)
    monkeypatch.setattr("app.worker.openwa", e)
    return e


@pytest.fixture
def reintentos_cortos(monkeypatch):
    """Burn the retry budget in seconds instead of minutes.

    The real backoff is 5s, 15s, 45s, 135s — correct in production, but a test
    that waits it out honestly takes three minutes and nobody runs a suite like
    that. Two attempts exercise the same code path in about five seconds.
    """
    monkeypatch.setattr("app.db.cfg", replace(cfg, max_intentos=2))


async def _encolar(cola, msg_id: str, cuerpo: str = "477 327") -> bool:
    return await cola.encolar(
        msg_id=msg_id, chat_id="573001112233@c.us", remitente="573001112233",
        tipo="text", cuerpo=cuerpo, payload={},
    )


async def _drenar(cola, segundos: float = 6.0) -> None:
    """Run the real worker until the queue stops having work due."""
    parar = asyncio.Event()
    tarea = asyncio.create_task(worker.bucle(parar))
    limite = asyncio.get_event_loop().time() + segundos
    while asyncio.get_event_loop().time() < limite:
        await asyncio.sleep(0.05)
        prof = await cola.profundidad()
        if not prof.get("pendiente") and not prof.get("procesando"):
            break
    parar.set()
    await asyncio.wait_for(tarea, timeout=5)


# --- carga normal -----------------------------------------------------------

async def test_rafaga_se_procesa_completa_y_en_orden(cola, espia):
    """20 pesajes seguidos: todos, una sola vez, en el orden en que llegaron."""
    for i in range(20):
        await _encolar(cola, f"m{i:02d}")

    await _drenar(cola)

    assert espia.procesados == [f"m{i:02d}" for i in range(20)]
    assert len(espia.enviados) == 20
    assert (await cola.profundidad()).get("hecho") == 20


async def test_reentregas_no_duplican(cola, espia):
    """OpenWA reintenta la entrega: el mismo mensaje no puede anotarse dos veces."""
    for _ in range(5):
        await _encolar(cola, "repetido")
    await _encolar(cola, "otro")

    await _drenar(cola)

    assert espia.procesados.count("repetido") == 1
    assert sorted(espia.procesados) == ["otro", "repetido"]


async def test_los_mensajes_sobreviven_al_reinicio(cola, espia, tmp_path):
    """Encolar, 'apagar', volver a abrir: el pesaje sigue esperando."""
    for i in range(3):
        await _encolar(cola, f"m{i}")
    cola.cerrar()                      # se cae el proceso

    revivida = BaseDatos(str(tmp_path / "cola.db"))
    try:
        assert (await revivida.profundidad()).get("pendiente") == 3
    finally:
        revivida.cerrar()


# --- fallos -----------------------------------------------------------------

async def test_fallo_transitorio_termina_saliendo(cola, espia):
    """Sheets se cae un rato: el pesaje se anota igual, sólo más tarde."""
    await _encolar(cola, "m1")
    espia.fallar_veces["m1"] = 2       # falla dos veces, a la tercera pasa

    await _drenar(cola, segundos=30)

    assert espia.procesados == ["m1"]
    assert (await cola.profundidad()).get("hecho") == 1
    # Y no se le avisó de nada: para él fue un pesaje normal.
    assert all("No pude guardar" not in t for _, t in espia.enviados)


async def test_un_fallo_no_bloquea_a_los_demas(cola, espia, reintentos_cortos):
    """El mensaje malo se aparta; los buenos siguen pasando."""
    await _encolar(cola, "malo")
    espia.fallar_siempre.add("malo")
    for i in range(5):
        await _encolar(cola, f"bueno{i}")

    await _drenar(cola, segundos=15)

    assert sorted(espia.procesados) == [f"bueno{i}" for i in range(5)]


async def test_al_agotar_reintentos_se_le_avisa(cola, espia, reintentos_cortos):
    """Lo único inaceptable es el silencio: si no se guardó, se le dice."""
    from app import messages as M

    await _encolar(cola, "m1")
    espia.fallar_siempre.add("m1")

    await _drenar(cola, segundos=20)

    prof = await cola.profundidad()
    assert prof.get("fallido") == 1
    assert espia.procesados == []

    cuerpos = [t for _, t in espia.enviados]
    assert M.ERROR_GUARDANDO in cuerpos, "tiene que enterarse de que NO se guardó"
    assert any("no se pudo procesar" in t for t in cuerpos), "y yo tengo que enterarme"


async def test_el_reintento_espera_antes_de_volver(cola, espia):
    """El backoff existe: no se reintenta en bucle apretado contra una API caída."""
    await _encolar(cola, "m1")
    espia.fallar_siempre.add("m1")

    parar = asyncio.Event()
    tarea = asyncio.create_task(worker.bucle(parar))
    await asyncio.sleep(1.5)
    parar.set()
    await asyncio.wait_for(tarea, timeout=5)

    fila = cola._conexion().execute(
        "SELECT intentos FROM entrantes WHERE msg_id='m1'"
    ).fetchone()
    assert fila["intentos"] < cfg.max_intentos, "no debió quemar los reintentos de una"


# --- caídas a mitad de camino ----------------------------------------------

async def test_un_mensaje_a_medias_se_recupera(cola, espia):
    """Un redeploy justo mientras procesa no puede perder el pesaje."""
    await _encolar(cola, "m1")
    tomado = await cola.tomar()                  # queda 'procesando'
    assert tomado["msg_id"] == "m1"
    assert await cola.tomar() is None            # nadie más se lo lleva

    assert await cola.recuperar_huerfanos() == 1  # arranca de nuevo el servicio
    await _drenar(cola)

    assert espia.procesados == ["m1"]


async def test_timeout_no_traba_la_cola(cola, espia, monkeypatch):
    """Una llamada colgada se corta y deja pasar a los siguientes."""
    monkeypatch.setattr("app.worker.cfg", replace(cfg, timeout_mensaje=1))

    async def colgarse(mensaje):
        if mensaje["msg_id"] == "colgado":
            await asyncio.sleep(60)
        espia.procesados.append(mensaje["msg_id"])
        return "ok"

    monkeypatch.setattr("app.worker.atender", colgarse)

    await _encolar(cola, "colgado")
    await _encolar(cola, "normal")

    await _drenar(cola, segundos=12)

    assert "normal" in espia.procesados, "el de atrás no puede quedarse esperando"


# --- limpieza de la cola muerta --------------------------------------------

async def test_descartar_deja_la_cola_sana(cola, espia, reintentos_cortos):
    await _encolar(cola, "m1")
    espia.fallar_siempre.add("m1")
    await _drenar(cola, segundos=20)

    assert len(await cola.fallidos()) == 1
    assert await cola.descartar_fallidos() == 1
    assert await cola.fallidos() == []
    assert (await cola.profundidad()).get("fallido") is None
