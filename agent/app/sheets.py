"""Google Sheets access — the herd's permanent record.

The sheet is the source of truth, and it is deliberately shaped so the bot only
ever *appends*. Every write is one API call with no read-modify-write, so a
retry after a timeout, or two people weighing at the same time, cannot clobber
somebody else's cell. The wide grid your dad would recognise is produced by a
formula on a separate tab, not by the bot reaching in and placing cells.

Deletion never deletes: it flips `anulado` to TRUE, so a mistaken entry leaves a
trace and the pivot simply stops counting it.

gspread is synchronous, so every call is pushed to a worker thread — a blocked
event loop would stall the webhook that feeds it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import gspread
from google.oauth2.service_account import Credentials
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import cfg
from .nombres import asignar_nombre
from .texto import normalizar

log = logging.getLogger(__name__)

ALCANCES = ("https://www.googleapis.com/auth/spreadsheets",)

CAB_REGISTROS = ["fecha", "vaca", "peso_kg", "origen", "msg_id", "anulado", "nota"]
CAB_VACAS = ["vaca", "nombre", "alta", "activa"]

# 1-indexed columns in Registros, used for targeted single-cell updates.
COL_PESO = 3
COL_ANULADO = 6

_RANGO = re.compile(r"!([A-Z]+)(\d+)")


class ErrorSheets(RuntimeError):
    """A Sheets operation failed after every retry."""


def _es_transitorio(e: BaseException) -> bool:
    """Rate limits and server errors are worth retrying; a bad key never is.

    Retrying a 403 (sheet not shared with the service account) would just burn
    30 seconds before reporting the same misconfiguration, so it fails fast.
    """
    if isinstance(e, gspread.exceptions.APIError):
        codigo = getattr(getattr(e, "response", None), "status_code", None)
        return codigo in {429, 500, 502, 503, 504}
    return isinstance(e, (TimeoutError, ConnectionError))


_reintento = retry(
    retry=retry_if_exception(_es_transitorio),
    stop=stop_after_attempt(4),
    wait=wait_exponential_jitter(initial=1, max=30),
    reraise=True,
)


@dataclass(frozen=True)
class Vaca:
    numero: str
    nombre: str | None
    alta: str = ""
    activa: bool = True


@dataclass(frozen=True)
class Pesaje:
    fecha: date
    vaca: str
    peso: float
    origen: str = ""
    msg_id: str = ""
    nota: str = ""


def numero_canonico(v: str | int | float | None) -> str:
    """Normalise a cow number so 0347, 347 and 347.0 are the same cow."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    if re.fullmatch(r"\d+(\.0+)?", s):
        return str(int(float(s)))
    return s.upper()


