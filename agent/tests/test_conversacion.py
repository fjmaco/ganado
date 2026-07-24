"""End-to-end conversation flows, in Spanish.

These are the rules that keep bad data out of his records — an unknown cow is
asked about rather than created, a wild weight is questioned, a voice note is
read back — plus the correction paths that let him fix a mistake by just saying
so. The model is stubbed throughout: what is under test is the decision-making
around it, not the model itself.
"""

from __future__ import annotations

import pytest

from app import messages as M
from app.conversacion import atender
from app.entender import Consulta, Entendido, RegistroDetectado


@pytest.fixture(autouse=True)
def _cablear(monkeypatch, hato_falso, base):
    """Point the conversation at the fake herd and an in-memory queue."""
    monkeypatch.setattr("app.conversacion.hato", hato_falso)
    monkeypatch.setattr("app.conversacion.db", base)
    return hato_falso


CHAT = "573001112233@c.us"


def _mensaje(texto: str, msg_id: str = "m1", tipo: str = "text") -> dict:
    return {"chat_id": CHAT, "remitente": "573001112233",
            "msg_id": msg_id, "cuerpo": texto, "tipo": tipo}


def _fingir_entendimiento(monkeypatch, ent: Entendido):
    async def falso(*a, **k):
        return ent

    monkeypatch.setattr("app.conversacion.entender", falso)


# --- registro feliz --------------------------------------------------------

async def test_registra_vaca_conocida(hato_falso):
    respuesta = await atender(_mensaje("477 327"))

    assert "Carmen" in respuesta and "327" in respuesta
    registros = await hato_falso.registros()
    assert len(registros) == 1
    assert registros[0]["vaca"] == "477"
    assert registros[0]["peso"] == 327
    assert registros[0]["origen"] == "texto"


async def test_el_eco_muestra_el_cambio_desde_la_ultima_vez(hato_falso):
    await atender(_mensaje("477 300", msg_id="m1"))
    respuesta = await atender(_mensaje("477 330", msg_id="m2"))

    assert "Anterior" in respuesta
    assert "300" in respuesta
    assert "+30" in respuesta


async def test_registra_varias_vacas_de_un_mensaje(hato_falso):
    respuesta = await atender(_mensaje("477 327, 348 512"))

    assert "Carmen" in respuesta and "Lucía" in respuesta
    registros = await hato_falso.registros()
    assert {r["vaca"] for r in registros} == {"477", "348"}
    # Cada fila conserva un msg_id propio aunque vinieran en el mismo mensaje.
    assert len({r["msg_id"] for r in registros}) == 2


# --- vaca desconocida ------------------------------------------------------

async def test_vaca_desconocida_pregunta_y_no_escribe(hato_falso):
    respuesta = await atender(_mensaje("34 450"))

    assert "34" in respuesta and "SÍ" in respuesta
    assert await hato_falso.registros() == [], "no debió escribir nada todavía"


async def test_si_crea_la_vaca_con_nombre_y_la_registra(hato_falso):
    await atender(_mensaje("34 450", msg_id="m1"))
    respuesta = await atender(_mensaje("sí", msg_id="m2"))

    vacas = await hato_falso.vacas()
    assert "34" in vacas
    nombre = vacas["34"].nombre
    assert nombre and nombre not in {"Carmen", "Lucía", "Rosario"}
    assert nombre in respuesta

    registros = await hato_falso.registros()
    assert len(registros) == 1 and registros[0]["peso"] == 450


async def test_no_cancela_y_no_crea_nada(hato_falso):
    await atender(_mensaje("34 450", msg_id="m1"))
    respuesta = await atender(_mensaje("no", msg_id="m2"))

    assert respuesta == M.CANCELADO
    assert "34" not in await hato_falso.vacas()
    assert await hato_falso.registros() == []


