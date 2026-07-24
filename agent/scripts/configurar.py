#!/usr/bin/env python3
"""One-off setup: check the session, register the webhook, prepare the sheet.

Run this once after both services are up and the phone is paired:

    python -m scripts.configurar --revisar          # estado, sin cambiar nada
    python -m scripts.configurar --hoja             # crear pestañas y fórmulas
    python -m scripts.configurar --webhook https://…/webhook/openwa
    python -m scripts.configurar --vacas 477,348,312   # precargar el hato

Registering the webhook against a container-internal URL only works when
`SSRF_ALLOWED_HOSTS` on the openwa service includes the agent's appName —
OpenWA validates the URL at registration time and rejects private addresses
with a 400 otherwise. That failure is reported here in plain terms.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import cfg  # noqa: E402
from app.nombres import asignar_nombre  # noqa: E402
from app.openwa import openwa  # noqa: E402
from app.sheets import hato  # noqa: E402


async def revisar() -> int:
    print(f"OpenWA   : {cfg.openwa_url}  (sesión '{cfg.openwa_session}')")
    print(f"LiteLLM  : {cfg.litellm_url}")
    print(f"Hoja     : {cfg.sheet_id or '(sin configurar)'}")
    print(f"Permitidos: {', '.join(cfg.remitentes_permitidos) or '(ninguno)'}")

    if faltan := cfg.validar():
        print(f"\n⚠️  Faltan variables: {', '.join(faltan)}")

    try:
        estado = await openwa.estado_sesion()
        print(f"\nSesión de WhatsApp: {estado.get('status')}")
        if estado.get("status") != "ready":
            print("   → todavía no está lista; empareja el teléfono en el dashboard.")
    except Exception as e:  # noqa: BLE001
        print(f"\n❌ No pude hablar con OpenWA: {e}")
        return 1

    try:
        hooks = await openwa.listar_webhooks()
        print(f"\nWebhooks registrados: {len(hooks)}")
        for h in hooks:
            print(f"   • {h.get('url')}  eventos={h.get('events')}")
        if not hooks:
            print("   → ninguno; regístralo con --webhook")
    except Exception as e:  # noqa: BLE001
        print(f"❌ No pude listar webhooks: {e}")
        return 1

    return 0


async def preparar_hoja() -> int:
    try:
        await hato.asegurar_estructura()
    except Exception as e:  # noqa: BLE001
        print(f"❌ No pude preparar la hoja: {e}")
        print("   Revisa que la hoja esté compartida como Editor con el email")
        print("   de la service account, y que SHEET_ID sea correcto.")
        return 1

    vacas = await hato.vacas(refrescar=True)
    print("✅ Hoja lista: pestañas 'Registros', 'Vacas' y 'Tabla'.")
    print(f"   Vacas registradas: {len(vacas)}")
    return 0


async def registrar_webhook(url: str) -> int:
    try:
        hook = await openwa.registrar_webhook(
            url=url, secreto=cfg.webhook_secret, remitentes=cfg.remitentes_permitidos
        )
    except Exception as e:  # noqa: BLE001
        print(f"❌ No pude registrar el webhook: {e}")
        if "400" in str(e):
            print("\n   Un 400 aquí casi siempre es la protección SSRF de OpenWA:")
            print("   valida la URL al registrarla y rechaza direcciones privadas.")
            print("   Añade el appName del agente a SSRF_ALLOWED_HOSTS en el")
            print("   servicio openwa y vuelve a intentar.")
        return 1

    verbo = "actualizado" if hook.pop("_actualizado", False) else "registrado"
    print(f"✅ Webhook {verbo}: {hook.get('id')} → {url}")
    if cfg.remitentes_permitidos:
        print(f"   Filtrado a: {', '.join(cfg.remitentes_permitidos)}")
    else:
        print("   ⚠️  Sin ALLOWED_SENDERS: OpenWA entregará TODO y el filtrado")
        print("      queda sólo del lado del agente. Llena la variable y vuelve")
        print("      a correr esto para filtrar también en la pasarela.")
    return 0


async def precargar_vacas(numeros: str) -> int:
    hoy = date.today()
    existentes = await hato.vacas(refrescar=True)
    creadas = []

    for crudo in numeros.split(","):
        numero = crudo.strip()
        if not numero or numero in existentes:
            continue
        vaca = await hato.crear_vaca(numero, hoy)
        creadas.append((numero, vaca.nombre))
        existentes = await hato.vacas(refrescar=True)

    if not creadas:
        print("Nada que crear: todas esas vacas ya estaban.")
        return 0

    print(f"✅ {len(creadas)} vacas creadas:")
    for numero, nombre in creadas:
        print(f"   {numero} → {nombre}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Configuración inicial de Pericos")
    p.add_argument("--revisar", action="store_true", help="mostrar estado, sin cambiar nada")
    p.add_argument("--hoja", action="store_true", help="crear pestañas y fórmulas")
    p.add_argument("--webhook", metavar="URL", help="registrar el webhook en OpenWA")
    p.add_argument("--vacas", metavar="N,N,N", help="precargar números de vaca")
    args = p.parse_args()

    async def correr() -> int:
        codigo = 0
        hizo_algo = False
        if args.revisar:
            codigo |= await revisar(); hizo_algo = True
        if args.hoja:
            codigo |= await preparar_hoja(); hizo_algo = True
        if args.vacas:
            codigo |= await precargar_vacas(args.vacas); hizo_algo = True
        if args.webhook:
            codigo |= await registrar_webhook(args.webhook); hizo_algo = True
        if not hizo_algo:
            p.print_help()
        await openwa.cerrar()
        return codigo

    return asyncio.run(correr())


if __name__ == "__main__":
    raise SystemExit(main())
