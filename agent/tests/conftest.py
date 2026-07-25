"""Test fixtures.

Environment is set *before* the app package is imported: `config.cfg` is built
at import time, so a test that sets a variable afterwards would be configuring
a copy nobody reads.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

os.environ.setdefault("OPENWA_API_KEY", "test-api-key")
os.environ.setdefault("OPENWA_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("LITELLM_API_KEY", "test-llm-key")
os.environ.setdefault("SHEET_ID", "test-sheet")
os.environ.setdefault("GOOGLE_SA_JSON_B64", "{}")
os.environ.setdefault("ALLOWED_SENDERS", "573001112233,573004445566")
os.environ.setdefault("ADMIN_WHATSAPP", "573009998877")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TZ", "America/Bogota")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.db import BaseDatos  # noqa: E402
from app.sheets import Vaca  # noqa: E402


class FakeHato:
    """In-memory stand-in for the spreadsheet, with the same contract."""

    def __init__(self, vacas: dict[str, str] | None = None) -> None:
        self._vacas: dict[str, Vaca] = {
            n: Vaca(numero=n, nombre=nom) for n, nom in (vacas or {}).items()
        }
        self._registros: list[dict] = []
        self._siguiente_fila = 2
        self.nombres_asignados: list[str] = []

    async def vacas(self, refrescar: bool = False) -> dict[str, Vaca]:
        return dict(self._vacas)

    async def crear_vaca(self, numero: str, hoy: date, sexo: str = "H") -> Vaca:
        from app.nombres import asignar_nombre

        if numero in self._vacas:
            return self._vacas[numero]
        nombre = asignar_nombre({v.nombre for v in self._vacas.values() if v.nombre}, sexo)
        vaca = Vaca(numero=numero, nombre=nombre, sexo=sexo, alta=hoy.isoformat())
        self._vacas[numero] = vaca
        self.nombres_asignados.append(nombre)
        return vaca

    async def retirar_vaca(self, numero: str, motivo: str, cuando) -> bool:
        if numero not in self._vacas:
            return False
        v = self._vacas[numero]
        self._vacas[numero] = Vaca(numero=v.numero, nombre=v.nombre, alta=v.alta,
                                   activa=False, baja=cuando.isoformat(), motivo=motivo)
        return True

    async def reactivar_vaca(self, numero: str) -> bool:
        if numero not in self._vacas:
            return False
        v = self._vacas[numero]
        self._vacas[numero] = Vaca(numero=v.numero, nombre=v.nombre, alta=v.alta, activa=True)
        return True

    async def renombrar(self, numero: str, nombre: str) -> bool:
        if numero not in self._vacas:
            return False
        viejo = self._vacas[numero]
        self._vacas[numero] = Vaca(numero=numero, nombre=nombre, alta=viejo.alta)
        return True

    async def registrar(self, p) -> int:
        fila = self._siguiente_fila
        self._siguiente_fila += 1
        self._registros.append({
            "fila": fila, "fecha": p.fecha, "vaca": p.vaca, "peso": p.peso,
            "origen": p.origen, "msg_id": p.msg_id, "anulado": False, "nota": p.nota,
        })
        return fila

    async def registros(self, refrescar: bool = False, incluir_anulados: bool = False):
        if incluir_anulados:
            return list(self._registros)
        return [r for r in self._registros if not r["anulado"]]

    async def ultimo_pesaje(self, numero: str):
        propios = [r for r in self._registros if r["vaca"] == numero and not r["anulado"]]
        if not propios:
            return None
        return max(propios, key=lambda r: (r["fecha"], r["fila"]))

    async def pesaje_del_dia(self, numero: str, dia):
        delhoy = [r for r in self._registros
                  if r["vaca"] == numero and r["fecha"] == dia and not r["anulado"]]
        return max(delhoy, key=lambda r: r["fila"]) if delhoy else None

    async def pesaje_anterior_a(self, numero: str, dia):
        previos = [r for r in self._registros
                   if r["vaca"] == numero and r["fecha"] < dia and not r["anulado"]]
        if not previos:
            return None
        return max(previos, key=lambda r: (r["fecha"], r["fila"]))

    async def actualizar_peso(self, fila: int, peso: float) -> None:
        for r in self._registros:
            if r["fila"] == fila:
                r["peso"] = peso

    async def anular(self, fila: int) -> None:
        for r in self._registros:
            if r["fila"] == fila:
                r["anulado"] = True


class FakeOpenWA:
    def __init__(self) -> None:
        self.enviados: list[tuple[str, str]] = []

    async def enviar_texto(self, chat_id: str, texto: str) -> str:
        self.enviados.append((chat_id, texto))
        return f"msg-{len(self.enviados)}"

    async def marcar_escribiendo(self, chat_id: str) -> None:
        return None


@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    """No test may reach the real gateway.

    One of these quietly started calling llm.lamhara.co for real and only
    surfaced as a 400 in the logs. A suite that depends on a free API being up
    isn't testing the code, it's testing the weather — so the default is a
    hard failure, and any test that wants model behaviour stubs it explicitly.
    """
    async def prohibido(*a, **k):
        raise AssertionError(
            "una prueba intentó llamar al gateway de verdad; "
            "hay que simular el modelo (monkeypatch de llm.chat / llm.chat_json)"
        )

    monkeypatch.setattr("app.llm.llm.chat", prohibido)
    monkeypatch.setattr("app.llm.llm.chat_json", prohibido)
    monkeypatch.setattr("app.llm.llm.transcribir", prohibido)


@pytest.fixture
def hato_falso() -> FakeHato:
    return FakeHato({"477": "Carmen", "348": "Lucía", "312": "Rosario"})


@pytest.fixture
def base() -> BaseDatos:
    """A throwaway in-memory queue.

    Sync on purpose: the TestClient runs the app in its own event loop, and a
    fixture created inside pytest's loop would bind its lock to the wrong one.
    """
    bd = BaseDatos(":memory:")
    yield bd
    bd.cerrar()


@pytest.fixture
def openwa_falso() -> FakeOpenWA:
    return FakeOpenWA()
