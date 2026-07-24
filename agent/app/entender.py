"""Turning whatever your dad said into something the system can act on.

He should never learn a format. He writes or says what comes naturally — *la
vaca 477 pesa 327*, *477 327*, *hoy pesé la 477, dio 327 kilos*, *¿cómo va
Carmen?* — and it works.

Two paths get us there:

1. A **deterministic fast path** for messages that are unambiguously just
   number pairs. It is a pure optimisation and is invisible to him: skipping it
   changes nothing about what he is allowed to say, it only saves a round trip
   on the most common message of the month.
2. The **model**, for everything else — which is the normal case for natural
   phrasing, spelled-out numbers in a voice note, and every question.

Whatever the path, the result is validated here and low confidence becomes a
question in Spanish, never a guess and never a silent drop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .config import cfg
from .llm import llm
from .nombres import buscar_por_nombre
from .sheets import numero_canonico
from .texto import normalizar

log = logging.getLogger(__name__)

# Intents the rest of the app knows how to handle.
INTENCIONES = {
    "registrar", "consultar", "corregir", "borrar",
    "renombrar", "saludo", "ayuda", "otro",
}

# Report kinds `reportes.py` implements.
CONSULTAS = {
    "hato", "total", "promedio", "minimo", "maximo",
    "mejor_ganancia", "peor_ganancia", "vaca", "sin_pesar", "alertas", "conteo",
}

# Filler your dad naturally puts around the numbers. Stripping these is what
# lets the fast path recognise "la vaca 477 pesa 327 kilos" as a bare pair.
_RELLENO = re.compile(
    r"\b(la|el|las|los|una|un|vaca|vacas|novilla|novillas|toro|ternero|ternera|"
    r"numero|nro|num|no|peso|pesa|pese|pesamos|pesaron|pesada|pesaba|pesan|"
    r"dio|da|dieron|kilos?|kg|kgs|kilogramos?|hoy|ayer|marco|marca|esta|"
    r"quedo|salio|dando|anota|anotar|apunta|y|con|en|a|de|del)\b",
    re.IGNORECASE,
)
# Units written flush against the number ("327kg") have no word boundary for
# _RELLENO to catch, so they are stripped first.
_UNIDAD_PEGADA = re.compile(r"(?<=\d)\s*(kgs?|kilogramos?|kilos?|k)\b", re.IGNORECASE)
_SOLO_NUMEROS = re.compile(r"^[\d\s.,:;/\-–—+()]*$")
_NUMERO = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class RegistroDetectado:
    vaca: str
    peso: float


@dataclass
class Consulta:
    tipo: str = "hato"
    vaca: str | None = None
    periodo: str | None = None


@dataclass
class Entendido:
    intencion: str = "otro"
    registros: list[RegistroDetectado] = field(default_factory=list)
    consulta: Consulta | None = None
    peso_correccion: float | None = None
    nombre_nuevo: str | None = None
    vaca_referida: str | None = None
    confianza: float = 0.0
    via: str = "llm"          # "regex" | "llm" — for logging and tests
    texto: str = ""


# --------------------------------------------------------------------------
# Camino rápido determinista
# --------------------------------------------------------------------------

def _a_float(s: str) -> float | None:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def fast_path(texto: str) -> list[RegistroDetectado] | None:
    """Parse a message that is *only* cow/weight pairs. None means "ask the model".

    Deliberately conservative. It fires only when, after removing the filler
    words above, nothing is left but numbers and separators — so anything with
    real sentence structure, a question, or a correction goes to the model
    instead of being guessed at here.
    """
    n = normalizar(texto)
    if not n:
        return None

    resto = _RELLENO.sub(" ", _UNIDAD_PEGADA.sub(" ", n))
    if not _SOLO_NUMEROS.match(resto):
        return None

    crudos = _NUMERO.findall(resto)
    if len(crudos) < 2 or len(crudos) % 2 != 0:
        return None

    registros: list[RegistroDetectado] = []
    for i in range(0, len(crudos), 2):
        vaca_raw, peso_raw = crudos[i], crudos[i + 1]
        peso = _a_float(peso_raw)
        if peso is None:
            return None
        # If the second number isn't a believable weight, our cow-then-weight
        # assumption is probably wrong — hand it to the model rather than
        # writing something wrong into his records.
        if not (cfg.peso_min <= peso <= cfg.peso_max):
            return None
        vaca = numero_canonico(vaca_raw)
        if not vaca or len(vaca) > 6:
            return None
        registros.append(RegistroDetectado(vaca=vaca, peso=peso))

    return registros or None


# --------------------------------------------------------------------------
# Camino con modelo
# --------------------------------------------------------------------------

_SISTEMA = """\
Eres el cerebro de un asistente de WhatsApp que le ayuda a un ganadero \
colombiano a llevar el registro del peso de sus vacas. Él habla español \
coloquial y NO sigue ningún formato: interpreta lo que quiso decir.

Devuelve SOLO un objeto JSON con esta forma exacta:

{
  "intencion": "registrar|consultar|corregir|borrar|renombrar|saludo|ayuda|otro",
  "registros": [{"vaca": "477", "peso_kg": 327}],
  "consulta": {"tipo": "hato|total|promedio|minimo|maximo|mejor_ganancia|peor_ganancia|vaca|sin_pesar|alertas|conteo", "vaca": null, "periodo": null},
  "peso_correccion": null,
  "nombre_nuevo": null,
  "vaca_referida": null,
  "confianza": 0.0
}

