"""The deterministic fast path, and the guards that keep it honest.

Every case here runs with the model unreachable, which is the point: these are
the phrasings that must work when the free gateway is down.
"""

from __future__ import annotations

import pytest

from app.entender import (
    Entendido,
    entender,
    fast_path,
    fast_path_baja,
    fast_path_consulta,
)


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

    # Una frase que de verdad necesita al modelo: las preguntas de siempre ya
    # las resuelve el camino rápido y ni lo consultan.
    ent = await entender("oye y la de la loma en cuánto quedó", {})
    assert isinstance(ent, Entendido)
    assert ent.intencion == "otro"
    assert ent.confianza == 0.0


async def test_las_preguntas_de_siempre_sobreviven_al_gateway_caido(monkeypatch):
    """Lo que él pregunta a diario tiene que funcionar con el gateway abajo."""
    async def explotar(*a, **k):
        raise RuntimeError("gateway caído")

    monkeypatch.setattr("app.entender.llm.chat_json", explotar)

    for frase in ("como va el ganado", "cuales faltan por pesar", "477 327"):
        ent = await entender(frase, {"477": "Lucía"})
        assert ent.intencion in {"consultar", "registrar"}, frase
        assert ent.via == "regex", frase


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


# --- preguntas de siempre: sin modelo, y sin importar las tildes ------------

VACAS_PRUEBA = {"477": "Lucía", "101": "Carmen", "251": "Elisa"}


@pytest.mark.parametrize(
    "mensaje, tipo",
    [
        # El mismo hato dicho de todas las formas en que él lo diría.
        ("Como va el ganado", "hato"),
        ("Como va el hato", "hato"),
        ("cómo va el hato", "hato"),
        ("COMO VA EL GANADO?", "hato"),
        ("¿cómo están las vacas?", "hato"),
        ("Cual es el peso total de mi ganado hoy", "hato"),
        ("cuanto pesa el hato", "hato"),
        ("resumen", "hato"),
        # Rankings
        ("cual vaca ha engordado mas", "mejor_ganancia"),
        ("¿cuál engordó más?", "mejor_ganancia"),
        ("cual engordo menos", "peor_ganancia"),
        ("cual es la peor del hato en engorde", "peor_ganancia"),
        # Pendientes y alertas
        ("cuales faltan por pesar", "sin_pesar"),
        ("que vacas estan sin pesar", "sin_pesar"),
        ("alguna bajo de peso", "alertas"),
        ("¿alguna perdió peso?", "alertas"),
        ("cuantas vacas tengo", "conteo"),
    ],
)
def test_preguntas_frecuentes_sin_modelo(mensaje, tipo):
    ent = fast_path_consulta(mensaje, VACAS_PRUEBA)
    assert ent is not None, f"debería reconocerse sin modelo: {mensaje!r}"
    assert ent.intencion == "consultar"
    assert ent.consulta.tipo == tipo
    assert ent.via == "regex"


def test_tildes_no_cambian_nada():
    """'cómo va el hato' y 'como va el ganado' son la misma pregunta."""
    a = fast_path_consulta("cómo va el hato", VACAS_PRUEBA)
    b = fast_path_consulta("como va el ganado", VACAS_PRUEBA)
    assert a.consulta.tipo == b.consulta.tipo == "hato"


@pytest.mark.parametrize(
    "mensaje, numero",
    [
        ("como va Carmen", "101"),
        ("¿cómo va Carmen?", "101"),
        ("como va lucia", "477"),
        ("cuanto pesa la 477", "477"),
        ("historial de Elisa", "251"),
    ],
)
def test_pregunta_por_una_vaca(mensaje, numero):
    ent = fast_path_consulta(mensaje, VACAS_PRUEBA)
    assert ent is not None and ent.consulta.tipo == "vaca"
    assert ent.consulta.vaca == numero


def test_saludo_y_ayuda():
    assert fast_path_consulta("hola", VACAS_PRUEBA).intencion == "saludo"
    assert fast_path_consulta("buenos días", VACAS_PRUEBA).intencion == "saludo"
    assert fast_path_consulta("ayuda", VACAS_PRUEBA).intencion == "ayuda"


@pytest.mark.parametrize(
    "mensaje",
    ["no, eran 445", "borra lo ultimo", "la 477 se llama Lucero", "gracias"],
)
def test_lo_que_no_es_pregunta_va_al_modelo(mensaje):
    assert fast_path_consulta(mensaje, VACAS_PRUEBA) is None


async def test_un_peso_le_gana_a_la_pregunta(monkeypatch):
    """'477 327' es un pesaje, no una consulta, aunque traiga números."""
    async def explotar(*a, **k):
        raise AssertionError("no debió llamarse al modelo")

    monkeypatch.setattr("app.entender.llm.chat_json", explotar)
    ent = await entender("477 327", VACAS_PRUEBA)
    assert ent.intencion == "registrar"


async def test_las_preguntas_no_llaman_al_modelo(monkeypatch):
    async def explotar(*a, **k):
        raise AssertionError("no debió llamarse al modelo")

    monkeypatch.setattr("app.entender.llm.chat_json", explotar)
    ent = await entender("Como va el ganado", VACAS_PRUEBA)
    assert ent.intencion == "consultar"
    assert ent.consulta.tipo == "hato"
    assert ent.via == "regex"


# --- bajas del hato ---------------------------------------------------------

@pytest.mark.parametrize(
    "mensaje, numero, motivo",
    [
        ("se murio la 477", "477", "muerte"),
        ("se me murió Carmen", "101", "muerte"),
        ("amaneció muerta la 101", "101", "muerte"),
        ("vendí la 477", "477", "venta"),
        ("se vendió Carmen", "101", "venta"),
        ("sacrifiqué la 251", "251", "sacrificio"),
        ("me robaron la 477", "477", "robo"),
        ("da de baja la 101", "101", "otro"),
        ("quita la 477", "477", "otro"),
    ],
)
def test_baja_reconoce_vaca_y_motivo(mensaje, numero, motivo):
    ent = fast_path_baja(mensaje, VACAS_PRUEBA)
    assert ent is not None and ent.intencion == "retirar"
    assert ent.vaca_referida == numero
    assert ent.motivo_baja == motivo


def test_baja_sin_decir_cual_vaca():
    """'Se me murió una vaca' se entiende, aunque falte saber cuál."""
    ent = fast_path_baja("Se me murió una vaca", VACAS_PRUEBA)
    assert ent is not None and ent.intencion == "retirar"
    assert ent.motivo_baja == "muerte"
    assert ent.vaca_referida is None


@pytest.mark.parametrize(
    "mensaje", ["revive a Carmen", "reactiva la 477", "no se murió, era otra"]
)
def test_reactivar(mensaje):
    ent = fast_path_baja(mensaje, VACAS_PRUEBA)
    assert ent is not None and ent.intencion == "reactivar"


@pytest.mark.parametrize("mensaje", ["477 327", "como va el hato", "no, eran 445"])
def test_lo_que_no_es_baja(mensaje):
    assert fast_path_baja(mensaje, VACAS_PRUEBA) is None


async def test_una_baja_no_llama_al_modelo(monkeypatch):
    async def explotar(*a, **k):
        raise AssertionError("no debió llamarse al modelo")

    monkeypatch.setattr("app.entender.llm.chat_json", explotar)
    ent = await entender("se murió la 477", VACAS_PRUEBA)
    assert ent.intencion == "retirar" and ent.via == "regex"
