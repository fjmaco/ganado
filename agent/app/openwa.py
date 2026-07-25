"""Client for the OpenWA WhatsApp gateway.

Two jobs: send replies, and fetch the bytes of a voice note.

That second one is less obvious than it looks. The `message.received` webhook
tells us `type: "voice"` and `hasMedia: true` but carries **no audio payload**,
and OpenWA exposes no per-message media download route — the only `/media`
endpoint in its API is for statuses. So the audio has to be pulled back out of
the chat history with `includeMedia=true` and matched on the message id.
"""

from __future__ import annotations

import base64
import binascii
import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import cfg

log = logging.getLogger(__name__)


class ErrorOpenWA(RuntimeError):
    """OpenWA could not be reached or refused the request."""


class ClienteOpenWA:
    def __init__(self, cliente: httpx.AsyncClient | None = None) -> None:
        self._cliente = cliente
        self._propio = cliente is None

    async def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(
                base_url=cfg.api_base,
                headers={"X-API-Key": cfg.openwa_api_key},
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._cliente

    async def cerrar(self) -> None:
        if self._cliente is not None and self._propio:
            await self._cliente.aclose()
            self._cliente = None

    # -- enviar -----------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, ErrorOpenWA)),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1, max=20),
        reraise=True,
    )
    async def enviar_texto(self, chat_id: str, texto: str) -> str | None:
        """Send a reply. Returns the message id, or None if unreported.

        WhatsApp caps a text body at 4096 characters; nothing this bot writes
        comes close, but a runaway report should be truncated rather than
        rejected outright.
        """
        http = await self._http()
        if len(texto) > 4000:
            texto = texto[:3990] + "\n…"

        r = await http.post("/messages/send-text", json={"chatId": chat_id, "text": texto})
        if r.status_code >= 500 or r.status_code == 429:
            raise ErrorOpenWA(f"send-text {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        try:
            return r.json().get("messageId")
        except Exception:
            return None

    async def marcar_escribiendo(self, chat_id: str) -> None:
        """Show the typing indicator. Best-effort — never worth failing a reply."""
        try:
            http = await self._http()
            await http.post("/chats/typing", json={"chatId": chat_id, "duration": 3000})
        except Exception:
            pass

    # -- recibir audio ----------------------------------------------------

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, ErrorOpenWA)),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=2, max=20),
        reraise=True,
    )
    async def obtener_audio(
        self, chat_id: str, msg_id: str, limite: int = 10
    ) -> tuple[bytes, str, str]:
        """Fetch a voice note's bytes. Returns (audio, mimetype, filename)."""
        http = await self._http()
        r = await http.get(
            f"/messages/{chat_id}/history",
            params={"limit": limite, "includeMedia": "true"},
        )
        if r.status_code >= 500 or r.status_code == 429:
            raise ErrorOpenWA(f"history {r.status_code}: {r.text[:200]}")
        r.raise_for_status()

        mensajes = r.json()
        if not isinstance(mensajes, list):
            raise ErrorOpenWA(f"history devolvió {type(mensajes).__name__}, no una lista")

        objetivo = next((m for m in mensajes if m.get("id") == msg_id), None)
        if objetivo is None:
            raise ErrorOpenWA(f"no encontré el mensaje {msg_id} en los últimos {limite}")

        media = objetivo.get("media") or {}
        if media.get("omitted"):
            raise ErrorOpenWA(f"OpenWA omitió el audio de {msg_id} (¿demasiado grande?)")

        datos = media.get("data")
        if not datos:
            raise ErrorOpenWA(f"el mensaje {msg_id} no traía audio")

        try:
            audio = base64.b64decode(datos)
        except (binascii.Error, ValueError) as e:
            raise ErrorOpenWA(f"audio de {msg_id} no es base64 válido") from e

        mimetype = media.get("mimetype") or "audio/ogg"
        # Whisper picks its decoder off the extension, so the filename matters.
        base = mimetype.split(";")[0].strip()
        ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a",
               "audio/wav": "wav", "audio/webm": "webm", "audio/aac": "aac"}.get(base, "ogg")
        return audio, mimetype, f"nota.{ext}"

    # -- administración (usado por scripts/configurar.py) ------------------

    async def estado_sesion(self) -> dict:
        http = await self._http()
        r = await http.get("")
        r.raise_for_status()
        return r.json()

    async def listar_webhooks(self) -> list[dict]:
        http = await self._http()
        r = await http.get("/webhooks")
        r.raise_for_status()
        datos = r.json()
        return datos if isinstance(datos, list) else datos.get("webhooks", [])

    async def registrar_webhook(
        self, url: str, secreto: str, remitentes: list[str], reintentos: int = 3
    ) -> dict:
        """Subscribe to inbound messages, filtered to the allowed senders.

        The filter runs inside OpenWA, so messages from anyone else are dropped
        before a request is ever made to this service — defence in depth on top
        of the allowlist check in `security.py`.

        Idempotent by URL: re-running this after adding a sender updates the
        existing subscription instead of leaving two live webhooks behind, which
        would deliver every message twice.
        """
        http = await self._http()
        cuerpo = {
            "url": url,
            "events": ["message.received"],
            "secret": secreto,
            "retryCount": reintentos,
        }
        if remitentes:
            cuerpo["filters"] = {
                "conditions": [
                    {
                        "field": "sender",
                        "operator": "is",
                        "value": [f"{n}@c.us" for n in remitentes],
                    }
                ]
            }

        existentes = await self.listar_webhooks()
        creado = await self._crear_o_actualizar(
            existentes, cuerpo, lambda w: "message.received" in (w.get("events") or [])
        )

        # Session lifecycle goes on a SEPARATE webhook, with no filter.
        #
        # The filter above matches on `sender`, which only exists on message
        # events — putting session events behind it would drop every one of
        # them, and the whole point is to notice when the link dies. Two
        # subscriptions is the only way to have both.
        vigilancia = {
            "url": url,
            "events": [
                "session.status", "session.disconnected",
                "session.qr", "session.authenticated",
            ],
            "secret": secreto,
            "retryCount": reintentos,
        }
        await self._crear_o_actualizar(
            existentes, vigilancia,
            lambda w: any(e.startswith("session.") for e in (w.get("events") or [])),
        )
        return creado

    async def _crear_o_actualizar(self, existentes, cuerpo, coincide) -> dict:
        http = await self._http()
        for w in existentes:
            if w.get("url") == cuerpo["url"] and coincide(w):
                r = await http.put(f"/webhooks/{w['id']}", json=cuerpo)
                r.raise_for_status()
                datos = r.json()
                datos["_actualizado"] = True
                return datos
        r = await http.post("/webhooks", json=cuerpo)
        r.raise_for_status()
        return r.json()


openwa = ClienteOpenWA()
