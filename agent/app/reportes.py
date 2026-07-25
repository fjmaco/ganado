"""Herd reports — the part that a notebook could never do.

**Every number in here is computed in Python.** The model's only job is to pick
which report he asked for and, at the end, add one short line of colour. It is
never handed the arithmetic and never gets to restate a figure, because a free
model will do arithmetic wrong with total confidence and this is his herd's
record. If the closing remark fails to generate, the report still goes out —
it just goes out without the flourish.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from . import messages as M
from .config import cfg
from .llm import llm

log = logging.getLogger(__name__)

PERIODOS = {"mes": 31, "3meses": 93, "6meses": 184, "ano": 366, "año": 366, "todo": None}


@dataclass
class Ganancia:
    vaca: str
    nombre: str | None
    kg: float
    gramos_dia: float
    dias: int
    desde: date
    hasta: date
    peso_inicial: float
    peso_final: float


@dataclass
class ResumenHato:
    conteo: int = 0
    total_kg: float = 0.0
    promedio_kg: float = 0.0
    mas_pesada: tuple[str, str | None, float] | None = None
    mas_liviana: tuple[str, str | None, float] | None = None
    mejor: Ganancia | None = None
    peor: Ganancia | None = None
    sin_pesar: list[tuple[str, str | None]] = field(default_factory=list)
    corte: date | None = None


# --------------------------------------------------------------------------
# Cálculo (determinista, sin modelo)
# --------------------------------------------------------------------------

def _marco(registros: list[dict]) -> pd.DataFrame:
    """Live weighings as a tidy frame, oldest first."""
    if not registros:
        return pd.DataFrame(columns=["fecha", "vaca", "peso"])
    df = pd.DataFrame([
        {"fecha": r["fecha"], "vaca": r["vaca"], "peso": float(r["peso"])}
        for r in registros
        if not r.get("anulado")
    ])
    if df.empty:
        return df
    df["fecha"] = pd.to_datetime(df["fecha"])
    return df.sort_values(["vaca", "fecha"]).reset_index(drop=True)


def _dias(periodo: str | None) -> int | None:
    if not periodo:
        return None
    return PERIODOS.get(str(periodo).strip().lower())


def _recortar(df: pd.DataFrame, periodo: str | None) -> pd.DataFrame:
    dias = _dias(periodo)
    if dias is None or df.empty:
        return df
    corte = pd.Timestamp(date.today() - timedelta(days=dias))
    return df[df["fecha"] >= corte]


def ultimos_pesos(df: pd.DataFrame) -> pd.DataFrame:
    """One row per cow: her most recent weighing."""
    if df.empty:
        return df
    return df.groupby("vaca", as_index=False).last()


def ganancias(
    df: pd.DataFrame, nombres: dict[str, str], periodo: str | None = None
) -> list[Ganancia]:
    """Gain per cow between her first and last weighing in the window.

    Cows with a single weighing have no measurable gain and are left out rather
    than reported as zero — a zero would rank them against real performers.
    """
    ventana = _recortar(df, periodo)
    if ventana.empty:
        return []

    salida: list[Ganancia] = []
    for vaca, grupo in ventana.groupby("vaca"):
        if len(grupo) < 2:
            continue
        primero, ultimo = grupo.iloc[0], grupo.iloc[-1]
        dias = (ultimo["fecha"] - primero["fecha"]).days
        if dias <= 0:
            continue
        kg = float(ultimo["peso"] - primero["peso"])
        salida.append(
            Ganancia(
                vaca=str(vaca),
                nombre=nombres.get(str(vaca)),
                kg=kg,
                gramos_dia=kg * 1000.0 / dias,
                dias=dias,
                desde=primero["fecha"].date(),
                hasta=ultimo["fecha"].date(),
                peso_inicial=float(primero["peso"]),
                peso_final=float(ultimo["peso"]),
            )
        )
    return sorted(salida, key=lambda g: g.gramos_dia, reverse=True)


def sin_pesar(
    df: pd.DataFrame, vacas: dict[str, str], desde: date | None = None
) -> list[tuple[str, str | None]]:
    """Active cows with no weighing since `desde` (default: start of this month)."""
    if desde is None:
        hoy = date.today()
        desde = hoy.replace(day=1)

    if df.empty:
        pesadas: set[str] = set()
    else:
        recientes = df[df["fecha"] >= pd.Timestamp(desde)]
        pesadas = set(recientes["vaca"].astype(str))

    return sorted(
        ((num, nom) for num, nom in vacas.items() if num not in pesadas),
        key=lambda p: (p[1] or "", p[0]),
    )


def alertas(df: pd.DataFrame, nombres: dict[str, str]) -> list[tuple[str, str | None, float]]:
    """Cows whose most recent weighing came in below the one before it."""
    if df.empty:
        return []
    salida = []
    for vaca, grupo in df.groupby("vaca"):
        if len(grupo) < 2:
            continue
        dif = float(grupo.iloc[-1]["peso"] - grupo.iloc[-2]["peso"])
        if dif < 0:
            salida.append((str(vaca), nombres.get(str(vaca)), dif))
    return sorted(salida, key=lambda t: t[2])


def solo_activas(df: pd.DataFrame, activas: set[str] | None) -> pd.DataFrame:
    """Drop retired cows from herd-wide numbers.

    A cow that died or was sold still has real history in `Registros` — that
    stays. But counting her in the herd total, or in the average, describes a
    herd that no longer exists.
    """
    if activas is None or df.empty:
        return df
    return df[df["vaca"].astype(str).isin(activas)]


def resumen(
    registros: list[dict],
    vacas: dict[str, str],
    periodo: str | None = "mes",
    activas: set[str] | None = None,
) -> ResumenHato:
    """The headline numbers for the whole herd."""
    df = solo_activas(_marco(registros), activas)
    r = ResumenHato(corte=date.today())
    if df.empty:
        return r

    ultimos = ultimos_pesos(df)
    r.conteo = int(len(ultimos))
    r.total_kg = float(ultimos["peso"].sum())
    r.promedio_kg = float(ultimos["peso"].mean())

    fila_max = ultimos.loc[ultimos["peso"].idxmax()]
    fila_min = ultimos.loc[ultimos["peso"].idxmin()]
    r.mas_pesada = (str(fila_max["vaca"]), vacas.get(str(fila_max["vaca"])), float(fila_max["peso"]))
    r.mas_liviana = (str(fila_min["vaca"]), vacas.get(str(fila_min["vaca"])), float(fila_min["peso"]))

    g = ganancias(df, vacas, periodo)
    if g:
        r.mejor, r.peor = g[0], g[-1]

    r.sin_pesar = sin_pesar(df, vacas)
    return r


@dataclass
class Camino:
    """A cow's progress toward the sale target."""
    vaca: str
    nombre: str | None
    peso: float
    falta: float
    gramos_dia: float | None = None
    dias: int | None = None
    fecha: date | None = None