async def test_corregir_el_numero_en_vez_de_confirmar(hato_falso):
    """Escribió 34 por error: al mandar el número bueno se anota ese, no el 34."""
    await atender(_mensaje("34 450", msg_id="m1"))
    respuesta = await atender(_mensaje("477 450", msg_id="m2"))

    assert "Carmen" in respuesta
    assert "34" not in await hato_falso.vacas()
    registros = await hato_falso.registros()
    assert len(registros) == 1 and registros[0]["vaca"] == "477"


# --- pesos raros -----------------------------------------------------------

async def test_peso_fuera_de_rango_se_rechaza(monkeypatch, hato_falso):
    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="registrar", confianza=1.0,
        registros=[RegistroDetectado(vaca="477", peso=12.0)],
    ))
    respuesta = await atender(_mensaje("477 12"))

    assert "no me cuadra" in respuesta
    assert await hato_falso.registros() == []


async def test_salto_grande_pregunta_antes_de_escribir(hato_falso):
    await atender(_mensaje("477 300", msg_id="m1"))
    respuesta = await atender(_mensaje("477 500", msg_id="m2"))

    assert "cambio grande" in respuesta or "¿" in respuesta
    assert len(await hato_falso.registros()) == 1, "el segundo aún no debe estar"


async def test_confirmar_el_salto_lo_escribe(hato_falso):
    await atender(_mensaje("477 300", msg_id="m1"))
    await atender(_mensaje("477 500", msg_id="m2"))
    respuesta = await atender(_mensaje("sí", msg_id="m3"))

    registros = await hato_falso.registros()
    assert len(registros) == 2
    assert registros[-1]["peso"] == 500
    assert "Carmen" in respuesta


# --- correcciones ----------------------------------------------------------

async def test_corrige_el_ultimo_peso(monkeypatch, hato_falso):
    await atender(_mensaje("477 327", msg_id="m1"))

    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="corregir", peso_correccion=445.0, confianza=0.95,
    ))
    respuesta = await atender(_mensaje("no, eran 445", msg_id="m2"))

    assert "445" in respuesta
    registros = await hato_falso.registros()
    assert len(registros) == 1, "corregir actualiza la fila, no agrega otra"
    assert registros[0]["peso"] == 445


async def test_borra_el_ultimo_pesaje(monkeypatch, hato_falso):
    await atender(_mensaje("477 327", msg_id="m1"))

    _fingir_entendimiento(monkeypatch, Entendido(intencion="borrar", confianza=0.95))
    respuesta = await atender(_mensaje("borra eso", msg_id="m2"))

    assert "Borré" in respuesta or "borr" in respuesta.lower()
    assert await hato_falso.registros() == [], "ya no debe contar"
    # Pero la fila sigue ahí, marcada — el historial no se destruye.
    todos = await hato_falso.registros(incluir_anulados=True)
    assert len(todos) == 1 and todos[0]["anulado"] is True


async def test_corregir_sin_nada_previo(monkeypatch, hato_falso):
    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="corregir", peso_correccion=445.0, confianza=0.9,
    ))
    assert await atender(_mensaje("no, eran 445")) == M.NADA_QUE_CORREGIR


# --- notas de voz ----------------------------------------------------------

async def test_nota_de_voz_se_lee_de_vuelta_antes_de_escribir(monkeypatch, hato_falso):
    async def transcribir(chat_id, msg_id):
        return "la vaca cuatrocientos setenta y siete pesa trescientos veintisiete"

    monkeypatch.setattr("app.conversacion.transcribir_nota", transcribir)
    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="registrar", confianza=0.9,
        registros=[RegistroDetectado(vaca="477", peso=327.0)],
    ))

    respuesta = await atender(_mensaje("", msg_id="v1", tipo="voice"))

    assert "Escuché" in respuesta and "Carmen" in respuesta
    assert await hato_falso.registros() == [], "la voz se confirma antes de escribir"


