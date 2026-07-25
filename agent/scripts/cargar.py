#!/usr/bin/env python3
"""Load a real weighing session from pasted text.

    python -m scripts.cargar --fecha 2026-06-20 --archivo jornada.txt
    cat jornada.txt | python -m scripts.cargar --fecha 2026-06-20
    python -m scripts.cargar --fecha 2026-06-20 --archivo j.txt --ensayo

Built for the way the numbers actually arrive: a WhatsApp conversation pasted
straight in, timestamps and all. It reads `309 peso 417`, `Ternero peso 126`,
and `[9:46 p.m., 24/7/2026] Fulana: 136 peso 425` the same way.

**Idempotent by (cow, date).** Loading the same session twice corrects the
weights instead of doubling them, which matters because the dates here are
provisional — re-running with the right date is the expected workflow, not an
accident.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import cfg  # noqa: E402
from app.nombres import HEMBRA, MACHO, asignar_nombre, sexo_mencionado  # noqa: E402
from app.sheets import Pesaje, hato, numero_canonico  # noqa: E402
from app.texto import normalizar  # noqa: E402

# Quita el prefijo de WhatsApp: "[9:46 p.m., 24/7/2026] Fulana: "
_PREFIJO = re.compile(r"^\s*\[[^\]]*\]\s*[^:]{0,40}:\s*")
# "309 peso 417", "309 417", "309 - 417", "309 peso 417 peso"
_LINEA = re.compile(
    r"^\s*(?P<id>[A-Za-zÁÉÍÓÚÑáéíóúñ0-9]+)\s*(?:peso|pesa|de|-|:)?\s*"
    r"(?P<peso>\d+(?:[.,]\d+)?)\s*(?:kg|kilos?|peso)?\s*$",
    re.IGNORECASE,
)


def parsear(texto: str, id_ternero: str) -> tuple[list[tuple[str, float, str]], list[str]]:
    """Return ([(numero, peso, sexo)], [lineas que no se entendieron])."""
    entradas: list[tuple[str, float, str]] = []
    rechazos: list[str] = []

    for cruda in texto.splitlines():
        linea = _PREFIJO.sub("", cruda).strip()
        if not linea:
            continue

        m = _LINEA.match(linea)
        if not m:
            rechazos.append(cruda.strip())
            continue

        bruto, peso = m.group("id"), float(m.group("peso").replace(",", "."))

        # Un animal sin caravana, nombrado por lo que es ("Ternero").
        if not bruto.isdigit():
            sexo = sexo_mencionado(bruto) or MACHO
            numero = id_ternero
        else:
            sexo = HEMBRA
            numero = numero_canonico(bruto)

        entradas.append((numero, peso, sexo))

    return entradas, rechazos


def _ordenar(cabecera: list[str], datos: dict) -> list:
    """Values in the sheet's actual column order."""
    return [datos.get(col, "") for col in cabecera]


