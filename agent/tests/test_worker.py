"""The worker's failure behaviour.

The unacceptable outcome is silence: if a weight could not be saved, he has to
be told, because saying nothing reads exactly like success.
"""

from __future__ import annotations

import asyncio

import pytest

from app import messages as M
from app import worker
from app.config import cfg


@pytest.fixture(autouse=True)
def _cablear(monkeypatch, base, openwa_falso):
    monkeypatch.setattr("app.worker.db", base)
    monkeypatch.setattr("app.worker.openwa", openwa_falso)
    return base


CHAT = "573001112233@c.us"


async def _encolar(base, msg_id="m1"):
    await base.encolar(
        msg_id=msg_id, chat_id=CHAT, remitente="573001112233",
        tipo="text", cuerpo="477 327", payload={},
    )


async def _correr_un_ciclo(base):
    """Drive the loop through exactly one queued message."""
    parar = asyncio.Event()
    tarea = asyncio.create_task(worker.bucle(parar))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if (await base.profundidad()).get("pendiente", 0) == 0:
            break
    parar.set()
    await asyncio.wait_for(tarea, timeout=5)


async def test_mensaje_ok_se_responde_y_se_marca_hecho(monkeypatch, base, openwa_falso):
    async def responder(mensaje):
        return "✅ listo"

    monkeypatch.setattr("app.worker.atender", responder)
    await _encolar(base)
    await _correr_un_ciclo(base)

    assert openwa_falso.enviados == [(CHAT, "✅ listo")]
    assert (await base.profundidad()).get("hecho") == 1


async def test_fallo_transitorio_se_reintenta_sin_avisar(monkeypatch, base, openwa_falso):
    """Un tropiezo pasajero no debe alarmarlo: se reintenta en silencio."""
    async def explotar(mensaje):
        raise RuntimeError("Sheets 500")

    monkeypatch.setattr("app.worker.atender", explotar)
    await _encolar(base)
    await _correr_un_ciclo(base)

    assert openwa_falso.enviados == [], "no se le avisa en el primer fallo"
    assert (await base.profundidad()).get("pendiente") == 1


async def test_avisa_en_espanol_que_no_se_guardo(monkeypatch, base, openwa_falso):
    """El mensaje al papá es en español y no técnico."""
    async def explotar(mensaje):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.worker.atender", explotar)
    monkeypatch.setattr(
        "app.worker.db.reintentar", lambda *a, **k: _verdadero()
    )
    await _encolar(base)
    await _correr_un_ciclo(base)

    destinos = [chat for chat, _ in openwa_falso.enviados]
    cuerpos = [cuerpo for _, cuerpo in openwa_falso.enviados]

    assert CHAT in destinos
    assert M.ERROR_GUARDANDO in cuerpos
    # Y a ti te llega la alerta técnica, por separado.
    assert any(cfg.admin_whatsapp in d for d in destinos)
    assert any("boom" in c for c in cuerpos)


async def _verdadero() -> bool:
    return True


async def test_respuesta_vacia_no_manda_nada(monkeypatch, base, openwa_falso):
    async def callar(mensaje):
        return ""

    monkeypatch.setattr("app.worker.atender", callar)
    await _encolar(base)
    await _correr_un_ciclo(base)

    assert openwa_falso.enviados == []
    assert (await base.profundidad()).get("hecho") == 1
