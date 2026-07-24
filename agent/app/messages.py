"""Every user-facing string, in Spanish.

Your dad only speaks Spanish, so nothing else ever reaches him — not an error,
not a stack trace, not an English fallback. Keeping all the wording in one file
means the tone can be tuned without touching a line of logic.

Tone: short lines, warm, no jargon, no computer words. He is standing next to a
cow holding a phone, not reading a dashboard.
"""

from __future__ import annotations

from datetime import date, datetime

# Spanish date names, hardcoded so we never depend on a system locale being
# installed in the container.
DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
MESES = ("ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic")


def fecha_corta(d: date | datetime) -> str:
    """24 jul"""
    return f"{d.day} {MESES[d.month - 1]}"


def fecha_dia(d: date | datetime) -> str:
    """vie 24 jul"""
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"


def fmt_kg(v: float) -> str:
    """Colombian number formatting: 1.234,5 — thousands with '.', decimals ','."""
    if v is None:
        return "—"
    entero = int(abs(v))
    dec = abs(v) - entero
    miles = f"{entero:,}".replace(",", ".")
    signo = "-" if v < 0 else ""
    if dec >= 0.05:
        return f"{signo}{miles},{round(dec * 10)}"
    return f"{signo}{miles}"


def fmt_delta(v: float) -> str:
    """Always signed, so +15 reads as a gain at a glance."""
    return f"+{fmt_kg(v)}" if v >= 0 else fmt_kg(v)


def etiqueta(numero: str, nombre: str | None) -> str:
    """'Carmen (477)' when named, '477' when not."""
    return f"{nombre} ({numero})" if nombre else str(numero)


# --------------------------------------------------------------------------
# Registro de peso
# --------------------------------------------------------------------------

def registro_ok(numero, nombre, peso, cuando, peso_previo=None,
                fecha_previa=None, gramos_dia=None) -> str:
    lineas = [f"✅ {etiqueta(numero, nombre)} · {fmt_kg(peso)} kg · {fecha_dia(cuando)}"]

    if peso_previo is not None and fecha_previa is not None:
        dif = peso - peso_previo
        detalle = f"{fmt_delta(dif)} kg"
        if gramos_dia is not None:
            detalle += f" · {fmt_delta(gramos_dia)} g/día"
        lineas.append(f"Anterior: {fmt_kg(peso_previo)} kg el {fecha_corta(fecha_previa)}  ({detalle})")
        if dif < 0:
            lineas.append("⚠️ Bajó de peso desde la última vez.")
    else:
        lineas.append("Primer pesaje registrado.")

    lineas.append("")
    lineas.append("Si algo está mal, solo dime y lo corrijo.")
    return "\n".join(lineas)


def registro_multiple(resumen: list[str]) -> str:
    return "✅ Anoté estos pesajes:\n\n" + "\n".join(resumen) + "\n\nSi algo está mal, solo dime."


# --------------------------------------------------------------------------
# Vacas nuevas
# --------------------------------------------------------------------------

def vaca_desconocida(numero, peso) -> str:
    return (
        f"⚠️ No tengo ninguna vaca con el número {numero}.\n\n"
        f"¿Es una vaca nueva? Responde *SÍ* y la creo con {fmt_kg(peso)} kg.\n"
        f"Si te equivocaste de número, mándame el correcto."
    )


def vaca_creada(numero, nombre, peso, cuando) -> str:
    return (
        f"🐄 Le puse *{nombre}* a la vaca {numero}.\n\n"
        f"{nombre} ({numero}) · {fmt_kg(peso)} kg · {fecha_dia(cuando)} — primer pesaje."
    )


def vaca_renombrada(numero, antes, ahora) -> str:
    return f"✏️ Listo: la vaca {numero} ya no se llama {antes}, ahora es *{ahora}*."


# --------------------------------------------------------------------------
# Dudas y errores (siempre pregunta, nunca adivina)
# --------------------------------------------------------------------------

def peso_sospechoso(numero, nombre, peso, peso_previo, fecha_previa) -> str:
    return (
        f"⚠️ ¿{fmt_kg(peso)} kg?\n\n"
        f"{etiqueta(numero, nombre)} pesó {fmt_kg(peso_previo)} kg "
        f"el {fecha_corta(fecha_previa)}, es un cambio grande.\n\n"
        f"Responde *SÍ* si está bien, o mándame el peso otra vez."
    )