async def test_confirmar_la_voz_la_escribe_con_origen_voz(monkeypatch, hato_falso):
    async def transcribir(chat_id, msg_id):
        return "la 477 pesa 327"

    monkeypatch.setattr("app.conversacion.transcribir_nota", transcribir)
    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="registrar", confianza=0.9,
        registros=[RegistroDetectado(vaca="477", peso=327.0)],
    ))
    await atender(_mensaje("", msg_id="v1", tipo="voice"))
    await atender(_mensaje("sí", msg_id="m2"))

    registros = await hato_falso.registros()
    assert len(registros) == 1
    assert registros[0]["origen"] == "voz"
    # La transcripción cruda queda guardada para poder auditar un número mal oído.
    assert "477" in registros[0]["nota"]


async def test_si_falla_la_transcripcion_pide_texto(monkeypatch, hato_falso):
    from app.transcribe import ErrorTranscripcion

    async def explotar(chat_id, msg_id):
        raise ErrorTranscripcion("whisper caído")

    monkeypatch.setattr("app.conversacion.transcribir_nota", explotar)
    respuesta = await atender(_mensaje("", msg_id="v1", tipo="voice"))

    assert respuesta == M.ERROR_TRANSCRIBIENDO
    assert await hato_falso.registros() == []


# --- consultas (v2) --------------------------------------------------------

async def test_consulta_del_hato(monkeypatch, hato_falso):
    monkeypatch.setattr("app.reportes.comentario", lambda *a, **k: _vacio())
    await atender(_mensaje("477 327", msg_id="m1"))
    await atender(_mensaje("348 512", msg_id="m2"))

    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="consultar", consulta=Consulta(tipo="hato"), confianza=0.9,
    ))
    respuesta = await atender(_mensaje("¿cómo va el hato?", msg_id="m3"))

    assert "Peso total" in respuesta
    assert "839" in respuesta          # 327 + 512, calculado en Python
    assert "Carmen" in respuesta


async def test_consulta_de_una_vaca(monkeypatch, hato_falso):
    monkeypatch.setattr("app.reportes.comentario", lambda *a, **k: _vacio())
    await atender(_mensaje("477 327", msg_id="m1"))

    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="consultar", consulta=Consulta(tipo="vaca", vaca="477"), confianza=0.9,
    ))
    respuesta = await atender(_mensaje("¿cómo va Carmen?", msg_id="m2"))

    assert "Carmen" in respuesta and "327" in respuesta


async def test_consulta_sin_datos(monkeypatch, hato_falso):
    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="consultar", consulta=Consulta(tipo="hato"), confianza=0.9,
    ))
    assert await atender(_mensaje("¿cómo va el hato?")) == M.SIN_DATOS


async def _vacio() -> str:
    return ""


# --- casos borde -----------------------------------------------------------

async def test_mensaje_vacio(hato_falso):
    assert await atender(_mensaje("")) == M.NO_ENTENDI


async def test_baja_confianza_pregunta_en_vez_de_adivinar(monkeypatch, hato_falso):
    """Una lectura dudosa no puede convertirse en una fila equivocada."""
    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="registrar", confianza=0.2, via="llm",
        registros=[RegistroDetectado(vaca="477", peso=327.0)],
    ))
    respuesta = await atender(_mensaje("mmm la de allá creo que 327"))

    assert respuesta == M.NO_ENTENDI
    assert await hato_falso.registros() == []


async def test_ayuda_y_saludo(monkeypatch, hato_falso):
    _fingir_entendimiento(monkeypatch, Entendido(intencion="ayuda", confianza=1.0))
    assert "anotar un peso" in await atender(_mensaje("ayuda"))

    _fingir_entendimiento(monkeypatch, Entendido(intencion="saludo", confianza=1.0))
    assert "Hola" in await atender(_mensaje("buenos días"))


async def test_renombrar_una_vaca(monkeypatch, hato_falso):
    _fingir_entendimiento(monkeypatch, Entendido(
        intencion="renombrar", vaca_referida="477", nombre_nuevo="Lucero", confianza=0.9,
    ))
    respuesta = await atender(_mensaje("la 477 se llama Lucero"))

    assert "Lucero" in respuesta
    vacas = await hato_falso.vacas()
    assert vacas["477"].nombre == "Lucero"
