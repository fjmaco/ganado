# Despliegue

> **Ningún secreto vive en este repo** — es público. Las claves están en la
> pestaña *Environment* de cada servicio en Dokploy y nada más.

## Lo que ya está creado

Proyecto **pericos** en Dokploy (`150.136.68.245:3000`), con dos aplicaciones:

| Servicio | Origen | Puerto | `appName` (= DNS interno) |
|---|---|---|---|
| `openwa` | imagen `rmyndharis/openwa:0.10.10` | 2785 | `app-index-cross-platform-matrix-mcd8lk` |
| `agent` | GitHub `fjmaco/ganado`, contexto `/agent` | 8000 | `app-program-1080p-interface-ktj5b6` |

Dentro de la red de Dokploy los servicios se llaman por su `appName`, así que:

- el agente habla con OpenWA en `http://app-index-cross-platform-matrix-mcd8lk:2785`
- OpenWA entrega el webhook en `http://app-program-1080p-interface-ktj5b6:8000/webhook/openwa`

Dashboard de OpenWA (para emparejar el teléfono):
<https://app-index-cross-platform-matrix-mcd8lk-dc4145-150-136-68-245.sslip.io>

---

## 1. Antes de nada: cosas que hay que conseguir

1. **Un SIM/eSIM dedicado.** OpenWA maneja un cliente no oficial de WhatsApp y
   el propio proyecto avisa que las cuentas se restringen. No usar el número
   personal de mi papá.
2. **Service account de Google** con la Sheets API habilitada, y su JSON.
3. **La hoja de cálculo**, compartida como *Editor* con el email de la service
   account (`…@….iam.gserviceaccount.com`). Sin eso, todo da 403.
4. **Los números de caravana reales** del hato, aunque sea aproximados.

---

## 2. Volúmenes (en la interfaz de Dokploy)

Esto **no** se puede hacer por la API con los permisos actuales del MCP, y es
lo más fácil de olvidar y lo más molesto de descubrir.

En cada servicio: **Advanced → Volumes → Add**

| Servicio | Tipo | Ruta en el contenedor | Por qué |
|---|---|---|---|
| `openwa` | Volume | `/app/data` | Ahí vive la sesión de WhatsApp. Sin volumen, cada redeploy obliga a emparejar el teléfono otra vez. |
| `agent` | Volume | `/data` | Ahí vive la cola de mensajes pendientes. Sin volumen, un redeploy a mitad de un pesaje lo pierde. |

---

## 3. Llenar las variables que faltan

En **agent → Environment** hay un bloque marcado `FALTA LLENAR`:

```
ALLOWED_SENDERS=573001112233      # el número de mi papá
ADMIN_WHATSAPP=57300…             # el mío, para alertas técnicas
SHEET_ID=1AbC…                    # lo que va entre /d/ y /edit en la URL
GOOGLE_SA_JSON_B64=…              # base64 -w0 credenciales.json
```

El servicio **no arranca** hasta que las cuatro tengan valor. Es a propósito:
mejor que falle al desplegar y no en silencio a las 6am.

---

## 4. Desplegar y emparejar

1. **Deploy** en `openwa`, esperar a que quede `done`.
2. Abrir el dashboard (link de arriba), entrar con la `API_MASTER_KEY` que está
   en su Environment, y crear una sesión llamada **`pericos`**
   (tiene que coincidir con `OPENWA_SESSION_ID` del agente).
3. Escanear el QR con el teléfono del SIM dedicado. Estado → `ready`.
   *(También existe `POST /api/sessions/pericos/pairing-code` si se prefiere
   teclear un código en vez de escanear.)*
4. **Deploy** en `agent`.

---

## 5. Preparar la hoja y registrar el webhook

Desde una terminal con las variables del agente cargadas (o desde el propio
contenedor):

```bash
cd agent

# Crea las pestañas Registros / Vacas / Tabla y sus fórmulas
python -m scripts.configurar --hoja

# Precarga el hato: cada una queda con su nombre
python -m scripts.configurar --vacas 477,348,312,201,155

# Suscribe el webhook, filtrado al número de mi papá dentro del propio OpenWA
python -m scripts.configurar --webhook http://app-program-1080p-interface-ktj5b6:8000/webhook/openwa

# Estado general
python -m scripts.configurar --revisar
```

> Si el registro del webhook devuelve **400**, casi siempre es la protección
> SSRF de OpenWA: valida la URL *al registrarla* y rechaza direcciones
> privadas. `SSRF_ALLOWED_HOSTS` ya trae el `appName` del agente, así que
> revisar que no se haya cambiado.

---

## 6. Gateway LiteLLM (para las notas de voz)

El gateway sólo servía modelos de chat. En el repo `fjmaco/litellm` se agregó,
**debajo** del marcador `<<< END AUTO-MANAGED MODELS <<<`:

```yaml
  - model_name: transcribe
    litellm_params:
      model: groq/whisper-large-v3
      api_key: os.environ/GROQ_API_KEY
      rpm: 20
    model_info:
      mode: audio_transcription
```

La posición importa: `splice()` en `agent/refresh_models.py` sólo reescribe lo
que está *entre* los marcadores, así que esta entrada sobrevive cada refresco
(el agente sólo descubre modelos de chat, nunca de audio).

Hace falta hacer redeploy del servicio `llm` para que aparezca. Verificar:

```bash
curl -s http://llm.lamhara.co/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  | grep -o transcribe
```

---

## 7. Comprobar que quedó bien

```bash
# El agente vivo y con la cola sana
curl -s https://<dominio-del-agente>/health
# {"estado":"ok","cola":{},"fallidos":0,"worker":true}
```

Desde el teléfono de mi papá:

1. `la vaca 477 pesa 327` → debe llegar el eco con nombre, peso y fecha.
2. `no, eran 445` → la fila se corrige *en el sitio*, no se agrega otra.
3. `borra lo último` → sale de `Tabla` pero la fila queda marcada `anulado`.
4. Un número que no existe → **pregunta** antes de crear nada.
5. Una nota de voz en español → la lee de vuelta antes de escribir.
6. `¿cómo va el hato?` → reporte con cifras calculadas en Python.
7. **Redeploy de `openwa`** → la sesión sobrevive. Esta es la prueba de que el
   volumen del punto 2 quedó bien configurado.

---

## Operación

- Cola y worker: `GET /health`. Un `fallidos` que crece es la señal de que algo
  necesita un humano.
- Logs y estado: pestaña *Deployments* de cada servicio en Dokploy.
- Si a mi papá le llega *«no pude guardar el dato»*, el mensaje quedó en la
  tabla `entrantes` con estado `fallido`; el error está en su columna `error`.
