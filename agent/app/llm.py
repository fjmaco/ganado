"""Client for the LiteLLM free-tier gateway.

Two things matter here.

**Tiers.** The gateway exposes five logical models (`x-high` … `lowest`) and
falls back downward on its own when a tier is saturated or cooling down. Asking
for a tier therefore sets a ceiling, not a guarantee — which is exactly what we
want: a busy free tier degrades to a smaller model instead of erroring.

**Retries.** Everything the models are used for here is either replacing a
notebook entry or answering a question about the herd, and neither may be lost
because a free provider had a bad minute. So calls retry with backoff on top of
the gateway's own `num_retries`, and callers get a clear failure to act on
rather than an exception to swallow.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import cfg

log = logging.getLogger(__name__)

# Free models sometimes wrap JSON in prose or a fenced block despite being told
# not to. Rather than fail, dig the object out.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ErrorLLM(RuntimeError):
    """The gateway could not answer after every retry."""


class RespuestaInvalida(ErrorLLM):
    """The gateway answered, but not with the JSON contract we asked for."""


def extraer_json(texto: str) -> dict:
    """Pull a JSON object out of a model response, tolerating fences and prose."""
    if not texto:
        raise RespuestaInvalida("respuesta vacía")

    candidatos: list[str] = []
    if m := _FENCE.search(texto):
        candidatos.append(m.group(1).strip())
    candidatos.append(texto.strip())

    # Last resort: the outermost {...} span in the text.
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio != -1 and fin > inicio:
        candidatos.append(texto[inicio : fin + 1])

    for c in candidatos:
        try:
            valor = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(valor, dict):
            return valor

    raise RespuestaInvalida(f"no se pudo leer JSON de: {texto[:200]!r}")


class ClienteLLM:
    def __init__(self, cliente: httpx.AsyncClient | None = None) -> None:
        self._cliente = cliente
        self._propio = cliente is None

    async def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(
                base_url=cfg.litellm_url.rstrip("/"),
                headers={"Authorization": f"Bearer {cfg.litellm_key}"},
                timeout=httpx.Timeout(90.0, connect=10.0),
            )
        return self._cliente

    async def cerrar(self) -> None:
        if self._cliente is not None and self._propio:
            await self._cliente.aclose()
            self._cliente = None

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, ErrorLLM)),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=20),
        reraise=True,
    )
    async def chat(
        self,
        tier: str,
        sistema: str,
        usuario: str,
        *,
        temperatura: float = 0.0,
        max_tokens: int = 700,
    ) -> str:
        """One chat completion against a tier. Returns the raw text."""
        http = await self._http()
        cuerpo = {
            "model": tier,
            "messages": [
                {"role": "system", "content": sistema},
                {"role": "user", "content": usuario},
            ],
            "temperature": temperatura,
            "max_tokens": max_tokens,
        }
        r = await http.post("/v1/chat/completions", json=cuerpo)
        if r.status_code >= 500 or r.status_code == 429:
            raise ErrorLLM(f"gateway {r.status_code}: {r.text[:200]}")
        r.raise_for_status()

        datos = r.json()
        try:
            return datos["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RespuestaInvalida(f"respuesta inesperada del gateway: {datos}") from e

    async def chat_json(
        self,
        tier: str,
        sistema: str,
        usuario: str,
        *,
        temperatura: float = 0.0,
        max_tokens: int = 700,
    ) -> dict:
        """A chat completion that must come back as a JSON object.

        Retried independently of `chat`: a model that answered but rambled is a
        different failure from one that never answered, and is worth one more
        shot with the instruction repeated more firmly.
        """
        refuerzo = (
            f"{sistema}\n\n"
            "RESPONDE ÚNICAMENTE CON UN OBJETO JSON VÁLIDO. "
            "Sin explicaciones, sin markdown, sin ```."
        )
        ultimo: Exception | None = None
        for intento in range(2):
            try:
                crudo = await self.chat(
                    tier, refuerzo, usuario,
                    temperatura=temperatura, max_tokens=max_tokens,
                )
                return extraer_json(crudo)
            except RespuestaInvalida as e:
                ultimo = e
                log.warning("respuesta no-JSON del tier %s (intento %d): %s", tier, intento + 1, e)
        raise RespuestaInvalida(str(ultimo))

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, ErrorLLM)),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=2, max=30),
        reraise=True,
    )
    async def transcribir(self, audio: bytes, nombre_archivo: str, mimetype: str) -> str:
        """Speech to text via the gateway's `transcribe` model (Groq Whisper).

        Pinned to Spanish — your dad speaks nothing else, and telling Whisper
        the language up front measurably reduces number errors versus letting
        it auto-detect from a few seconds of audio.
        """
        http = await self._http()
        r = await http.post(
            "/v1/audio/transcriptions",
            files={"file": (nombre_archivo, audio, mimetype)},
            data={
                "model": cfg.modelo_transcribir,
                "language": "es",
                "temperature": "0",
                # Priming the decoder with the vocabulary it should expect.
                "prompt": "Pesaje de ganado. Se menciona el número de la vaca y su peso en kilos.",
            },
        )
        if r.status_code >= 500 or r.status_code == 429:
            raise ErrorLLM(f"transcripción {r.status_code}: {r.text[:200]}")
        r.raise_for_status()

        datos = r.json()
        texto = (datos.get("text") or "").strip()
        if not texto:
            raise RespuestaInvalida("transcripción vacía")
        return texto


llm = ClienteLLM()
