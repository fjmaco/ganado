"""Webhook authentication and the sender allowlist."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace

from app.config import cfg
from app.security import firma_valida, remitente_permitido


def _firmar(cuerpo: bytes, secreto: str | None = None) -> str:
    clave = (secreto or cfg.webhook_secret).encode()
    return hmac.new(clave, cuerpo, hashlib.sha256).hexdigest()


CUERPO = b'{"event":"message.received","data":{"id":"m1"}}'


def test_firma_correcta_pasa():
    assert firma_valida(CUERPO, f"sha256={_firmar(CUERPO)}") is True


def test_acepta_firma_sin_prefijo():
    assert firma_valida(CUERPO, _firmar(CUERPO)) is True


def test_firma_de_otro_secreto_se_rechaza():
    assert firma_valida(CUERPO, f"sha256={_firmar(CUERPO, 'otro-secreto')}") is False


def test_firma_sobre_otro_cuerpo_se_rechaza():
    """La firma se calcula sobre los bytes exactos: un byte distinto la invalida."""
    assert firma_valida(CUERPO + b" ", f"sha256={_firmar(CUERPO)}") is False


def test_sin_cabecera_se_rechaza():
    assert firma_valida(CUERPO, None) is False
    assert firma_valida(CUERPO, "") is False


def test_firma_basura_se_rechaza():
    assert firma_valida(CUERPO, "sha256=no-es-hex") is False


def test_sin_secreto_configurado_se_rechaza_todo(monkeypatch):
    """Un endpoint sin firmar que escribe en sus registros es peor que uno caído."""
    monkeypatch.setattr("app.security.cfg", replace(cfg, webhook_secret=""))
    assert firma_valida(CUERPO, f"sha256={_firmar(CUERPO)}") is False


# --- lista de remitentes ---------------------------------------------------

def test_remitente_de_la_lista_pasa():
    assert remitente_permitido("573001112233") is True
    assert remitente_permitido("573004445566") is True


def test_remitente_con_formato_distinto_pasa():
    """El mismo número escrito con + o con espacios sigue siendo el mismo."""
    assert remitente_permitido("+57 300 111 2233") is True


def test_desconocido_se_rechaza():
    assert remitente_permitido("573009999999") is False
    assert remitente_permitido("") is False


def test_sin_lista_no_pasa_nadie(monkeypatch):
    monkeypatch.setattr("app.security.cfg", replace(cfg, remitentes_permitidos=[]))
    assert remitente_permitido("573001112233") is False