Reglas:
- "registrar": está reportando el peso de una o más vacas. Llena "registros".
  Los números en palabras cuéntalos como cifras ("cuatrocientos cincuenta" = 450).
  El peso de una vaca adulta va entre 50 y 1200 kg. El otro número es la vaca.
- "consultar": está preguntando algo sobre el hato. Llena "consulta".
  Si pregunta por una vaca puntual, usa tipo "vaca" y pon su número en "consulta.vaca".
  "periodo" puede ser "mes", "3meses", "ano", "todo" o null.
- "corregir": está arreglando el último dato ("no, eran 445"). Pon 445 en "peso_correccion".
- "borrar": quiere eliminar el último registro ("borra eso", "quita lo último").
- "renombrar": le quiere cambiar el nombre a una vaca. Pon el nombre en "nombre_nuevo"
  y el número en "vaca_referida".
- "saludo" para saludos, "ayuda" si pide ayuda, "otro" si no encaja en nada.
- "confianza": 0.0 a 1.0, qué tan seguro estás. Si dudas, ponla baja: es mejor
  preguntarle que inventar un dato.
- Si menciona una vaca por su NOMBRE, usa el número correspondiente del hato.
- Nunca inventes pesos ni números de vaca que él no haya dicho.
"""


def _contexto_hato(vacas: dict[str, str], limite: int = 80) -> str:
    if not vacas:
        return "El hato todavía no tiene vacas registradas."
    pares = [f"{num}={nom}" for num, nom in list(vacas.items())[:limite]]
    extra = "" if len(vacas) <= limite else f" (y {len(vacas) - limite} más)"
    return "Vacas del hato (número=nombre): " + ", ".join(pares) + extra


def _limpiar(datos: dict, vacas: dict[str, str], texto: str) -> Entendido:
    """Validate the model's JSON. Anything malformed degrades, never crashes."""
    e = Entendido(via="llm", texto=texto)

    intencion = str(datos.get("intencion") or "otro").strip().lower()
    e.intencion = intencion if intencion in INTENCIONES else "otro"

    try:
        e.confianza = max(0.0, min(1.0, float(datos.get("confianza") or 0)))
    except (TypeError, ValueError):
        e.confianza = 0.0

    for cruda in datos.get("registros") or []:
        if not isinstance(cruda, dict):
            continue
        vaca = numero_canonico(cruda.get("vaca"))
        try:
            peso = float(cruda.get("peso_kg"))
        except (TypeError, ValueError):
            continue
        if vaca and peso > 0:
            e.registros.append(RegistroDetectado(vaca=vaca, peso=peso))

    cons = datos.get("consulta")
    if isinstance(cons, dict):
        tipo = str(cons.get("tipo") or "hato").strip().lower()
        e.consulta = Consulta(
            tipo=tipo if tipo in CONSULTAS else "hato",
            vaca=numero_canonico(cons.get("vaca")) or None,
            periodo=(str(cons.get("periodo")).strip().lower()
                     if cons.get("periodo") else None),
        )

    if (v := datos.get("peso_correccion")) not in (None, ""):
        try:
            e.peso_correccion = float(v)
        except (TypeError, ValueError):
            pass

    if (v := datos.get("nombre_nuevo")) not in (None, ""):
        e.nombre_nuevo = str(v).strip()[:40]

    e.vaca_referida = numero_canonico(datos.get("vaca_referida")) or None

    # A name he used instead of a number, resolved against the real herd.
    if e.consulta and e.consulta.tipo == "vaca" and not e.consulta.vaca:
        e.consulta.vaca = buscar_por_nombre(e.texto, vacas)

    return e


async def entender(texto: str, vacas: dict[str, str], *, es_voz: bool = False) -> Entendido:
    """Understand one message. Never raises — a failure comes back as "otro"."""
    texto = (texto or "").strip()
    if not texto:
        return Entendido(intencion="otro", texto=texto)

    # 1. Deterministic pairs — no model needed.
    if (rapido := fast_path(texto)) is not None:
        return Entendido(
            intencion="registrar",
            registros=rapido,
            confianza=1.0,
            via="regex",
            texto=texto,
        )

    # 2. Everything else: ask the model.
    tier = cfg.tier_extraer_voz if es_voz else cfg.tier_extraer_texto
    usuario = f"{_contexto_hato(vacas)}\n\nMensaje del ganadero:\n\"\"\"\n{texto}\n\"\"\""

    try:
        datos = await llm.chat_json(tier, _SISTEMA, usuario, max_tokens=600)
    except Exception as e:  # noqa: BLE001 — degrade into a question, never crash
        log.warning("no se pudo entender el mensaje vía LLM: %s", e)
        return Entendido(intencion="otro", confianza=0.0, texto=texto)

    entendido = _limpiar(datos, vacas, texto)

    # A cow referred to only by name, in a logging message.
    for reg in entendido.registros:
        if not reg.vaca:
            if numero := buscar_por_nombre(texto, vacas):
                reg.vaca = numero
    entendido.registros = [r for r in entendido.registros if r.vaca]

    return entendido
