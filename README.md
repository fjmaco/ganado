# 🐄 Pericos — registro de ganado por WhatsApp

Mi papá tiene entre 20 y 40 vacas y acaba de comprar una báscula. Sin esto, los
pesos se quedan en un cuaderno y el historial se pierde. Él no usa computador,
así que la interfaz es la única app que ya usa: **WhatsApp**.

Le manda un mensaje o una nota de voz — *la vaca 477 pesa 327* — y queda
registrado en una hoja de Google, con confirmación de vuelta y el cambio desde
la última vez. También puede preguntar: *¿cómo va el hato?*, *¿cuál engordó
más?*, *¿cómo va Carmen?*

**Todo el bot habla español.** Es el único idioma de su usuario.

---

## Cómo funciona

```
Teléfono de papá ──WhatsApp──► openwa  (pasarela, motor Baileys)
                                   │
                                   │  webhook message.received (firmado HMAC,
                                   │  filtrado a su número en el propio OpenWA)
                                   ▼
                              agent  (FastAPI)
                                   │
                    ①  guarda el mensaje en SQLite y responde 200
                                   │
                    ②  un worker aparte lo procesa, con reintentos
                                   │
      ┌────────────────────────────┼──────────────────────┬──────────────────┐
      ▼                            ▼                      ▼                  ▼
  ¿voz? baja el ogg          entender (español          registrar →      consultar →
  y lo transcribe            libre, sin formato)        Sheets + eco     pandas + eco
      │                            │
      ▼                            ▼
  llm.lamhara.co  ·  gateway LiteLLM de tiers gratis
                                   │
                                   ▼
                    Google Sheet · Registros (solo se agregan filas)
                                 · Vacas     (número ↔ nombre)
                                 · Tabla     (la cuadrícula ancha, por fórmula)
```

## Decisiones que vale la pena entender

**Él nunca aprende un formato.** El modelo es el intérprete principal. Hay un
camino rápido determinista para mensajes que son puramente pares número/peso
(`477 327`), pero es solo una optimización: saltárselo no cambia nada de lo que
él puede decir, solo ahorra una llamada en el mensaje más común del mes. Todo
lo demás — frases naturales, números deletreados en una nota de voz, preguntas —
lo resuelve el modelo.

**La hoja solo recibe filas nuevas.** Cada escritura es una sola llamada, sin
leer-modificar-escribir, así que un reintento tras un timeout no puede pisar la
celda de nadie. La cuadrícula ancha (una columna por fecha) existe igual, pero
la produce una fórmula en la pestaña `Tabla` — el bot nunca mete la mano ahí.
Borrar no borra: marca `anulado`, y el pivote deja de contarla.

**El modelo nunca hace cuentas.** Cada cifra de cada reporte se calcula en
Python sobre la hoja. El modelo elige qué reporte es y agrega una frase final
de color; si esa frase trae un número, se descarta entera. Los modelos gratis
se equivocan en aritmética con total seguridad, y estos son los datos de su
hato.

**Nada se pierde por una mala racha.** El webhook solo encola y responde. Si
Sheets se cae o un tier gratis está saturado, se atrasa la confirmación, no se
pierde el peso. `msg_id` es la llave primaria de la cola, así que un webhook
reentregado es un no-op. Cuando se agotan los reintentos, él recibe un aviso en
español de que **no** se guardó — el silencio se leería como éxito.

**Un número desconocido se pregunta, no se crea.** Con 30 vacas conocidas, un
`34` donde iba `347` sería una vaca fantasma que ensucia todos los promedios
para siempre. Un peso que se sale más de 20% del anterior también se pregunta.

**Cada vaca tiene nombre** — Carmen, Lucía, Paloma. Se asignan de una lista de
~160 nombres españoles reales, recorriéndola en orden fijo, así que no hay
duplicados posibles y un reinicio asigna lo mismo. Además él puede decir
*Carmen pesa 430* en vez del número.

## Tiers del gateway

El gateway cae solo hacia abajo (`x-high → high → medium → low → lowest`), así
que pedir un tier fija un techo y un tier saturado degrada en vez de fallar.

| Tarea | Tier | Por qué |
|---|---|---|
| Pesajes simples y preguntas de siempre | *(ninguno)* | Los resuelve el camino rápido, sin llamar a nadie |
| Entender el resto (`TIER_ENTENDER`) | `high` | Es la cola difícil, no el caso fácil — ver abajo |
| Nota de voz (`TIER_EXTRAER_VOZ`) | `high` | Muletillas y números deletreados (*trescientos veintisiete*) |
| Frase final del reporte (`TIER_NARRAR`) | `high` | Es lo que él lee; el tono importa |
| Transcribir voz (`MODELO_TRANSCRIBIR`) | `transcribe` | Groq Whisper, `language=es` |

**El tier de entendimiento no es el barato, y es a propósito.** Los caminos
rápidos ya contestan los pesajes normales y las preguntas frecuentes sin
modelo, así que todo lo que *llega* al modelo es, por construcción, lo difícil:
una frase rara, una transcripción machucada, algo ambiguo. Mandarle sólo los
casos difíciles al tier más débil está al revés — y se notaba: la misma
pregunta funcionaba una vez y a la siguiente no.

## Estructura

```
ganado/
├── docs/
│   ├── SETUP.md         # despliegue paso a paso
│   └── PARA-PAPA.md     # la hoja que le mando a él
└── agent/
    ├── Dockerfile
    ├── app/
    │   ├── main.py          # webhook + /health
    │   ├── worker.py        # cola, reintentos, dead-letter
    │   ├── agenda.py        # resumen mensual sin que lo pida
    │   ├── entender.py      # camino rápido + modelo
    │   ├── conversacion.py  # las reglas que cuidan los datos
    │   ├── reportes.py      # toda la aritmética (pandas)
    │   ├── sheets.py        # Google Sheets
    │   ├── nombres.py       # ~160 nombres españoles
    │   ├── messages.py      # cada texto que él ve
    │   └── …
    ├── scripts/configurar.py
    └── tests/               # 124 pruebas
```

## Desarrollo

```bash
cd agent
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q          # 124 pruebas, sin red
```

Las pruebas corren con el modelo inalcanzable a propósito: lo que se verifica
son las decisiones alrededor del modelo, no el modelo.

Para desplegar, ver [docs/SETUP.md](docs/SETUP.md).

## Riesgos conocidos

- **OpenWA es un cliente no oficial de WhatsApp.** Usa un SIM dedicado; el
  motor Baileys tiene más riesgo de bloqueo que el de navegador. Si pasa, la
  sesión es desechable — la hoja es el activo real.
- **OpenWA es joven** (v0.10.x). La imagen va fijada por tag, nunca `latest`.
- **Whisper puede oír mal un número deletreado.** Por eso las notas de voz se
  confirman antes de escribirse mientras `VOZ_REQUIERE_CONFIRMACION` esté en
  `true`.
