# Puesta en marcha

Guía exacta, en orden. Cada paso dice **dónde** hacerlo y **qué** escribir.

> **Ningún secreto vive en este repo** — es público. Donde haga falta una clave,
> aquí sólo se dice *dónde encontrarla* en Dokploy.

---

## ⚠️ El orden importa

Montar el volumen en `openwa` **borra `/app/data`**, que es donde OpenWA guarda
su base de datos: la sesión de WhatsApp y los webhooks registrados.

Por eso el volumen va **antes** de escanear el QR. Si se escanea primero y se
monta después, hay que volver a emparejar el teléfono y a registrar el webhook.

Orden correcto: **volumen → sesión → QR → webhook**.

---

## 1. Google: conseguir dos valores

Se necesitan `SHEET_ID` y `GOOGLE_SA_JSON_B64`. Es el paso más lento y no
depende de nada más, así que conviene arrancar por aquí.

**a) Service account** → <https://console.cloud.google.com/iam-admin/serviceaccounts>

1. Elegir o crear un proyecto.
2. **Create service account** → nombre `pericos` → *Create* → *Done*
   (los pasos de roles se pueden saltar: no necesita permisos de GCP).
3. Abrirla → pestaña **Keys** → *Add key → Create new key → **JSON*** → se
   descarga un archivo.
4. Anotar su email, de la forma `pericos@<proyecto>.iam.gserviceaccount.com`.

**b) Habilitar la API** →
<https://console.cloud.google.com/apis/library/sheets.googleapis.com> → **Enable**

**c) La hoja de cálculo** → <https://sheets.new>

1. Nombrarla *Ganado*.
2. **Compartir** → pegar el email de la service account → rol **Editor** → Enviar.
   Sin esto, todo responde `403`.
3. De la URL `docs.google.com/spreadsheets/d/`**`1AbC…xyz`**`/edit`, la parte en
   negrita es el **`SHEET_ID`**.

**d) Codificar la llave:**

```bash
base64 -w0 ~/Downloads/<el-archivo>.json
```

Esa única línea larga es **`GOOGLE_SA_JSON_B64`**.

---

## 2. Volúmenes (interfaz de Dokploy)

<http://150.136.68.245:3000> → proyecto **pericos**

En cada aplicación: pestaña **Advanced → Volumes → Add**, tipo **Volume Mount**.

| Aplicación | Volume Name | Mount Path | Si falta… |
|---|---|---|---|
| **openwa** | `openwa-data` | `/app/data` | cada redeploy obliga a emparejar el teléfono otra vez |
| **agent** | `pericos-data` | `/data` | un redeploy a mitad de un pesaje pierde la cola |

Guardar en cada una. Después, **Deploy** sólo en `openwa`.

---

## 3. Crear la sesión y escanear

Dashboard de OpenWA:
<https://app-index-cross-platform-matrix-mcd8lk-dc4145-150-136-68-245.sslip.io>

La clave que pide es la `API_MASTER_KEY` que está en
**Dokploy → pericos → openwa → Environment**.

1. Crear una sesión llamada **`pericos`**.
2. **Start** → escanear el QR con el teléfono del **SIM dedicado** (nunca el
   número personal de mi papá).
3. Esperar a que el estado pase a `ready`.
4. **Copiar el UUID de la sesión.** Hace falta en el paso 4.

> **Ojo:** las rutas de la API usan ese **UUID**, no el nombre.
> `/api/sessions/pericos` devuelve `400`. Si alguna vez se recrea la sesión,
> hay que actualizar `OPENWA_SESSION_ID`.
>
> Si se prefiere teclear un código en vez de escanear:
> `POST /api/sessions/<uuid>/pairing-code`.

---

## 4. Variables del agente

**Dokploy → pericos → agent → Environment.** Llenar los cinco huecos:

```
OPENWA_SESSION_ID=<el UUID del paso 3>
ALLOWED_SENDERS=<número de mi papá, ej. 573001112233>
ADMIN_WHATSAPP=<mi número, para alertas técnicas>
SHEET_ID=<del paso 1c>
GOOGLE_SA_JSON_B64=<la línea del paso 1d>
```

