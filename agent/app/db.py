"""Local SQLite state: the inbound queue, pending questions, and recent entries.

The sheet is the permanent record; this is the short-term memory that makes the
system reliable and conversational.

**The queue is the reliability story.** The webhook's only job is to write the
message here and return 200. Everything after that — understanding it, writing
to Sheets, replying — happens in a worker that retries with backoff. So a
Google outage or a rate-limited free model delays a confirmation; it never
loses a weight. `msg_id` is the primary key, which makes a redelivered webhook
a no-op rather than a duplicate row in his records.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import cfg

log = logging.getLogger(__name__)

PENDIENTE, PROCESANDO, HECHO, FALLIDO = "pendiente", "procesando", "hecho", "fallido"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS entrantes (
    msg_id           TEXT PRIMARY KEY,
    chat_id          TEXT NOT NULL,
    remitente        TEXT NOT NULL,
    tipo             TEXT NOT NULL,
    cuerpo           TEXT,
    payload          TEXT,
    estado           TEXT NOT NULL DEFAULT 'pendiente',
    intentos         INTEGER NOT NULL DEFAULT 0,
    proximo_intento  REAL NOT NULL DEFAULT 0,
    error            TEXT,
    creado           REAL NOT NULL,
    actualizado      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entrantes_cola
    ON entrantes(estado, proximo_intento);

-- Un solo asunto pendiente por chat: la última pregunta que le hicimos.
CREATE TABLE IF NOT EXISTS pendientes (
    chat_id  TEXT PRIMARY KEY,
    tipo     TEXT NOT NULL,
    datos    TEXT NOT NULL,
    creado   REAL NOT NULL,
    expira   REAL NOT NULL
);

-- Pesajes recientes, para poder corregir o borrar "lo último".
CREATE TABLE IF NOT EXISTS recientes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   TEXT NOT NULL,
    msg_id    TEXT,
    vaca      TEXT NOT NULL,
    peso      REAL NOT NULL,
    fila      INTEGER NOT NULL,
    fecha     TEXT NOT NULL,
    anulado   INTEGER NOT NULL DEFAULT 0,
    creado    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recientes_chat ON recientes(chat_id, creado DESC);

-- Ajustes internos (p. ej. el mes en que ya se mandó el resumen).
CREATE TABLE IF NOT EXISTS ajustes (
    clave  TEXT PRIMARY KEY,
    valor  TEXT NOT NULL
);
"""


