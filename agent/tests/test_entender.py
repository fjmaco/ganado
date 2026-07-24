"""The deterministic fast path, and the guards that keep it honest.

Every case here runs with the model unreachable, which is the point: these are
the phrasings that must work when the free gateway is down.
"""

from __future__ import annotations

import pytest

from app.entender import Entendido, entender, fast_path


# Messages that are unambiguously cow/weight pairs.
@pytest.mark.parametrize(
    "mensaje, esperado",
    [
        ("477 327", [("477", 327.0)]),
        ("477 327kg", [("477", 327.0)]),
        ("477-327", [("477", 327.0)]),
        ("477 / 327", [("477", 327.0)]),
        ("la vaca 477 pesa 327", [("477", 327.0)]),
        ("vaca 477 peso 327 kg", [("477", 327.0)]),
        ("hoy pesé la 477, dio 327 kilos", [("477", 327.0)]),
        ("anota la 477 en 327", [("477", 327.0)]),
        ("LA VACA 477 PESA 327", [("477", 327.0)]),
        ("477 327, 348 512", [("477", 327.0), ("348", 512.0)]),
        ("477 327 348 512", [("477", 327.0), ("348", 512.0)]),
        ("no. 477 peso 327,5", [("477", 327.5)]),
    ],
)
def test_camino_rapido_reconoce(mensaje, esperado):
    resultado = fast_path(mensaje)
    assert resultado is not None, f"debería haber reconocido: {mensaje!r}"
    assert [(r.vaca, r.peso) for r in resultado] == esperado


# Anything with real meaning beyond a pair must go to the model instead of
# being guessed at here.
@pytest.mark.parametrize(
    "mensaje",
    [
        "¿cómo va el hato?",
        "no, eran 445",
        "borra lo último",
        "¿cuál vaca ha engordado más?",
        "Carmen pesa 430",
        "hola",
        "",
        "477",                 # sólo un número: falta el peso
        "477 327 348",         # impar: no se puede emparejar
        "477 45",              # 45 kg no es un peso creíble para una vaca
        "477 5000",            # fuera del rango superior
        "la vaca de la loma",
    ],
)
def test_camino_rapido_se_abstiene(mensaje):
    assert fast_path(mensaje) is None


def test_cero_leading_normaliza():
    """0477 y 477 son la misma vaca."""
    resultado = fast_path("0477 327")
    assert resultado is not None
    assert resultado[0].vaca == "477"


async def test_entender_usa_camino_rapido_sin_tocar_el_modelo(monkeypatch):
    """Si el regex resuelve, el gateway ni se llama."""
    async def explotar(*a, **k):
        raise AssertionError("no debió llamarse al modelo")

    monkeypatch.setattr("app.entender.llm.chat_json", explotar)

    ent = await entender("477 327", {})
    assert ent.intencion == "registrar"
    assert ent.via == "regex"
    assert ent.confianza == 1.0
    assert [(r.vaca, r.peso) for r in ent.registros] == [("477", 327.0)]


async def test_entender_degrada_si_el_modelo_falla(monkeypatch):
    """Un gateway caído produce 'otro' — nunca una excepción hacia arriba."""
    async def explotar(*a, **k):
        raise RuntimeError("gateway caído")

    monkeypatch.setattr("app.entender.llm.chat_json", explotar)

    ent = await entender("¿cómo va el hato?", {})
    assert isinstance(ent, Entendido)
    assert ent.intencion == "otro"
    assert ent.confianza == 0.0


async def test_entender_limpia_respuesta_del_modelo(monkeypatch):
    async def responder(*a, **k):
        return {
            "intencion": "registrar",
            "registros": [{"vaca": "0477", "peso_kg": "327"}],
            "confianza": 0.9,
        }

    monkeypatch.setattr("app.entender.llm.chat_json", responder)

    ent = await entender("la 477 quedó en 327 kilitos", {"477": "Carmen"})
    assert ent.intencion == "registrar"
    assert [(r.vaca, r.peso) for r in ent.registros] == [("477", 327.0)]


async def test_entender_descarta_intencion_desconocida(monkeypatch):
    """Un modelo que inventa una intención cae a 'otro', no rompe el flujo."""
    async def responder(*a, **k):
        return {"intencion": "hacer_asado", "confianza": 0.99}

    monkeypatch.setattr("app.entender.llm.chat_json", responder)

    ent = await entender("algo raro", {})
    assert ent.intencion == "otro"


async def test_entender_resuelve_vaca_por_nombre(monkeypatch):
    """Si dice el nombre y el modelo no devuelve número, lo resolvemos nosotros."""
    async def responder(*a, **k):
        return {
            "intencion": "consultar",
            "consulta": {"tipo": "vaca", "vaca": None},
            "confianza": 0.9,
        }

    monkeypatch.setattr("app.entender.llm.chat_json", responder)

    ent = await entender("¿cómo va Carmen?", {"477": "Carmen", "348": "Lucía"})
    assert ent.consulta is not None
    assert ent.consulta.vaca == "477"