def hacia_objetivo(
    df: pd.DataFrame, nombres: dict[str, str], objetivo: float,
    activas: set[str] | None = None,
) -> tuple[list[Camino], list[Camino]]:
    """Split the herd into (ready to sell, still growing).

    For the ones still growing, project a date from their own recent rate —
    not a herd average. A cow gaining 900 g/día and one gaining 200 are not
    weeks apart, they're months.
    """
    marco = solo_activas(df, activas)
    if marco.empty or objetivo <= 0:
        return [], []

    tasas = {g.vaca: g.gramos_dia for g in ganancias(marco, nombres, periodo="3meses")}
    listas: list[Camino] = []
    faltantes: list[Camino] = []

    for _, fila in ultimos_pesos(marco).iterrows():
        numero, peso = str(fila["vaca"]), float(fila["peso"])
        c = Camino(
            vaca=numero, nombre=nombres.get(numero) or None,
            peso=peso, falta=objetivo - peso,
        )
        if peso >= objetivo:
            listas.append(c)
            continue

        if (tasa := tasas.get(numero)) and tasa > 50:  # menos de 50 g/día no proyecta nada útil
            c.gramos_dia = tasa
            c.dias = int(round(c.falta * 1000 / tasa))
            if 0 < c.dias < 3650:
                c.fecha = date.today() + timedelta(days=c.dias)
        faltantes.append(c)

    listas.sort(key=lambda c: c.peso, reverse=True)
    faltantes.sort(key=lambda c: (c.dias is None, c.dias or 9999))
    return listas, faltantes


