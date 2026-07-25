"""Sale target, session watching, and the weekly heartbeat.

These three exist for the same reason: he weighs cattle to decide when to
sell, and he gets about twelve chances a year to trust this. A target he can
ask about, and a bot whose silence is detectable, are what make those twelve
chances count.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import agenda, reportes, sesion
from app.config import cfg

BOGOTA = ZoneInfo("America/Bogota")
NOMBRES = {"101": "Carmen", "348": "Lucía", "312": "Rosario"}


def _reg(fecha: date, vaca: str, peso: float) -> dict:
    return {"fecha": fecha, "vaca": vaca, "peso": peso, "anulado": False,
            "origen": "texto", "msg_id": "", "nota": "", "fila": 0}


# Carmen: 400 -> 460 en 60 días (1000 g/día) — le faltan 40 kg para 500
# Lucía:  480 -> 510 en 60 días — ya pasó los 500
# Rosario: 300 -> 302 en 60 días — ritmo casi nulo, no se puede proyectar
def _historial() -> list[dict]:
    hoy = date.today()
    antes = hoy - timedelta(days=60)
    return [
        _reg(antes, "101", 400), _reg(hoy, "101", 460),
        _reg(antes, "348", 480), _reg(hoy, "348", 510),
        _reg(antes, "312", 300), _reg(hoy, "312", 302),
    ]


# --- objetivo de venta ------------------------------------------------------

def test_separa_las_listas_de_las_que_faltan():
    listas, faltan = reportes.hacia_objetivo(
        reportes._marco(_historial()), NOMBRES, objetivo=500
    )
    assert [c.vaca for c in listas] == ["348"]
    assert {c.vaca for c in faltan} == {"101", "312"}


def test_proyecta_la_fecha_con_el_ritmo_de_cada_vaca():
    """40 kg a 1000 g/día son ~40 días — no un promedio del hato."""
    _, faltan = reportes.hacia_objetivo(
        reportes._marco(_historial()), NOMBRES, objetivo=500
    )
    carmen = next(c for c in faltan if c.vaca == "101")
    assert carmen.falta == 40
    assert 35 <= carmen.dias <= 45
    assert carmen.fecha is not None


def test_sin_ritmo_no_se_inventa_una_fecha():
    """Rosario casi no engorda: mejor no decir nada que decir un disparate."""
    _, faltan = reportes.hacia_objetivo(
        reportes._marco(_historial()), NOMBRES, objetivo=500
    )
    rosario = next(c for c in faltan if c.vaca == "312")
    assert rosario.fecha is None and rosario.dias is None


def test_las_de_baja_no_aparecen():
    listas, faltan = reportes.hacia_objetivo(
        reportes._marco(_historial()), NOMBRES, objetivo=500, activas={"101", "312"}
    )
    assert "348" not in [c.vaca for c in listas + faltan]


def test_texto_del_objetivo_en_espanol():
    listas, faltan = reportes.hacia_objetivo(
        reportes._marco(_historial()), NOMBRES, objetivo=500
    )
    texto = reportes.texto_objetivo(listas, faltan, 500)
    assert "Lucía" in texto and "listas" in texto.lower()
    assert "Carmen" in texto and "días" in texto


def test_sin_objetivo_no_hay_nada_que_decir():
    assert reportes.hacia_objetivo(reportes._marco([]), NOMBRES, objetivo=0) == ([], [])


def test_el_eco_menciona_lo_que_falta():
    from app import messages as M

    hoy = date.today()
    listo = M.registro_ok("348", "Lucía", 510, hoy, 480, hoy - timedelta(days=60),
                          500.0, objetivo=500)
    assert "Ya está en peso de venta" in listo

    falta = M.registro_ok("101", "Carmen", 460, hoy, 400, hoy - timedelta(days=60),
                          1000.0, objetivo=500)
    assert "Le faltan" in falta and "40" in falta

    sin = M.registro_ok("101", "Carmen", 460, hoy, 400, hoy - timedelta(days=60), 1000.0)
    assert "🎯" not in sin, "sin objetivo configurado no se menciona"


# --- vigilancia de la sesión de WhatsApp ------------------------------------

@pytest.fixture(autouse=True)
def _cablear(monkeypatch, base, openwa_falso):
    monkeypatch.setattr("app.sesion.db", base)
    monkeypatch.setattr("app.sesion.openwa", openwa_falso)
    monkeypatch.setattr("app.agenda.db", base)
    monkeypatch.setattr("app.agenda.openwa", openwa_falso)
    return base


async def test_una_caida_queda_registrada(base, openwa_falso):
    await sesion.registrar_evento("session.disconnected", {"status": "disconnected"})

    estado = await sesion.estado_actual()
    assert estado["estado"] == "disconnected"
    assert estado["conectada"] is False
    assert estado["caida_desde"] is not None


async def test_el_qr_avisa_que_hay_que_reemparejar(base, openwa_falso):
    await sesion.registrar_evento("session.qr", {})

    assert (await sesion.estado_actual())["conectada"] is False
    cuerpos = [t for _, t in openwa_falso.enviados]
    assert any("emparejar" in t for t in cuerpos)


async def test_al_volver_dice_cuanto_estuvo_caida(base, openwa_falso):
    await sesion.registrar_evento("session.disconnected", {"status": "disconnected"})
    openwa_falso.enviados.clear()

    await sesion.registrar_evento("session.status", {"status": "ready"})

    estado = await sesion.estado_actual()
    assert estado["conectada"] is True and estado["caida_desde"] is None
    cuerpos = [t for _, t in openwa_falso.enviados]
    assert any("volvió" in t for t in cuerpos)
    assert any("mandó algo mientras tanto" in t for t in cuerpos)


async def test_el_mismo_estado_no_avisa_dos_veces(base, openwa_falso):
    for _ in range(4):
        await sesion.registrar_evento("session.status", {"status": "disconnected"})
    assert len(openwa_falso.enviados) == 1


# --- latido semanal ---------------------------------------------------------

async def test_el_latido_avisa_cuando_nadie_lo_usa(base, openwa_falso, hato_falso, monkeypatch):
    monkeypatch.setattr("app.agenda.hato", hato_falso)

    assert await agenda.enviar_latido(datetime.now(BOGOTA)) is True
    _, cuerpo = openwa_falso.enviados[0]
    assert "sigue vivo" in cuerpo
    assert "Nada esta semana" in cuerpo, "el silencio es la señal que importa"


async def test_el_latido_cuenta_las_vacas_de_la_semana(
    base, openwa_falso, hato_falso, monkeypatch
):
    from app.sheets import Pesaje

    monkeypatch.setattr("app.agenda.hato", hato_falso)
    for vaca in ("477", "348"):
        await hato_falso.registrar(Pesaje(fecha=date.today(), vaca=vaca, peso=400))

    await agenda.enviar_latido(datetime.now(BOGOTA))
    _, cuerpo = openwa_falso.enviados[0]
    assert "Registró 2 vacas" in cuerpo


async def test_el_latido_va_una_vez_por_semana(base, openwa_falso, hato_falso, monkeypatch):
    monkeypatch.setattr("app.agenda.hato", hato_falso)

    lunes = datetime(2026, 7, 27, cfg.latido_hora, tzinfo=BOGOTA)   # lunes
    assert lunes.weekday() == cfg.latido_dia
    assert await agenda._toca_latido(lunes) is True

    await agenda.enviar_latido(lunes)
    assert await agenda._toca_latido(lunes) is False
    assert await agenda._toca_latido(lunes.replace(hour=cfg.latido_hora + 5)) is False

    siguiente = lunes + timedelta(days=7)
    assert await agenda._toca_latido(siguiente) is True


async def test_el_latido_se_puede_apagar(base, monkeypatch):
    monkeypatch.setattr("app.agenda.cfg", replace(cfg, latido_activo=False))
    assert await agenda._toca_latido(datetime(2026, 7, 27, 8, tzinfo=BOGOTA)) is False