Números con indicativo, sin `+` y sin espacios. Guardar.

El servicio **no arranca** si falta alguna: sale
`Faltan variables de entorno obligatorias: …` en el log del despliegue. Es a
propósito — mejor que falle al desplegar y no en silencio a las 6am.

---

## 5. Preparar la hoja y registrar el webhook

Desde mi máquina, con el repo clonado:

```bash
cd /home/tito/Desktop/other/ganado/agent

export OPENWA_URL=https://app-index-cross-platform-matrix-mcd8lk-dc4145-150-136-68-245.sslip.io
export OPENWA_API_KEY=<API_MASTER_KEY de openwa en Dokploy>
export OPENWA_WEBHOOK_SECRET=<OPENWA_WEBHOOK_SECRET de agent en Dokploy>
export OPENWA_SESSION_ID=<el UUID del paso 3>
export ALLOWED_SENDERS=<número de mi papá>
export SHEET_ID=<del paso 1c>
export GOOGLE_SA_JSON_B64=<la línea del paso 1d>

# Crea las pestañas Registros / Vacas / Tabla y sus fórmulas
.venv/bin/python -m scripts.configurar --hoja

# Precarga el hato — imprime cada número con el nombre que le tocó
.venv/bin/python -m scripts.configurar --vacas 477,348,312

# Suscribe el webhook
.venv/bin/python -m scripts.configurar --webhook http://app-program-1080p-interface-ktj5b6:8000/webhook/openwa

# Resumen de cómo quedó todo
.venv/bin/python -m scripts.configurar --revisar
```

Dos cosas que parecen erratas y no lo son:

- `OPENWA_URL` apunta al **dominio público** porque el script corre desde mi
  máquina, que no ve la red interna de Dokploy.
- La URL del webhook es la **interna** (`app-program-…:8000`), porque es OpenWA
  quien va a llamarla, desde dentro.

El comando es idempotente por URL: correrlo de nuevo **actualiza** el webhook
existente en vez de dejar dos vivos entregando cada mensaje por duplicado.

> Si el registro devuelve **400**, es la protección SSRF de OpenWA: valida la URL
> *al registrarla* y rechaza direcciones privadas. `SSRF_ALLOWED_HOSTS` (en el
> Environment de `openwa`) tiene que contener el `appName` del agente.

---

## 6. Desplegar el agente

**Dokploy → pericos → agent → Deploy.**

Comprobar:

```bash
curl -s https://<dominio-del-agente>/health
# {"estado":"ok","cola":{},"fallidos":0,"worker":true}
```

*(El agente no tiene dominio propio: no lo necesita, sólo lo llama OpenWA por la
red interna. Si se quiere ver `/health` desde fuera, agregar un dominio al
puerto 8000 en su pestaña **Domains**.)*

---

## 7. Recargar el gateway (sólo para notas de voz)

> **Deploy NO alcanza.** LiteLLM lee `config.yaml` una sola vez, al arrancar, y
> el archivo entra por volumen (`./config.yaml:/app/config.yaml:ro`). Un deploy
> baja el archivo nuevo al disco pero `docker compose up` no ve cambios en la
> imagen ni en el compose, así que **no recrea el contenedor** y el proceso
> sigue con la configuración vieja en memoria. El deploy termina en medio
> segundo y dice `done`, que es justo lo que despista.
>
> Hace falta **Stop y luego Start**. (Es lo mismo que hace el workflow de
> autoheal del repo `litellm`: *redeploy + stop/start to pull and reload*.)

**Dokploy → proyecto Personal → servicio `llm` → Stop → Start.**

Verificar que quedó — el modelo tiene que aparecer en la lista:

```bash
curl -s http://llm.lamhara.co/v1/models \
  -H "Authorization: Bearer <LITELLM_MASTER_KEY>" | grep -o transcribe
```

Y que de verdad transcribe, no sólo que esté listado:

```bash
curl -s http://llm.lamhara.co/v1/audio/transcriptions \
  -H "Authorization: Bearer <LITELLM_MASTER_KEY>" \
  -F "file=@<un-audio>.ogg" -F "model=transcribe" -F "language=es"
# -> {"text":"..."}
```