def peso_fuera_de_rango(peso, minimo, maximo) -> str:
    return (
        f"🤔 {fmt_kg(peso)} kg no me cuadra para una vaca "
        f"(espero entre {fmt_kg(minimo)} y {fmt_kg(maximo)} kg).\n\n"
        f"¿Me lo repites?"
    )


NO_ENTENDI = (
    "🤔 No te entendí bien.\n\n"
    "Mándame el número de la vaca y el peso, como tú prefieras. Por ejemplo:\n"
    "• _la vaca 477 pesa 327_\n"
    "• _477 327_\n"
    "• o una nota de voz diciéndolo\n\n"
    "También puedes preguntarme cosas como _¿cómo va el hato?_"
)

FALTA_PESO = "🤔 Me diste la vaca pero no el peso. ¿Cuántos kilos dio?"
FALTA_VACA = "🤔 Me diste el peso pero no sé de cuál vaca. ¿Qué número es?"

ERROR_GUARDANDO = (
    "😕 No pude guardar el dato en este momento.\n\n"
    "Ya lo tengo anotado y lo voy a reintentar solo. "
    "Si en un rato no te confirmo, mándamelo otra vez."
)

ERROR_TRANSCRIBIENDO = (
    "🎤 No logré entender la nota de voz.\n\n"
    "¿Me la mandas escrita? Con el número de la vaca y el peso me basta."
)

ERROR_GENERAL = (
    "😕 Algo falló de mi lado. Ya quedó anotado para reintentar.\n"
    "Si no te confirmo pronto, vuelve a mandármelo."
)


# --------------------------------------------------------------------------
# Correcciones
# --------------------------------------------------------------------------

def corregido(numero, nombre, antes, ahora) -> str:
    return (
        f"✏️ Corregido: {etiqueta(numero, nombre)} pasa de "
        f"{fmt_kg(antes)} a *{fmt_kg(ahora)} kg*."
    )


def borrado(numero, nombre, peso, cuando) -> str:
    return (
        f"🗑️ Borré el pesaje de {etiqueta(numero, nombre)} "
        f"({fmt_kg(peso)} kg, {fecha_corta(cuando)})."
    )


NADA_QUE_CORREGIR = (
    "🤔 No encuentro un pesaje reciente tuyo para corregir.\n"
    "Dime el número de la vaca y el peso correcto y lo anoto de nuevo."
)

CANCELADO = "👍 Listo, no anoté nada."


# --------------------------------------------------------------------------
# Ayuda y saludo
# --------------------------------------------------------------------------

AYUDA = (
    "🐄 *Hola, soy el ayudante del ganado.*\n\n"
    "*Para anotar un peso* — háblame normal:\n"
    "• _la vaca 477 pesa 327_\n"
    "• _477 327_\n"
    "• _hoy pesé la 477, dio 327 kilos_\n"
    "• o mándame una nota de voz diciéndolo\n\n"
    "Puedes mandarme varias de una vez:\n"
    "• _477 327, 348 512_\n\n"
    "*Para preguntarme cosas:*\n"
    "• _¿cómo va el hato?_\n"
    "• _¿cuál vaca ha engordado más?_\n"
    "• _¿cómo va Carmen?_\n"
    "• _¿cuáles faltan por pesar?_\n\n"
    "*Si me equivoco*, dime _no, eran 445_ o _borra lo último_.\n\n"
    "Cada vaca tiene nombre, así que también puedes decirme "
    "_Carmen pesa 430_ en vez del número."
)

SALUDO = (
    "🐄 ¡Hola! Aquí estoy para anotar los pesos del ganado.\n\n"
    "Mándame el número de la vaca y el peso como tú prefieras, "
    "o pregúntame _¿cómo va el hato?_\n\n"
    "Escribe *ayuda* cuando quieras ver todo lo que puedo hacer."
)


# --------------------------------------------------------------------------
# Reportes
# --------------------------------------------------------------------------

SIN_DATOS = (
    "📭 Todavía no tengo pesajes anotados.\n"
    "Mándame el primero: número de vaca y kilos."
)


def sin_datos_vaca(numero, nombre) -> str:
    return f"📭 Todavía no tengo pesajes de {etiqueta(numero, nombre)}."


def alerta_admin(asunto: str, detalle: str) -> str:
    return f"🚨 *Pericos* — {asunto}\n\n{detalle}"
