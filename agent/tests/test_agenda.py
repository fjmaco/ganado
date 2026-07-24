"""The unprompted monthly summary.

The thing that must never happen is sending it twice — he'd think the herd
report changed when it didn't — so the once-a-month guard is what's tested
hardest here.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import agenda
from app.config import cfg

BOGOTA = ZoneInfo("America/Bogota")


@pytest.fixture(autouse=True)
def _cablear(monkeypatch, hato_falso, base, openwa_falso):
    monkeypatch.setattr("app.agenda.hato", hato_falso)
    monkeypatch.setattr("app.agenda.db", base)
    monkeypatch.setattr("app.agenda.openwa", openwa_falso)

    async def sin_comentario(*a, **k):
        return ""

    monkeypatch.setattr("app.reportes.comentario", sin_comentario)
    return hato_falso


async def _con_pesajes(hato_falso):
    from datetime import date

    from app.sheets import Pesaje

    await hato_falso.registrar(Pesaje(fecha=date.today(), vaca="477", peso=327))
    await hato_falso.registrar(Pesaje(fecha=date.today(), vaca="348", peso=512))


async def test_toca_el_dia_y_la_hora_configurados(base):
    momento = datetime(2026, 8, cfg.resumen_dia, cfg.resumen_hora, 5, tzinfo=BOGOTA)
    assert await agenda._toca_ahora(momento) is True


async def test_no_toca_antes_de_la_hora(base):
    momento = datetime(2026, 8, cfg.resumen_dia, cfg.resumen_hora - 1, tzinfo=BOGOTA)
    assert await agenda._toca_ahora(momento) is False


async def test_no_toca_otro_dia(base):
    momento = datetime(2026, 8, cfg.resumen_dia + 15, cfg.resumen_hora, tzinfo=BOGOTA)
    assert await agenda._toca_ahora(momento) is False


async def test_no_se_manda_dos_veces_el_mismo_mes(base, hato_falso, openwa_falso):
    await _con_pesajes(hato_falso)
    momento = datetime(2026, 8, cfg.resumen_dia, cfg.resumen_hora, tzinfo=BOGOTA)

    assert await agenda._toca_ahora(momento) is True
    await agenda.enviar_resumen(momento)
    assert len(openwa_falso.enviados) == len(cfg.remitentes_permitidos)

    # Aunque el bucle despierte otra vez ese mismo día, ya no toca.
    assert await agenda._toca_ahora(momento) is False
    mas_tarde = momento.replace(hour=cfg.resumen_hora + 3)
    assert await agenda._toca_ahora(mas_tarde) is False


async def test_el_mes_siguiente_vuelve_a_tocar(base, hato_falso):
    await _con_pesajes(hato_falso)
    agosto = datetime(2026, 8, cfg.resumen_dia, cfg.resumen_hora, tzinfo=BOGOTA)
    await agenda.enviar_resumen(agosto)

    septiembre = datetime(2026, 9, cfg.resumen_dia, cfg.resumen_hora, tzinfo=BOGOTA)
    assert await agenda._toca_ahora(septiembre) is True


async def test_el_resumen_va_en_espanol_con_las_cifras(hato_falso, openwa_falso):
    await _con_pesajes(hato_falso)
    momento = datetime(2026, 8, cfg.resumen_dia, cfg.resumen_hora, tzinfo=BOGOTA)

    assert await agenda.enviar_resumen(momento) is True
    _, cuerpo = openwa_falso.enviados[0]
    assert "Resumen del mes" in cuerpo
    assert "839" in cuerpo          # 327 + 512
    assert "Carmen" in cuerpo


async def test_sin_pesajes_no_manda_nada(hato_falso, openwa_falso):
    momento = datetime(2026, 8, cfg.resumen_dia, cfg.resumen_hora, tzinfo=BOGOTA)
    assert await agenda.enviar_resumen(momento) is False
    assert openwa_falso.enviados == []


async def test_si_falla_el_envio_no_se_marca_como_hecho(monkeypatch, hato_falso, base):
    """Un WhatsApp caído no puede costarle el resumen del mes entero."""
    await _con_pesajes(hato_falso)

    async def explotar(*a, **k):
        raise RuntimeError("whatsapp caído")

    monkeypatch.setattr("app.agenda.openwa.enviar_texto", explotar)
    momento = datetime(2026, 8, cfg.resumen_dia, cfg.resumen_hora, tzinfo=BOGOTA)

    assert await agenda.enviar_resumen(momento) is False
    assert await agenda._toca_ahora(momento) is True, "debe reintentarse"


async def test_desactivado_nunca_toca(monkeypatch, base):
    from dataclasses import replace

    monkeypatch.setattr("app.agenda.cfg", replace(cfg, resumen_activo=False))
    momento = datetime(2026, 8, cfg.resumen_dia, cfg.resumen_hora, tzinfo=BOGOTA)
    assert await agenda._toca_ahora(momento) is False
