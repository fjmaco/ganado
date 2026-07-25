#!/usr/bin/env python3
"""Datos de prueba para ejercitar el bot antes de soltarlo.

    python -m scripts.sembrar --sembrar    # 30 vacas × 7 pesajes
    python -m scripts.sembrar --resumen    # qué hay en la hoja ahora
    python -m scripts.sembrar --limpiar    # borra SÓLO lo sembrado

**El borrado es quirúrgico, no un vaciado.** Cada fila sembrada lleva
`origen=prueba` y cada vaca sembrada sale de una lista fija de números, así que
`--limpiar` puede quitar exactamente eso y nada más. Para cuando toque limpiar,
es perfectamente posible que mi papá ya haya anotado pesajes de verdad — un
`borrar todo` se los llevaría por delante.

Escribe en lote (`append_rows`) en vez de usar `hato.registrar()`: son 210
filas, y una llamada por fila agotaría la cuota de escritura de Sheets
(~60/minuto). El bot escribe de a una porque así una reentrega no pisa nada;
aquí no hay concurrencia que proteger.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import cfg  # noqa: E402
from app.nombres import asignar_nombre  # noqa: E402
from app.sheets import CAB_REGISTROS, CAB_VACAS, hato  # noqa: E402

ORIGEN = "prueba"

# Números fijos: son la llave para poder deshacer exactamente esto.
NUMEROS = [
    "101", "104", "107", "112", "118", "123", "127", "134", "141", "146",
    "152", "158", "163", "169", "174", "180", "187", "193", "201", "208",
    "215", "222", "229", "236", "244", "251", "259", "266", "274", "281",
]

PESAJES = 7            # ~7 meses de historia
INTERVALO_DIAS = 30


def _historia(rng: random.Random, i: int, hoy: date) -> list[tuple[date, float]]:
    """Una curva de engorde creíble, con casos que hagan interesantes los reportes."""
    inicial = rng.uniform(255, 400)

    # Perfiles: la mayoría engorda normal; unos pocos existen para que
    # "peor ganancia" y las alertas de pérdida tengan de qué hablar.
    if i == 0:
        mensual, ruido = 31.0, 2.0      # la estrella
    elif i in (7, 18):
        mensual, ruido = 1.5, 3.0       # estancadas
    elif i in (11, 25):
        mensual, ruido = -3.5, 2.5      # adelgazando
    else:
        mensual, ruido = rng.uniform(13, 25), 4.0

    # Todas las vacas se pesan en todas las jornadas: así es como lo hace mi
    # papá, baja el ganado entero a la báscula de una vez. Por eso las fechas
    # de una misma jornada son iguales para todo el hato — el reporte de
    # "faltan por pesar" queda vacío a propósito, que es el estado correcto.
    salida: list[tuple[date, float]] = []
    peso = inicial
    for n in range(PESAJES):
        cuando = hoy - timedelta(days=(PESAJES - 1 - n) * INTERVALO_DIAS)
        if n:
            peso += mensual + rng.gauss(0, ruido)
        peso = max(120.0, min(peso, 900.0))
        salida.append((cuando, round(peso, 1)))
    return salida


async def sembrar() -> int:
    rng = random.Random(20260724)     # fijo: repetible
    hoy = date.today()

    existentes = await hato.vacas(refrescar=True)
    ya = [n for n in NUMEROS if n in existentes]
    if ya:
        print(f"⚠️  Ya existen {len(ya)} de las vacas de prueba ({', '.join(ya[:5])}…).")
        print("   Corre --limpiar primero si quieres sembrar de cero.")
        return 1

    usados = {v.nombre for v in existentes.values() if v.nombre}
    filas_vacas, filas_reg = [], []

    for i, numero in enumerate(NUMEROS):
        nombre = asignar_nombre(usados)
        usados.add(nombre)
        hist = _historia(rng, i, hoy)
        filas_vacas.append([numero, nombre, hist[0][0].isoformat(), "TRUE"])
        for n, (cuando, peso) in enumerate(hist):
            filas_reg.append(
                [cuando.isoformat(), numero, peso, ORIGEN, f"seed-{numero}-{n}", "FALSE", ""]
            )

    libro = hato._abrir()
    libro.worksheet(cfg.hoja_vacas).append_rows(
        filas_vacas, value_input_option="USER_ENTERED", table_range="A1"
    )
    libro.worksheet(cfg.hoja_registros).append_rows(
        filas_reg, value_input_option="USER_ENTERED", table_range="A1"
    )
    hato.invalidar()

    print(f"✅ Sembradas {len(filas_vacas)} vacas y {len(filas_reg)} pesajes.")
    print(f"   Nombres: {', '.join(f[1] for f in filas_vacas[:8])}…")
    print("   Todas las filas van con origen='prueba' para poder deshacerlo.")
    return 0


def _borrar_filas(hoja, filas: list[int]) -> int:
    """Borra filas (1-indexadas) agrupando las consecutivas en un solo llamado.

    Una llamada por fila agota la cuota de escritura de Sheets (~60/minuto) en
    cuanto hay más de un puñado — con 210 filas sembradas, revienta siempre.
    Como lo sembrado queda contiguo, agrupar lo deja en uno o dos llamados.
    De abajo hacia arriba, porque borrar corre hacia arriba lo que está debajo.
    """
    if not filas:
        return 0

    rangos: list[tuple[int, int]] = []
    for f in sorted(filas):
        if rangos and f == rangos[-1][1] + 1:
            rangos[-1] = (rangos[-1][0], f)
        else:
            rangos.append((f, f))

    for inicio, fin in reversed(rangos):
        hoja.delete_rows(inicio, fin)
    return sum(fin - inicio + 1 for inicio, fin in rangos)


async def limpiar() -> int:
    """Quita sólo lo sembrado: filas con origen='prueba' y las vacas de la lista."""
    libro = hato._abrir()

    hoja_reg = libro.worksheet(cfg.hoja_registros)
    col_origen = CAB_REGISTROS.index("origen")
    filas_reg = [
        i + 1
        for i, fila in enumerate(hoja_reg.get_all_values())
        if i > 0 and len(fila) > col_origen and fila[col_origen] == ORIGEN
    ]
    borradas = _borrar_filas(hoja_reg, filas_reg)

    hoja_vac = libro.worksheet(cfg.hoja_vacas)
    col_vaca = CAB_VACAS.index("vaca")
    filas_vac = [
        i + 1
        for i, fila in enumerate(hoja_vac.get_all_values())
        if i > 0 and len(fila) > col_vaca and fila[col_vaca] in NUMEROS
    ]
    borradas_v = _borrar_filas(hoja_vac, filas_vac)

    hato.invalidar()
    print(f"🧹 Borrados {borradas} pesajes de prueba y {borradas_v} vacas de prueba.")
    print("   Lo que no era 'prueba' quedó intacto.")
    return 0


async def resumen() -> int:
    vacas = await hato.vacas(refrescar=True)
    registros = await hato.registros(refrescar=True)
    de_prueba = [r for r in registros if r["origen"] == ORIGEN]

    print(f"Vacas    : {len(vacas)}")
    print(f"Pesajes  : {len(registros)}  (de prueba: {len(de_prueba)})")
    if registros:
        fechas = sorted({r['fecha'] for r in registros})
        print(f"Rango    : {fechas[0]} → {fechas[-1]}")
        ultimos = {}
        for r in sorted(registros, key=lambda x: x["fecha"]):
            ultimos[r["vaca"]] = r["peso"]
        total = sum(ultimos.values())
        print(f"Peso total del hato: {total:,.0f} kg  ·  promedio {total/len(ultimos):,.0f} kg")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Datos de prueba para Pericos")
    p.add_argument("--sembrar", action="store_true", help="crear 30 vacas × 7 pesajes")
    p.add_argument("--limpiar", action="store_true", help="borrar SÓLO lo sembrado")
    p.add_argument("--resumen", action="store_true", help="qué hay en la hoja")
    args = p.parse_args()

    async def correr() -> int:
        if args.limpiar:
            return await limpiar()
        if args.sembrar:
            return await sembrar()
        if args.resumen:
            return await resumen()
        p.print_help()
        return 0

    return asyncio.run(correr())


if __name__ == "__main__":
    raise SystemExit(main())