async def cargar(entradas, cuando: date, ensayo: bool) -> int:
    vacas = await hato.vacas(refrescar=True)
    registros = await hato.registros(refrescar=True)
    ya = {(r["vaca"], r["fecha"]): r for r in registros}

    usados = {v.nombre for v in vacas.values() if v.nombre}
    nuevas, actualizadas, agregadas = [], [], []

    for numero, peso, sexo in entradas:
        if numero not in vacas:
            nombre = asignar_nombre(usados, sexo)
            usados.add(nombre)
            nuevas.append((numero, nombre, sexo, peso))
        else:
            nombre = vacas[numero].nombre
            if (numero, cuando) in ya:
                actualizadas.append((numero, nombre, peso))
            else:
                agregadas.append((numero, nombre, peso))

    print(f"Jornada del {cuando}: {len(entradas)} animales")
    print(f"  vacas nuevas       : {len(nuevas)}")
    print(f"  pesajes nuevos     : {len(agregadas)}")
    print(f"  pesajes corregidos : {len(actualizadas)}")
    if nuevas:
        print()
        for numero, nombre, sexo, peso in nuevas:
            marca = " ♂" if sexo == MACHO else ""
            print(f"   {numero:>6}  →  {nombre}{marca}   {peso:g} kg")

    if ensayo:
        print("\n(ensayo: no se escribió nada)")
        return 0

    print()
    libro = hato._abrir()

    # En lote, no de a uno.
    #
    # hato.crear_vaca() relee el hato entero cada vez (correcto cuando nace un
    # animal suelto por WhatsApp), pero aquí son 20 seguidos: ~60 llamadas, y
    # la cuota de Sheets son 60 por minuto. La primera corrida se cortó a la
    # mitad justo por esto.
    # Siempre por NOMBRE de columna, nunca por posición: la hoja puede tener
    # columnas migradas al final y no coincidir con el orden canónico.
    if nuevas:
        hoja_v = libro.worksheet(cfg.hoja_vacas)
        # La cabecera se lee UNA vez: fila_para() la relee en cada llamada, y
        # dentro de un bucle de 20 eso son 20 lecturas de más — suficiente para
        # volver a chocar contra la cuota, que es lo que pasó.
        cab_v = hoja_v.row_values(1)
        hoja_v.append_rows(
            [_ordenar(cab_v, {
                "vaca": numero, "nombre": nombre, "sexo": sexo,
                "alta": cuando.isoformat(), "activa": "TRUE",
                "baja": "", "motivo": "",
            }) for numero, nombre, sexo, _peso in nuevas],
            value_input_option="USER_ENTERED", table_range="A1",
        )

    pesajes = [(n, p) for n, _nom, _sx, p in nuevas] + [
        (n, p) for n, _nom, p in agregadas
    ]
    if pesajes:
        hoja_r = libro.worksheet(cfg.hoja_registros)
        cab_r = hoja_r.row_values(1)
        hoja_r.append_rows(
            [_ordenar(cab_r, {
                "fecha": cuando.isoformat(), "vaca": numero, "peso_kg": peso,
                "origen": "carga", "msg_id": f"carga-{cuando}-{numero}",
                "anulado": "FALSE", "nota": "",
            }) for numero, peso in pesajes],
            value_input_option="USER_ENTERED", table_range="A1",
        )

    for numero, _nombre, peso in actualizadas:
        # Ya había un pesaje de esa vaca ese día: se corrige, no se duplica.
        await hato.actualizar_peso(int(ya[(numero, cuando)]["fila"]), peso)

    hato.invalidar()

    print(f"✅ Listo: {len(nuevas)} vacas creadas, "
          f"{len(nuevas) + len(agregadas)} pesajes anotados, "
          f"{len(actualizadas)} corregidos.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Cargar una jornada de pesaje real")
    p.add_argument("--fecha", required=True, help="fecha de la jornada, YYYY-MM-DD")
    p.add_argument("--archivo", help="archivo con las líneas (si no, lee de stdin)")
    p.add_argument("--ternero-id", default="1",
                   help="número a usar para el animal sin caravana (por defecto 1)")
    p.add_argument("--ensayo", action="store_true", help="mostrar sin escribir")
    args = p.parse_args()

    try:
        cuando = datetime.strptime(args.fecha, "%Y-%m-%d").date()
    except ValueError:
        print("La fecha va como YYYY-MM-DD, por ejemplo 2026-06-20")
        return 1

    texto = Path(args.archivo).read_text() if args.archivo else sys.stdin.read()
    entradas, rechazos = parsear(texto, args.ternero_id)

    if rechazos:
        print(f"⚠️  {len(rechazos)} línea(s) que no entendí:")
        for r in rechazos[:10]:
            print(f"     {r!r}")
        print()
    if not entradas:
        print("No encontré ningún pesaje.")
        return 1

    return asyncio.run(cargar(entradas, cuando, args.ensayo))


if __name__ == "__main__":
    raise SystemExit(main())
