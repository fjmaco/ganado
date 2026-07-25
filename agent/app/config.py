"""Runtime configuration — every knob is an environment variable.

Nothing here has a secret as its default. Anything unset that the service
genuinely cannot run without is reported by `validar()` at startup, so the
container fails loudly on a missing key instead of at 6am when someone is
standing in a corral with a cow on a scale.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "si", "sí", "on"}


def _list(name: str) -> list[str]:
    return [p.strip() for p in _str(name).split(",") if p.strip()]


@dataclass(frozen=True)
class Config:
    # --- OpenWA (WhatsApp gateway) ---------------------------------------
    openwa_url: str = field(default_factory=lambda: _str("OPENWA_URL", "http://localhost:2785"))
    openwa_api_key: str = field(default_factory=lambda: _str("OPENWA_API_KEY"))
    openwa_session: str = field(default_factory=lambda: _str("OPENWA_SESSION_ID", "pericos"))
    webhook_secret: str = field(default_factory=lambda: _str("OPENWA_WEBHOOK_SECRET"))

    # --- Who may talk to the bot -----------------------------------------
    # Bare phone numbers with country code, no '+', e.g. 573001234567.
    remitentes_permitidos: list[str] = field(default_factory=lambda: _list("ALLOWED_SENDERS"))
    # Where operational alerts go (you, not your dad).
    admin_whatsapp: str = field(default_factory=lambda: _str("ADMIN_WHATSAPP"))

    # --- LiteLLM gateway --------------------------------------------------
    litellm_url: str = field(default_factory=lambda: _str("LITELLM_URL", "http://llm.lamhara.co"))
    litellm_key: str = field(default_factory=lambda: _str("LITELLM_API_KEY"))

    # Model tier per task. The gateway falls back downward on its own
    # (x-high -> high -> medium -> low -> lowest), so these set a ceiling and
    # a saturated free tier degrades instead of failing outright.
    #
    # `tier_entender` is deliberately NOT the cheapest. The deterministic fast
    # paths in entender.py already answer simple weights and the usual
    # questions with no model at all, so everything that reaches the model is
    # the hard tail — odd phrasing, a garbled transcript, real ambiguity.
    # Pointing only the hard cases at the weakest tier is backwards, and it
    # showed up as the same question working one time and not the next.
    tier_entender: str = field(
        default_factory=lambda: _str("TIER_ENTENDER") or _str("TIER_EXTRAER_TEXTO", "high")
    )
    tier_extraer_voz: str = field(default_factory=lambda: _str("TIER_EXTRAER_VOZ", "high"))
    tier_narrar: str = field(default_factory=lambda: _str("TIER_NARRAR", "high"))
    modelo_transcribir: str = field(default_factory=lambda: _str("MODELO_TRANSCRIBIR", "transcribe"))

    # --- Google Sheets ----------------------------------------------------
    sheet_id: str = field(default_factory=lambda: _str("SHEET_ID"))
    # Service-account JSON, base64-encoded so it survives an env var intact.
    google_sa_b64: str = field(default_factory=lambda: _str("GOOGLE_SA_JSON_B64"))
    hoja_registros: str = field(default_factory=lambda: _str("HOJA_REGISTROS", "Registros"))
    hoja_vacas: str = field(default_factory=lambda: _str("HOJA_VACAS", "Vacas"))

    # --- Sanity limits on a weight ----------------------------------------
    peso_min: float = field(default_factory=lambda: _float("PESO_MIN_KG", 50))
    peso_max: float = field(default_factory=lambda: _float("PESO_MAX_KG", 1200))
    # Percentage swing against the cow's last known weight that triggers a
    # "are you sure?" instead of a silent write.
    salto_pct: float = field(default_factory=lambda: _float("SALTO_SOSPECHOSO_PCT", 20))

    # --- Behaviour --------------------------------------------------------
    # Whisper can mishear spelled-out Spanish numbers, so voice entries ask
    # for confirmation until you've seen real accuracy on his voice.
    voz_requiere_confirmacion: bool = field(
        default_factory=lambda: _bool("VOZ_REQUIERE_CONFIRMACION", True)
    )
    # How long a pending question ("¿es nueva?") stays answerable.
    ventana_confirmacion_min: int = field(
        default_factory=lambda: _int("VENTANA_CONFIRMACION_MIN", 30)
    )
    # How far back "corrige el último" may reach.
    ventana_correccion_h: int = field(default_factory=lambda: _int("VENTANA_CORRECCION_H", 24))

    # --- Resumen mensual sin que lo pida ----------------------------------
    resumen_activo: bool = field(default_factory=lambda: _bool("RESUMEN_MENSUAL", True))
    resumen_dia: int = field(default_factory=lambda: _int("RESUMEN_DIA", 1))
    resumen_hora: int = field(default_factory=lambda: _int("RESUMEN_HORA", 8))

    # --- Plumbing ---------------------------------------------------------
    # Ceiling on one message end to end. Without it a hung provider call wedges
    # the single worker and the whole queue stops — silently, because the
    # process is still perfectly healthy.
    timeout_mensaje: int = field(default_factory=lambda: _int("TIMEOUT_MENSAJE_SEG", 150))

    db_path: str = field(default_factory=lambda: _str("DB_PATH", "/data/pericos.db"))
    tz_nombre: str = field(default_factory=lambda: _str("TZ", "America/Bogota"))
    max_intentos: int = field(default_factory=lambda: _int("MAX_INTENTOS", 5))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_nombre)
        except Exception:
            return ZoneInfo("America/Bogota")

    @property
    def api_base(self) -> str:
        """OpenWA session-scoped API root."""
        return f"{self.openwa_url.rstrip('/')}/api/sessions/{self.openwa_session}"

    def validar(self) -> list[str]:
        """Return a list of fatal misconfigurations (empty means good to go)."""
        faltan = []
        if not self.openwa_api_key:
            faltan.append("OPENWA_API_KEY")
        if not self.webhook_secret:
            faltan.append("OPENWA_WEBHOOK_SECRET")
        if not self.litellm_key:
            faltan.append("LITELLM_API_KEY")
        if not self.sheet_id:
            faltan.append("SHEET_ID")
        if not self.google_sa_b64:
            faltan.append("GOOGLE_SA_JSON_B64")
        if not self.remitentes_permitidos:
            faltan.append("ALLOWED_SENDERS")
        return faltan


cfg = Config()