def texto_objetivo(listas: list[Camino], faltantes: list[Camino], objetivo: float) -> str:
    if not listas and not faltantes:
        return SIN_DATOS_OBJETIVO

    lineas = [f"🎯 *Peso de venta: {M.fmt_kg(objetivo)} kg*", ""]

    if listas:
        lineas.append(f"✅ *Ya están listas ({len(listas)}):*")
        lineas += [
            f"  • {M.etiqueta(c.vaca, c.nombre)} — {M.fmt_kg(c.peso)} kg "
            f"({M.fmt_delta(-c.falta)} kg de más)"
            for c in listas[:15]
        ]
        if len(listas) > 15:
            lineas.append(f"  … y {len(listas) - 15} más")
    else:
        lineas.append("Todavía ninguna llegó al peso de venta.")

    proximas = [c for c in faltantes if c.fecha][:5]
    if proximas:
        lineas += ["", "⏱️ *Las más próximas:*"]
        for c in proximas:
            lineas.append(
                f"  • {M.etiqueta(c.vaca, c.nombre)} — le faltan {M.fmt_kg(c.falta)} kg, "
                f"como en {c.dias} días (≈ {M.fecha_corta(c.fecha)})"
            )

    sin_ritmo = [c for c in faltantes if not c.fecha]
    if sin_ritmo:
        lineas += ["", f"❓ {len(sin_ritmo)} sin ritmo suficiente para estimar fecha."]

    return "\n".join(lineas)


SIN_DATOS_OBJETIVO = (
    "📭 Todavía no tengo pesajes suficientes para decirte cuáles están listas."
)


def historia(registros: list[dict], vaca: str) -> list[tuple[date, float]]:
    df = _marco(registros)
    if df.empty:
        return []
    propios = df[df["vaca"].astype(str) == str(vaca)]
    return [(f.date(), float(p)) for f, p in zip(propios["fecha"], propios["peso"])]


# --------------------------------------------------------------------------
# Redacción en español (plantillas fijas — los números nunca los toca el modelo)
# --------------------------------------------------------------------------

def _linea_vaca(numero, nombre, peso) -> str:
    return f"{M.etiqueta(numero, nombre)} con {M.fmt_kg(peso)} kg"


def texto_hato(r: ResumenHato) -> str:
    if r.conteo == 0:
        return M.SIN_DATOS

    lineas = [
        f"📊 *El hato al {M.fecha_corta(r.corte)}* — {r.conteo} "
        f"{'vaca' if r.conteo == 1 else 'vacas'}",
        "",
        f"Peso total: *{M.fmt_kg(r.total_kg)} kg*",
        f"Promedio: {M.fmt_kg(r.promedio_kg)} kg",
    ]
    if r.mas_pesada:
        lineas.append(f"Más pesada: {_linea_vaca(*r.mas_pesada)}")
    if r.mas_liviana:
        lineas.append(f"Más liviana: {_linea_vaca(*r.mas_liviana)}")

    if r.mejor and r.mejor.kg > 0:
        lineas += ["", f"🥇 Mejor engorde: *{M.etiqueta(r.mejor.vaca, r.mejor.nombre)}*, "
                       f"{M.fmt_delta(r.mejor.kg)} kg en {r.mejor.dias} días "
                       f"({M.fmt_delta(r.mejor.gramos_dia)} g/día)"]
    if r.peor and r.peor is not r.mejor:
        lineas.append(
            f"🐌 La que menos: {M.etiqueta(r.peor.vaca, r.peor.nombre)}, "
            f"{M.fmt_delta(r.peor.kg)} kg ({M.fmt_delta(r.peor.gramos_dia)} g/día)"
        )

    if r.sin_pesar:
        cuantas = len(r.sin_pesar)
        nombres = ", ".join(M.etiqueta(n, nom) for n, nom in r.sin_pesar[:6])
        extra = "" if cuantas <= 6 else f" y {cuantas - 6} más"
        lineas += ["", f"⏳ Sin pesar este mes ({cuantas}): {nombres}{extra}"]

    return "\n".join(lineas)


