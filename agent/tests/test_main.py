"""The HTTP surface: authenticate, enqueue, return fast.

The endpoint deliberately does no real work — everything it accepts is handed
to the queue — so these tests are about what gets in and what gets turned away.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import cfg


@pytest.fixture
def cliente(monkeypatch, base):
    """App with the worker stubbed out and an in-memory queue."""
    async def sin_worker(parar):
        await parar.wait()

    monkeypatch.setattr("app.main.worker.bucle", sin_worker)
    monkeypatch.setattr("app.main.agenda.bucle", sin_worker)
    monkeypatch.setattr("app.main.db", base)

    async def sin_hoja():
        return None

    monkeypatch.setattr("app.main.hato.asegurar_estructura", sin_hoja)

    from app.main import app

    with TestClient(app) as c:
        c.base = base
        yield c


def _evento(**cambios) -> dict:
    datos = {
        "id": "true_573001112233@c.us_ABC",
        "from": "573001112233@c.us",
        "chatId": "573001112233@c.us",
        "senderPhone": "573001112233",
        "body": "477 327",
        "type": "text",
        "fromMe": False,
        "isGroup": False,
        "kind": "individual",
    }
    datos.update(cambios)
    return {"event": "message.received", "sessionId": "pericos", "data": datos}


def _enviar(cliente, evento: dict, secreto: str | None = None):
    crudo = json.dumps(evento).encode()
    firma = hmac.new(
        (secreto or cfg.webhook_secret).encode(), crudo, hashlib.sha256
    ).hexdigest()
    return cliente.post(
        "/webhook/openwa",
        content=crudo,
        headers={"X-OpenWA-Signature": f"sha256={firma}", "Content-Type": "application/json"},
    )


def test_health(cliente):
    r = cliente.get("/health")
    assert r.status_code == 200
    assert r.json()["estado"] == "ok"


def test_mensaje_valido_se_encola(cliente):
    assert cliente.get("/health").json()["cola"] == {}

    r = _enviar(cliente, _evento())
    assert r.status_code == 202

    # El endpoint sólo encola: el mensaje queda pendiente para el worker.
    assert cliente.get("/health").json()["cola"] == {"pendiente": 1}


def test_firma_invalida_da_401(cliente):
    r = _enviar(cliente, _evento(), secreto="secreto-equivocado")
    assert r.status_code == 401


def test_sin_firma_da_401(cliente):
    r = cliente.post("/webhook/openwa", json=_evento())
    assert r.status_code == 401


def test_remitente_no_autorizado_se_ignora(cliente):
    r = _enviar(cliente, _evento(senderPhone="573009999999", **{"from": "573009999999@c.us"}))
    assert r.status_code == 204


def test_mensaje_propio_se_ignora(cliente):
    assert _enviar(cliente, _evento(fromMe=True)).status_code == 204


def test_grupo_se_ignora(cliente):
    assert _enviar(cliente, _evento(isGroup=True)).status_code == 204


def test_estado_se_ignora(cliente):
    assert _enviar(cliente, _evento(kind="status")).status_code == 204


def test_otro_evento_se_ignora(cliente):
    evento = _evento()
    evento["event"] = "message.ack"
    assert _enviar(cliente, evento).status_code == 204


def test_cuerpo_no_json_da_400(cliente):
    crudo = b"esto no es json"
    firma = hmac.new(cfg.webhook_secret.encode(), crudo, hashlib.sha256).hexdigest()
    r = cliente.post(
        "/webhook/openwa",
        content=crudo,
        headers={"X-OpenWA-Signature": f"sha256={firma}"},
    )
    assert r.status_code == 400


def test_reentrega_del_mismo_mensaje_sigue_dando_202(cliente):
    """OpenWA reintenta; la segunda vez no debe crear otro pesaje."""
    assert _enviar(cliente, _evento()).status_code == 202
    assert _enviar(cliente, _evento()).status_code == 202


# --- cola de fallidos ------------------------------------------------------

def test_fallidos_requiere_api_key(cliente):
    assert cliente.get("/fallidos").status_code == 401
    assert cliente.post("/fallidos/descartar").status_code == 401


def test_fallidos_con_api_key(cliente):
    cab = {"X-API-Key": cfg.openwa_api_key}
    r = cliente.get("/fallidos", headers=cab)
    assert r.status_code == 200
    assert r.json() == {"fallidos": []}


def test_descartar_limpia_el_estado_degradado(cliente):
    """Un fallo permanente no puede dejar /health en rojo para siempre."""
    cab = {"X-API-Key": cfg.openwa_api_key}
    _enviar(cliente, _evento())

    con = cliente.base._conexion()
    con.execute("UPDATE entrantes SET estado='fallido', error='boom'")
    con.commit()

    assert cliente.get("/health").json()["estado"] == "degradado"
    assert len(cliente.get("/fallidos", headers=cab).json()["fallidos"]) == 1

    assert cliente.post("/fallidos/descartar", headers=cab).json() == {"descartados": 1}
    assert cliente.get("/health").json()["estado"] == "ok"