def _a_fecha(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _a_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _verdadero(v) -> bool:
    return str(v).strip().upper() in {"TRUE", "VERDADERO", "SI", "SÍ", "1", "X"}


class RepositorioHato:
    """All reads and writes against the herd spreadsheet."""

    def __init__(self, ttl_cache: float = 60.0) -> None:
        self._libro: gspread.Spreadsheet | None = None
        self._ttl = ttl_cache
        self._cache_vacas: tuple[float, dict[str, Vaca]] | None = None
        self._cache_reg: tuple[float, list[dict]] | None = None
        self._lock = asyncio.Lock()

    # -- conexión ---------------------------------------------------------

    def _credenciales(self) -> Credentials:
        crudo = cfg.google_sa_b64.strip()
        try:
            # Accept both base64 and a pasted raw JSON blob.
            datos = json.loads(crudo) if crudo.startswith("{") else json.loads(
                base64.b64decode(crudo).decode("utf-8")
            )
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ErrorSheets(
                "GOOGLE_SA_JSON_B64 no es un JSON de service account válido "
                "(ni en base64 ni en texto plano)."
            ) from e
        return Credentials.from_service_account_info(datos, scopes=list(ALCANCES))

    def _abrir(self) -> gspread.Spreadsheet:
        if self._libro is None:
            cliente = gspread.authorize(self._credenciales())
            self._libro = cliente.open_by_key(cfg.sheet_id)
        return self._libro

    def _hoja(self, titulo: str, cabecera: list[str]) -> gspread.Worksheet:
        libro = self._abrir()
        try:
            return libro.worksheet(titulo)
        except gspread.exceptions.WorksheetNotFound:
            hoja = libro.add_worksheet(title=titulo, rows=1000, cols=max(len(cabecera), 8))
            hoja.update(values=[cabecera], range_name="A1")
            hoja.freeze(rows=1)
            return hoja

    # -- estructura -------------------------------------------------------

    @_reintento
    def _asegurar_estructura_sync(self) -> None:
        libro = self._abrir()
        self._hoja(cfg.hoja_registros, CAB_REGISTROS)
        self._hoja(cfg.hoja_vacas, CAB_VACAS)

        try:
            tabla = libro.worksheet("Tabla")
        except gspread.exceptions.WorksheetNotFound:
            tabla = libro.add_worksheet(title="Tabla", rows=200, cols=40)

        # The wide grid: one row per cow, one column per weighing date. Written
        # once as formulas so it stays live without the bot ever touching it.
        reg, vac = cfg.hoja_registros, cfg.hoja_vacas
        pivote = (
            f'=IFERROR(QUERY(FILTER({reg}!A2:C, {reg}!B2:B<>"", {reg}!F2:F<>TRUE),'
            f'"select Col2, max(Col3) group by Col2 pivot Col1 label Col2 \'vaca\'",0),'
            f'"Aún no hay pesajes")'
        )
        nombres = (
            f'=ARRAYFORMULA(IF(B2:B="","",IFERROR(VLOOKUP(B2:B,{vac}!A:B,2,FALSE),"")))'
        )
        tabla.update(
            values=[["nombre"], [nombres]],
            range_name="A1:A2",
            value_input_option="USER_ENTERED",
        )
        tabla.update(
            values=[[pivote]],
            range_name="B1",
            value_input_option="USER_ENTERED",
        )
        tabla.freeze(rows=1, cols=2)

    async def asegurar_estructura(self) -> None:
        await asyncio.to_thread(self._asegurar_estructura_sync)
        self.invalidar()

    def invalidar(self) -> None:
        self._cache_vacas = None
        self._cache_reg = None

    # -- vacas ------------------------------------------------------------

    @_reintento
    def _leer_vacas_sync(self) -> dict[str, Vaca]:
        hoja = self._hoja(cfg.hoja_vacas, CAB_VACAS)
        vacas: dict[str, Vaca] = {}
        for fila in hoja.get_all_records(expected_headers=CAB_VACAS):
            numero = numero_canonico(fila.get("vaca"))
            if not numero:
                continue
            vacas[numero] = Vaca(
                numero=numero,
                nombre=(str(fila.get("nombre") or "").strip() or None),
                alta=str(fila.get("alta") or ""),
                activa=str(fila.get("activa") or "TRUE").strip().upper()
                not in {"FALSE", "NO", "0"},
            )
        return vacas

    async def vacas(self, refrescar: bool = False) -> dict[str, Vaca]:
        async with self._lock:
            ahora = time.monotonic()
            if not refrescar and self._cache_vacas and ahora - self._cache_vacas[0] < self._ttl:
                return self._cache_vacas[1]
            vacas = await asyncio.to_thread(self._leer_vacas_sync)
            self._cache_vacas = (ahora, vacas)
            return vacas

    async def nombres_por_numero(self) -> dict[str, str]:
        return {n: v.nombre for n, v in (await self.vacas()).items() if v.nombre}

    @_reintento
    def _crear_vaca_sync(self, numero: str, nombre: str, alta: str) -> None:
        hoja = self._hoja(cfg.hoja_vacas, CAB_VACAS)
        hoja.append_row(
            [numero, nombre, alta, "TRUE"],
            value_input_option="USER_ENTERED",
            table_range="A1",
        )

    async def crear_vaca(self, numero: str, hoy: date) -> Vaca:
        """Register a new cow and give her a name nobody else has."""
        numero = numero_canonico(numero)
        vacas = await self.vacas(refrescar=True)
        if numero in vacas:
            return vacas[numero]

        nombre = asignar_nombre({v.nombre for v in vacas.values() if v.nombre})
        await asyncio.to_thread(self._crear_vaca_sync, numero, nombre, hoy.isoformat())
        self._cache_vacas = None
        return Vaca(numero=numero, nombre=nombre, alta=hoy.isoformat())

    @_reintento
    def _renombrar_sync(self, numero: str, nombre: str) -> bool:
        hoja = self._hoja(cfg.hoja_vacas, CAB_VACAS)
        columna = hoja.col_values(1)
        for i, valor in enumerate(columna[1:], start=2):
            if numero_canonico(valor) == numero:
                hoja.update_cell(i, 2, nombre)
                return True
        return False

    async def renombrar(self, numero: str, nombre: str) -> bool:
        ok = await asyncio.to_thread(self._renombrar_sync, numero_canonico(numero), nombre)
        self._cache_vacas = None
        return ok

    # -- registros --------------------------------------------------------

    @_reintento
    def _registrar_sync(self, p: Pesaje) -> int:
        hoja = self._hoja(cfg.hoja_registros, CAB_REGISTROS)
        respuesta = hoja.append_row(
            [
                p.fecha.isoformat(),
                p.vaca,
                p.peso,
                p.origen,
                p.msg_id,
                "FALSE",
                p.nota,
            ],
            value_input_option="USER_ENTERED",
            insert_data_option="INSERT_ROWS",
            table_range="A1",
        )
        # append_row reports where it landed — that row number is what makes a
        # later correction a targeted single-cell write.
        rango = (respuesta.get("updates", {}) or {}).get("updatedRange", "")
        if m := _RANGO.search(rango):
            return int(m.group(2))
        return -1

    async def registrar(self, p: Pesaje) -> int:
        fila = await asyncio.to_thread(self._registrar_sync, p)
        self._cache_reg = None
        return fila

    @_reintento
    def _actualizar_peso_sync(self, fila: int, peso: float) -> None:
        hoja = self._hoja(cfg.hoja_registros, CAB_REGISTROS)
        hoja.update_cell(fila, COL_PESO, peso)

    async def actualizar_peso(self, fila: int, peso: float) -> None:
        await asyncio.to_thread(self._actualizar_peso_sync, fila, peso)
        self._cache_reg = None

    @_reintento
    def _anular_sync(self, fila: int) -> None:
        hoja = self._hoja(cfg.hoja_registros, CAB_REGISTROS)
        hoja.update_cell(fila, COL_ANULADO, "TRUE")

    async def anular(self, fila: int) -> None:
        """Cancel an entry without erasing it — history stays auditable."""
        await asyncio.to_thread(self._anular_sync, fila)
        self._cache_reg = None

    @_reintento
    def _leer_registros_sync(self) -> list[dict]:
        hoja = self._hoja(cfg.hoja_registros, CAB_REGISTROS)
        filas = []
        for i, cruda in enumerate(
            hoja.get_all_records(expected_headers=CAB_REGISTROS), start=2
        ):
            numero = numero_canonico(cruda.get("vaca"))
            peso = _a_float(cruda.get("peso_kg"))
            fecha = _a_fecha(cruda.get("fecha"))
            if not numero or peso is None or fecha is None:
                continue
            filas.append(
                {
                    "fila": i,
                    "fecha": fecha,
                    "vaca": numero,
                    "peso": peso,
                    "origen": str(cruda.get("origen") or ""),
                    "msg_id": str(cruda.get("msg_id") or ""),
                    "anulado": _verdadero(cruda.get("anulado")),
                    "nota": str(cruda.get("nota") or ""),
                }
            )
        return filas

    async def registros(self, refrescar: bool = False, incluir_anulados: bool = False) -> list[dict]:
        async with self._lock:
            ahora = time.monotonic()
            if refrescar or not self._cache_reg or ahora - self._cache_reg[0] >= self._ttl:
                datos = await asyncio.to_thread(self._leer_registros_sync)
                self._cache_reg = (ahora, datos)
            datos = self._cache_reg[1]
        return datos if incluir_anulados else [r for r in datos if not r["anulado"]]

    async def ultimo_pesaje(self, numero: str) -> dict | None:
        """Most recent live weighing for a cow, for the delta in the echo."""
        numero = numero_canonico(numero)
        propios = [r for r in await self.registros() if r["vaca"] == numero]
        if not propios:
            return None
        return max(propios, key=lambda r: (r["fecha"], r["fila"]))

    async def existe_msg_id(self, msg_id: str) -> bool:
        """Second line of defence against a retried webhook double-logging."""
        if not msg_id:
            return False
        return any(r["msg_id"] == msg_id for r in await self.registros(incluir_anulados=True))


hato = RepositorioHato()
