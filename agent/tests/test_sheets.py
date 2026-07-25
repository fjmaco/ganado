"""Credential parsing and value normalisation.

The service-account key reaches production as a ~3.1 kB single line pasted by
hand into a web form. It arrives damaged in predictable ways, and the damage
that *can* be repaired should be, while the damage that can't must produce an
error that names the actual problem — a truncated paste and a corrupt key look
identical otherwise, and one of them wasted a real debugging session.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

import app.sheets as S
from app.sheets import ErrorSheets, RepositorioHato, numero_canonico

LLAVE = {
    "type": "service_account",
    "project_id": "pericos",
    "private_key_id": "abc123",
    # Clave de juguete, sólo para que el parser tenga algo con la forma correcta.
    "private_key": "-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\n",
    "client_email": "pericos@pericos.iam.gserviceaccount.com",
    "client_id": "1",
    "token_uri": "https://oauth2.googleapis.com/token",
}
BUENO = base64.b64encode(json.dumps(LLAVE).encode()).decode()


@pytest.fixture
def repo(monkeypatch):
    return RepositorioHato()


def _con_valor(monkeypatch, valor: str) -> None:
    monkeypatch.setattr(S, "cfg", replace(S.cfg, google_sa_b64=valor))


def _parsea(repo, monkeypatch, valor: str) -> dict:
    """Corre el parser hasta justo antes de construir las Credentials."""
    _con_valor(monkeypatch, valor)
    capturado = {}

    class FakeCreds:
        @staticmethod
        def from_service_account_info(datos, scopes=None):
            capturado.update(datos)
            return "credenciales"

    monkeypatch.setattr(S, "Credentials", FakeCreds)
    repo._credenciales()
    return capturado


# --- daños que sí se pueden reparar ---------------------------------------

def test_base64_limpio(repo, monkeypatch):
    assert _parsea(repo, monkeypatch, BUENO)["client_email"] == LLAVE["client_email"]


def test_base64_partido_en_lineas(repo, monkeypatch):
    """Pegar en un textarea puede meter saltos de línea: se ignoran."""
    partido = "\n".join(BUENO[i : i + 76] for i in range(0, len(BUENO), 76))
    assert _parsea(repo, monkeypatch, partido)["client_email"] == LLAVE["client_email"]


def test_base64_sin_relleno(repo, monkeypatch):
    """Si se pierden los '=' del final, se reponen."""
    assert _parsea(repo, monkeypatch, BUENO.rstrip("="))["client_email"] == LLAVE["client_email"]


def test_base64_con_espacios(repo, monkeypatch):
    assert _parsea(repo, monkeypatch, f"  {BUENO}  ")["client_email"] == LLAVE["client_email"]


def test_json_en_texto_plano(repo, monkeypatch):
    """También se acepta el JSON tal cual, útil al depurar."""
    assert _parsea(repo, monkeypatch, json.dumps(LLAVE))["client_email"] == LLAVE["client_email"]


# --- daños que no se pueden reparar: el error tiene que decir cuál es -------

def test_truncado_dice_que_esta_cortado(repo, monkeypatch):
    _con_valor(monkeypatch, BUENO[: len(BUENO) // 2])
    with pytest.raises(ErrorSheets) as e:
        repo._credenciales()
    mensaje = str(e.value)
    assert "cortado" in mensaje or "completo" in mensaje
    assert "caracteres" in mensaje, "debe reportar el largo, que es lo que delata el corte"


def test_vacio(repo, monkeypatch):
    _con_valor(monkeypatch, "")
    with pytest.raises(ErrorSheets, match="vacío"):
        repo._credenciales()


def test_no_es_base64(repo, monkeypatch):
    _con_valor(monkeypatch, "esto no es base64 ni de lejos !!!")
    with pytest.raises(ErrorSheets):
        repo._credenciales()


def test_json_valido_pero_sin_campos(repo, monkeypatch):
    """Un JSON que no es una service account se nombra como tal."""
    incompleto = base64.b64encode(json.dumps({"type": "service_account"}).encode()).decode()
    _con_valor(monkeypatch, incompleto)
    with pytest.raises(ErrorSheets, match="faltan campos"):
        repo._credenciales()


# --- lectura de valores: nada puede depender del locale ---------------------

@pytest.mark.parametrize(
    "entrada, esperado",
    [
        (394.7, 394.7),        # número real: pasa derecho
        (327, 327.0),
        (0, 0.0),
        ("394,7", 394.7),      # texto escrito a mano, coma decimal (es_ES)
        ("394.7", 394.7),      # texto con punto decimal (en_US)
        ("1.234,5", 1234.5),   # miles con punto, decimal con coma
        ("1,234.5", 1234.5),   # al revés
        ("  327  ", 327.0),
        ("", None),
        (None, None),
        ("no es un peso", None),
    ],
)
def test_a_float_es_independiente_del_locale(entrada, esperado):
    assert S._a_float(entrada) == esperado


def test_a_float_no_infla_decimales():
    """El bug que corrompió los reportes: 394,7 kg leído como 3947 kg.

    gspread numericiza el texto *formateado* asumiendo convenciones de EE.UU.,
    así que en una hoja es_ES la coma decimal se tomaba como separador de
    miles. Los datos guardados siempre estuvieron bien; sólo las lecturas
    salían diez veces más grandes.
    """
    assert S._a_float("394,7") == 394.7
    assert S._a_float("394,7") != 3947


@pytest.mark.parametrize(
    "serial, esperado",
    [
        (46047, __import__("datetime").date(2026, 1, 25)),
        (46227, __import__("datetime").date(2026, 7, 24)),
        (1, __import__("datetime").date(1899, 12, 31)),
    ],
)
def test_fecha_desde_serial(serial, esperado):
    """Las lecturas sin formato traen las fechas como serial de Sheets."""
    assert S._a_fecha(serial) == esperado


def test_fecha_desde_iso():
    import datetime as dt

    assert S._a_fecha("2026-07-24") == dt.date(2026, 7, 24)
    assert S._a_fecha(dt.date(2026, 7, 24)) == dt.date(2026, 7, 24)
    assert S._a_fecha(dt.datetime(2026, 7, 24, 10, 30)) == dt.date(2026, 7, 24)


def test_fecha_ambigua_prefiere_dia_primero():
    """05/07/2026 en Colombia es 5 de julio, no 7 de mayo."""
    import datetime as dt

    assert S._a_fecha("05/07/2026") == dt.date(2026, 7, 5)


def test_fecha_invalida():
    assert S._a_fecha("") is None
    assert S._a_fecha(None) is None
    assert S._a_fecha("cualquier cosa") is None


# --- normalización de números de vaca --------------------------------------

@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("477", "477"),
        ("0477", "477"),
        (477, "477"),
        (477.0, "477"),
        ("477.0", "477"),
        ("  477  ", "477"),
        ("a47", "A47"),
        ("", ""),
        (None, ""),
    ],
)
def test_numero_canonico(entrada, esperado):
    assert numero_canonico(entrada) == esperado