class BaseDatos:
    def __init__(self, ruta: str | None = None) -> None:
        self.ruta = ruta or cfg.db_path
        self._con: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    def _conexion(self) -> sqlite3.Connection:
        if self._con is None:
            if self.ruta != ":memory:":
                Path(self.ruta).parent.mkdir(parents=True, exist_ok=True)
            self._con = sqlite3.connect(self.ruta, check_same_thread=False)
            self._con.row_factory = sqlite3.Row
            # WAL keeps the worker reading while the webhook writes.
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA busy_timeout=5000")
            self._con.executescript(ESQUEMA)
            self._con.commit()
        return self._con

    def cerrar(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # -- cola de entrada --------------------------------------------------

    def _encolar_sync(self, msg_id, chat_id, remitente, tipo, cuerpo, payload) -> bool:
        con = self._conexion()
        ahora = time.time()
        cur = con.execute(
            """INSERT OR IGNORE INTO entrantes
               (msg_id, chat_id, remitente, tipo, cuerpo, payload,
                estado, intentos, proximo_intento, creado, actualizado)
               VALUES (?,?,?,?,?,?,?,0,?,?,?)""",
            (msg_id, chat_id, remitente, tipo, cuerpo,
             json.dumps(payload, ensure_ascii=False), PENDIENTE, ahora, ahora, ahora),
        )
        con.commit()
        return cur.rowcount > 0

    async def encolar(self, msg_id, chat_id, remitente, tipo, cuerpo, payload) -> bool:
        """Queue an inbound message. False means we'd already seen this msg_id."""
        async with self._lock:
            return await asyncio.to_thread(
                self._encolar_sync, msg_id, chat_id, remitente, tipo, cuerpo, payload
            )

    def _tomar_sync(self) -> dict | None:
        con = self._conexion()
        ahora = time.time()
        fila = con.execute(
            """SELECT * FROM entrantes
               WHERE estado = ? AND proximo_intento <= ?
               ORDER BY creado LIMIT 1""",
            (PENDIENTE, ahora),
        ).fetchone()
        if fila is None:
            return None
        con.execute(
            "UPDATE entrantes SET estado=?, actualizado=? WHERE msg_id=?",
            (PROCESANDO, ahora, fila["msg_id"]),
        )
        con.commit()
        d = dict(fila)
        d["payload"] = json.loads(d["payload"] or "{}")
        return d

    async def tomar(self) -> dict | None:
        """Claim the next due message, marking it in-flight."""
        async with self._lock:
            return await asyncio.to_thread(self._tomar_sync)

    def _finalizar_sync(self, msg_id: str, estado: str, error: str | None) -> None:
        con = self._conexion()
        con.execute(
            "UPDATE entrantes SET estado=?, error=?, actualizado=? WHERE msg_id=?",
            (estado, error, time.time(), msg_id),
        )
        con.commit()

    async def marcar_hecho(self, msg_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._finalizar_sync, msg_id, HECHO, None)

    def _reintentar_sync(self, msg_id: str, error: str, maximo: int) -> bool:
        """Schedule a retry with exponential backoff. Returns True if it gave up."""
        con = self._conexion()
        fila = con.execute(
            "SELECT intentos FROM entrantes WHERE msg_id=?", (msg_id,)
        ).fetchone()
        intentos = (fila["intentos"] if fila else 0) + 1
        ahora = time.time()

        if intentos >= maximo:
            con.execute(
                """UPDATE entrantes SET estado=?, intentos=?, error=?, actualizado=?
                   WHERE msg_id=?""",
                (FALLIDO, intentos, error[:500], ahora, msg_id),
            )
            con.commit()
            return True

        # 5s, 15s, 45s, 135s… capped at 10 minutes.
        espera = min(5 * (3 ** (intentos - 1)), 600)
        con.execute(
            """UPDATE entrantes
               SET estado=?, intentos=?, proximo_intento=?, error=?, actualizado=?
               WHERE msg_id=?""",
            (PENDIENTE, intentos, ahora + espera, error[:500], ahora, msg_id),
        )
        con.commit()
        return False

    async def reintentar(self, msg_id: str, error: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._reintentar_sync, msg_id, error, cfg.max_intentos
            )

    def _profundidad_sync(self) -> dict[str, int]:
        con = self._conexion()
        filas = con.execute(
            "SELECT estado, COUNT(*) AS n FROM entrantes GROUP BY estado"
        ).fetchall()
        return {f["estado"]: f["n"] for f in filas}

    async def profundidad(self) -> dict[str, int]:
        """Queue depth by state — surfaced on /health so a stuck pipeline shows."""
        async with self._lock:
            return await asyncio.to_thread(self._profundidad_sync)

    def _recuperar_sync(self) -> int:
        """Re-queue anything left in-flight by a crash or a redeploy."""
        con = self._conexion()
        cur = con.execute(
            "UPDATE entrantes SET estado=?, actualizado=? WHERE estado=?",
            (PENDIENTE, time.time(), PROCESANDO),
        )
        con.commit()
        return cur.rowcount

    async def recuperar_huerfanos(self) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._recuperar_sync)

    # -- preguntas pendientes ---------------------------------------------

    def _guardar_pendiente_sync(self, chat_id, tipo, datos, ttl_seg) -> None:
        con = self._conexion()
        ahora = time.time()
        con.execute(
            """INSERT INTO pendientes (chat_id, tipo, datos, creado, expira)
               VALUES (?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 tipo=excluded.tipo, datos=excluded.datos,
                 creado=excluded.creado, expira=excluded.expira""",
            (chat_id, tipo, json.dumps(datos, ensure_ascii=False), ahora, ahora + ttl_seg),
        )
        con.commit()

    async def guardar_pendiente(self, chat_id: str, tipo: str, datos: dict) -> None:
        ttl = cfg.ventana_confirmacion_min * 60
        async with self._lock:
            await asyncio.to_thread(
                self._guardar_pendiente_sync, chat_id, tipo, datos, ttl
            )

    def _leer_pendiente_sync(self, chat_id: str) -> dict | None:
        con = self._conexion()
        fila = con.execute(
            "SELECT * FROM pendientes WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if fila is None:
            return None
        if fila["expira"] < time.time():
            con.execute("DELETE FROM pendientes WHERE chat_id=?", (chat_id,))
            con.commit()
            return None
        return {"tipo": fila["tipo"], "datos": json.loads(fila["datos"])}

    async def leer_pendiente(self, chat_id: str) -> dict | None:
        async with self._lock:
            return await asyncio.to_thread(self._leer_pendiente_sync, chat_id)

    def _borrar_pendiente_sync(self, chat_id: str) -> None:
        con = self._conexion()
        con.execute("DELETE FROM pendientes WHERE chat_id=?", (chat_id,))
        con.commit()

    async def borrar_pendiente(self, chat_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._borrar_pendiente_sync, chat_id)

    # -- pesajes recientes (para corregir / borrar) ------------------------

    def _guardar_reciente_sync(self, chat_id, msg_id, vaca, peso, fila, fecha) -> None:
        con = self._conexion()
        con.execute(
            """INSERT INTO recientes (chat_id, msg_id, vaca, peso, fila, fecha, creado)
               VALUES (?,?,?,?,?,?,?)""",
            (chat_id, msg_id, vaca, peso, fila, fecha, time.time()),
        )
        con.commit()

    async def guardar_reciente(self, chat_id, msg_id, vaca, peso, fila, fecha) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._guardar_reciente_sync, chat_id, msg_id, vaca, peso, fila, fecha
            )

    def _ultimo_reciente_sync(self, chat_id: str, ventana_seg: int) -> dict | None:
        con = self._conexion()
        fila = con.execute(
            """SELECT * FROM recientes
               WHERE chat_id=? AND anulado=0 AND creado >= ?
               ORDER BY creado DESC LIMIT 1""",
            (chat_id, time.time() - ventana_seg),
        ).fetchone()
        return dict(fila) if fila else None

    async def ultimo_reciente(self, chat_id: str) -> dict | None:
        """The entry `corrige` and `borra` act on."""
        ventana = cfg.ventana_correccion_h * 3600
        async with self._lock:
            return await asyncio.to_thread(self._ultimo_reciente_sync, chat_id, ventana)

    def _actualizar_reciente_sync(self, id_: int, peso: float | None, anulado: bool) -> None:
        con = self._conexion()
        if anulado:
            con.execute("UPDATE recientes SET anulado=1 WHERE id=?", (id_,))
        else:
            con.execute("UPDATE recientes SET peso=? WHERE id=?", (peso, id_))
        con.commit()

    async def actualizar_reciente(
        self, id_: int, peso: float | None = None, anulado: bool = False
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(self._actualizar_reciente_sync, id_, peso, anulado)

    # -- ajustes internos --------------------------------------------------

    def _leer_ajuste_sync(self, clave: str) -> str | None:
        fila = self._conexion().execute(
            "SELECT valor FROM ajustes WHERE clave=?", (clave,)
        ).fetchone()
        return fila["valor"] if fila else None

    async def leer_ajuste(self, clave: str) -> str | None:
        async with self._lock:
            return await asyncio.to_thread(self._leer_ajuste_sync, clave)

    def _guardar_ajuste_sync(self, clave: str, valor: str) -> None:
        con = self._conexion()
        con.execute(
            """INSERT INTO ajustes (clave, valor) VALUES (?,?)
               ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor""",
            (clave, valor),
        )
        con.commit()

    async def guardar_ajuste(self, clave: str, valor: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._guardar_ajuste_sync, clave, valor)


db = BaseDatos()