Todo lo demás funciona sin esto; sólo las notas de voz lo necesitan.

> **Ojo con el silencio.** Whisper *alucina* cuando el audio no trae voz: con un
> tono puro de prueba devolvió `"¡Gracias!"`. Con ruido de corral puede pasar
> igual, y es exactamente por eso que `VOZ_REQUIERE_CONFIRMACION=true` viene
> activado: una nota de voz se lee de vuelta antes de escribir nada.

---

## 8. Probar, desde el teléfono de mi papá

| # | Mandar | Esperar |
|---|---|---|
| 1 | `477 327` | eco con nombre, peso y fecha |
| 2 | `no, eran 445` | corrige **la misma fila**, no agrega otra |
| 3 | `borra lo último` | sale de `Tabla`; la fila queda marcada `anulado` |
| 4 | `99 400` (número que no existe) | **pregunta** antes de crear nada |
| 5 | una nota de voz en español | la lee de vuelta antes de escribir |
| 6 | `¿cómo va el hato?` | reporte con cifras |
| 7 | *(redeploy de `openwa`)* | la sesión sobrevive |

La 7 es la que prueba que el volumen del paso 2 quedó bien.

Después, mandarle [PARA-PAPA.md](PARA-PAPA.md) a él.

---

## Referencia

**Dokploy** — <http://150.136.68.245:3000>, proyecto **pericos**

| | `openwa` | `agent` |
|---|---|---|
| Origen | imagen `rmyndharis/openwa:0.10.10` | GitHub `fjmaco/ganado`, contexto `/agent` |
| Puerto | 2785 | 8000 |
| `appName` (DNS interno) | `app-index-cross-platform-matrix-mcd8lk` | `app-program-1080p-interface-ktj5b6` |
| Volumen | `/app/data` | `/data` |

- El agente llama a OpenWA en `http://app-index-cross-platform-matrix-mcd8lk:2785`
- OpenWA entrega el webhook en `http://app-program-1080p-interface-ktj5b6:8000/webhook/openwa`

**Dónde está cada secreto**

| Valor | Dónde |
|---|---|
| `API_MASTER_KEY` de OpenWA | Dokploy → pericos → openwa → Environment |
| `OPENWA_WEBHOOK_SECRET` | Dokploy → pericos → agent → Environment |
| `LITELLM_MASTER_KEY` | Dokploy → Personal → llm → Environment |
| Llave de la service account | el JSON descargado en el paso 1 |

---

## Operación

- **Salud:** `GET /health` del agente. Un `fallidos` que crece es la señal de
  que algo necesita un humano.
- **Logs:** pestaña *Deployments* de cada servicio en Dokploy.
- **Un mensaje que no se guardó:** queda en la tabla `entrantes` con estado
  `fallido` y el motivo en su columna `error` (SQLite, en el volumen `/data`).
- **Cambiar de motor de WhatsApp:** `ENGINE_TYPE` en el Environment de `openwa`
  (`baileys` ↔ `whatsapp-web.js`) + redeploy. `whatsapp-web.js` levanta un
  Chromium por sesión: menos riesgo de bloqueo, ~2 GB de RAM.
- **Dejar de confirmar las notas de voz:** `VOZ_REQUIERE_CONFIRMACION=false` en
  el Environment de `agent`, cuando ya se le tenga confianza a la transcripción.
- **Ajustar el reparto de modelos:** las variables `TIER_*` del agente. El
  gateway cae solo hacia abajo, así que cada una es un techo, no una garantía.

## Limitaciones conocidas del MCP de Dokploy

`DOKPLOY_ENABLED_TAGS` no incluye `compose` ni `mounts`, así que **los volúmenes
del paso 2 y el deploy del servicio `llm` del paso 7 hay que hacerlos a mano en
la interfaz** — no se pueden automatizar desde aquí. Tampoco se puede cambiar el
`appName` después de crear una aplicación (devuelve `500`), que es por qué los
nombres internos son los aleatorios que salen en la tabla de arriba.
