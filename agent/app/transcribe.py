"""Voice note → text.

Split out from the conversation flow because it is the one step with an
external dependency that can plausibly fail on its own (Whisper), and the
caller needs to tell that failure apart from "I didn't understand you" — one
asks him to send it written, the other asks him to rephrase.
"""

from __future__ import annotations

import logging

from .llm import llm
from .openwa import openwa

log = logging.getLogger(__name__)


class ErrorTranscripcion(RuntimeError):
    """The voice note could not be turned into text."""


async def transcribir_nota(chat_id: str, msg_id: str) -> str:
    """Fetch a voice note from OpenWA and transcribe it in Spanish."""
    try:
        audio, mimetype, nombre = await openwa.obtener_audio(chat_id, msg_id)
    except Exception as e:  # noqa: BLE001
        raise ErrorTranscripcion(f"no pude bajar el audio: {e}") from e

    if not audio:
        raise ErrorTranscripcion("el audio venía vacío")

    try:
        texto = await llm.transcribir(audio, nombre, mimetype)
    except Exception as e:  # noqa: BLE001
        raise ErrorTranscripcion(f"no pude transcribir: {e}") from e

    log.info("nota de voz %s transcrita (%d bytes): %r", msg_id, len(audio), texto[:120])
    return texto