def texto_vaca(numero, nombre, hist: list[tuple[date, float]]) -> str:
    if not hist:
        return M.sin_datos_vaca(numero, nombre)

    fecha_ult, peso_ult = hist[-1]
    lineas = [f"📈 *{M.etiqueta(numero, nombre)}*", "",
              f"Último pesaje: *{M.fmt_kg(peso_ult)} kg* el {M.fecha_corta(fecha_ult)}"]

    if len(hist) >= 2:
        fecha_ant, peso_ant = hist[-2]
        dias = max((fecha_ult - fecha_ant).days, 1)
        dif = peso_ult - peso_ant
        lineas.append(
            f"Antes: {M.fmt_kg(peso_ant)} kg el {M.fecha_corta(fecha_ant)} "
            f"({M.fmt_delta(dif)} kg · {M.fmt_delta(dif * 1000 / dias)} g/día)"
        )

        fecha_ini, peso_ini = hist[0]
        total_dias = max((fecha_ult - fecha_ini).days, 1)
        total = peso_ult - peso_ini
        if len(hist) > 2:
            lineas += ["", f"Desde el {M.fecha_corta(fecha_ini)} lleva "
                           f"*{M.fmt_delta(total)} kg* en {total_dias} días "
                           f"({M.fmt_delta(total * 1000 / total_dias)} g/día)."]

        lineas += ["", "Historial:"]
        for f, p in hist[-6:]:
            lineas.append(f"  • {M.fecha_corta(f)} — {M.fmt_kg(p)} kg")
    else:
        lineas.append("Es su primer pesaje, todavía no puedo compararla.")

    return "\n".join(lineas)


def texto_ranking(gs: list[Ganancia], mejor: bool = True, tope: int = 5) -> str:
    if not gs:
        return ("📭 Todavía no tengo suficientes pesajes para comparar. "
                "Necesito al menos dos pesadas de la misma vaca.")

    orden = gs if mejor else list(reversed(gs))
    titulo = "🥇 *Las que más han engordado*" if mejor else "🐌 *Las que menos han engordado*"
    lineas = [titulo, ""]
    for i, g in enumerate(orden[:tope], start=1):
        lineas.append(
            f"{i}. {M.etiqueta(g.vaca, g.nombre)} — {M.fmt_delta(g.kg)} kg "
            f"en {g.dias} días ({M.fmt_delta(g.gramos_dia)} g/día)"
        )
    return "\n".join(lineas)


def texto_sin_pesar(faltantes: list[tuple[str, str | None]]) -> str:
    if not faltantes:
        return "✅ Ya pesaste todas las vacas este mes. ¡Vas al día!"
    lineas = [f"⏳ *Faltan por pesar este mes: {len(faltantes)}*", ""]
    lineas += [f"  • {M.etiqueta(n, nom)}" for n, nom in faltantes[:25]]
    if len(faltantes) > 25:
        lineas.append(f"  … y {len(faltantes) - 25} más")
    return "\n".join(lineas)


def texto_alertas(items: list[tuple[str, str | None, float]]) -> str:
    if not items:
        return "✅ Ninguna vaca bajó de peso en el último pesaje. Todo bien."
    lineas = ["⚠️ *Vacas que bajaron de peso*", ""]
    for numero, nombre, dif in items[:15]:
        lineas.append(f"  • {M.etiqueta(numero, nombre)} — {M.fmt_delta(dif)} kg")
    lineas += ["", "Vale la pena revisarlas."]
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# El toque humano (opcional, nunca toca los números)
# --------------------------------------------------------------------------

_SISTEMA_COMENTARIO = """\
Eres el asistente de un ganadero colombiano. Te paso un reporte ya calculado.
Escribe UNA sola frase corta en español (máximo 20 palabras), cálida y natural,
como un comentario final del reporte.

Reglas estrictas:
- NO repitas cifras ni inventes números nuevos.
- NO saludes ni te despidas.
- Habla de tú, en español coloquial de Colombia.
- Si el reporte trae malas noticias, sé alentador pero honesto.
Responde solo con la frase, sin comillas.
"""


async def comentario(reporte: str) -> str:
    """One warm closing sentence. Best-effort: silence is an acceptable result."""
    try:
        frase = await llm.chat(
            cfg.tier_narrar,
            _SISTEMA_COMENTARIO,
            reporte[:1500],
            temperatura=0.7,
            max_tokens=60,
        )
    except Exception as e:  # noqa: BLE001 — the report matters, the flourish doesn't
        log.info("sin comentario del modelo: %s", e)
        return ""

    frase = " ".join((frase or "").strip().split())
    # Any digit means it started restating figures — drop it rather than risk
    # a number that disagrees with the one computed above.
    if not frase or any(c.isdigit() for c in frase) or len(frase) > 200:
        return ""
    return frase.strip('"')
