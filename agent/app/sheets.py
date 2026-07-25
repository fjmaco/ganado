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
from datetime import date, datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import ValueRenderOption
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

# Read raw values, never what the cell displays.
#
# gspread's default reads FORMATTED text and then "numericises" it assuming US
# conventions. On this sheet (locale es_ES) a weight of 394.7 kg displays as
# "394,7" and comes back as the integer **3947** — silently inflating every
# reading tenfold. The stored data was always correct; only the reads were
# wrong, which is the worst kind of wrong: the spreadsheet looks right while
# every report and every "peso anterior" is nonsense.
#
# Unformatted values are locale-independent: numbers arrive as numbers and
# dates as serials.
SIN_FORMATO = ValueRenderOption.unformatted

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


# Google Sheets counts days from 1899-12-30 (the Lotus 1-2-3 epoch it inherited).
EPOCA_SHEETS = date(1899, 12, 30)


def _a_fecha(v) -> date | None:
    """Parse a date from a serial number, a date object, or a string.

    Reads use UNFORMATTED_VALUE, so dates arrive as serial numbers — which is
    the point: a serial means the same day in every locale, while '05/07/2026'
    means two different days depending on who is reading it.
    """
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return EPOCA_SHEETS + timedelta(days=int(v))
        except (OverflowError, ValueError):
            return None

    s = str(v or "").strip()
    if not s:
        return None
    # Only reached for text cells someone typed by hand. ISO first: it is the
    # one format that isn't ambiguous between day-first and month-first.
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _a_float(v) -> float | None:
    """Read a weight. Numbers pass straight through; text is parsed defensively."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)

    s = str(v).strip()
    # Hand-typed text, so the separators could be either convention. If both
    # appear, the last one is the decimal point ("1.234,5" vs "1,234.5").
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") \
            else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
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
        """Build credentials from GOOGLE_SA_JSON_B64.

        The value is a ~3.1 kB single line pasted by hand into a web form, so
        it arrives damaged in predictable ways: wrapped across lines, missing
        its `=` padding, or truncated. The first two are repaired here; the
        third can't be, so the error reports the length it actually got —
        which makes a truncated paste obvious instantly instead of looking
        like a malformed key.
        """
        bruto = cfg.google_sa_b64
        if not bruto.strip():
            raise ErrorSheets("GOOGLE_SA_JSON_B64 está vacío.")

        # A raw JSON blob is also accepted, for convenience when debugging.
        if bruto.lstrip().startswith("{"):
            try:
                return Credentials.from_service_account_info(
                    json.loads(bruto), scopes=list(ALCANCES)
                )
            except (json.JSONDecodeError, ValueError) as e:
                raise ErrorSheets(f"GOOGLE_SA_JSON_B64 parece JSON pero no es válido: {e}") from e

        limpio = "".join(bruto.split())          # quita saltos de línea y espacios
        limpio += "=" * (-len(limpio) % 4)       # repone el relleno que se haya perdido

        try:
            texto = base64.b64decode(limpio).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as e:
            raise ErrorSheets(
                f"GOOGLE_SA_JSON_B64 no se pudo decodificar de base64 "
                f"(recibí {len(limpio)} caracteres). Suele ser un pegado incompleto: "
                f"la llave completa son ~3100. Vuelve a pegar la salida de "
                f"`base64 -w0 <archivo>.json` en una sola línea."
            ) from e

        try:
            datos = json.loads(texto)
        except json.JSONDecodeError as e:
            raise ErrorSheets(
                f"GOOGLE_SA_JSON_B64 decodificó pero no es JSON completo "
                f"({len(limpio)} caracteres de base64 → {len(texto)} de texto). "
                f"Casi seguro quedó cortado al pegarlo: la llave completa son ~3100 "
                f"caracteres de base64."
            ) from e

        faltan = {"client_email", "private_key", "token_uri"} - set(datos)
        if faltan:
            raise ErrorSheets(
                f"Al JSON de la service account le faltan campos: {', '.join(sorted(faltan))}."
            )

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

    @staticmethod
    def _formulas_tabla(sep: str) -> tuple[str, str]:
        """The `Tabla` formulas, built with a given argument separator."""
        reg, vac = cfg.hoja_registros, cfg.hoja_vacas
        pivote = (
            f'=IFERROR(QUERY(FILTER({reg}!A2:C{sep}{reg}!B2:B<>""{sep}'
            f'{reg}!F2:F<>TRUE){sep}'
            f'"select Col2, max(Col3) group by Col2 pivot Col1 label Col2 \'vaca\'"'
            f'{sep}0){sep}"Aún no hay pesajes")'
        )
        nombres = (
            f'=ARRAYFORMULA(IF(B2:B=""{sep}""{sep}'
            f'IFERROR(VLOOKUP(B2:B{sep}{vac}!A:B{sep}2{sep}FALSE){sep}"")))'
        )
        return pivote, nombres

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
        #
        # The argument separator is locale-dependent — ',' in en_US, ';' in
        # es_ES and most of Europe — and getting it wrong is a *parse* error,
        # which renders as a bare #ERROR! that IFERROR cannot catch. Rather
        # than guess from `libro.locale` (a long and drifting list of which
        # locales use which), write it, read back what the cell actually
        # rendered, and switch separators if it failed. Empirical and correct
        # in every locale, including ones that don't exist yet.
        exito = False
        for sep in (",", ";"):
            pivote, nombres = self._formulas_tabla(sep)
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
            if "#ERROR!" not in (tabla.acell("B1").value or ""):
                log.info("fórmulas de 'Tabla' escritas con separador %r", sep)
                exito = True
                break
            log.info("separador %r no sirve en este locale; probando el otro", sep)

        if not exito:
            log.error(
                "no se pudieron escribir las fórmulas de 'Tabla' con ',' ni ';'. "
                "Los datos en 'Registros' están bien; sólo la vista ancha queda vacía."
            )

        # Cabecera y las dos primeras columnas fijas: con una fecha por
        # jornada, nombre y número tienen que seguir a la vista al desplazarse
        # a la derecha. Al hacerlo el contenido pasa por debajo de esas dos
        # columnas, que puede parecer que hay celdas escondidas — no las hay,
        # y `_desocultar` de abajo se asegura de que siga siendo cierto.
        tabla.freeze(rows=1, cols=2)
        self._desocultar(tabla)

    @staticmethod
    def _desocultar(hoja: gspread.Worksheet) -> None:
        """Force every row and column visible.

        Belt and braces: nothing here hides anything, but a stray hidden row
        in this tab would silently drop a cow from the view her owner uses to
        check on her, and that is not a thing to leave to chance.
        """
        hoja.spreadsheet.batch_update({
            "requests": [
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": hoja.id, "dimension": eje},
                        "properties": {"hiddenByUser": False},
                        "fields": "hiddenByUser",
                    }
                }
                for eje in ("ROWS", "COLUMNS")
            ]
        })

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
        for fila in hoja.get_all_records(
            expected_headers=CAB_VACAS, value_render_option=SIN_FORMATO
        ):
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
            hoja.get_all_records(
                expected_headers=CAB_REGISTROS, value_render_option=SIN_FORMATO
            ),
            start=2,
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
