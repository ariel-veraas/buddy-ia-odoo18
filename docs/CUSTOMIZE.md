# Personalizar y extender Buddy

Dos partes: lo que puede hacer **quien administra Odoo** sin programar, y lo que puede hacer **un desarrollador** para extender el motor.

## Para administradores (sin programar)

Todo se hace en **Ajustes > Buddy IA** (solo administradores) y en el menú **Buddy**. Estos son los ajustes que existen, con el nombre que ves en pantalla.

### Para empezar
Lista que se tilda sola: proveedor de IA (clave y modelo cargados), Google Drive conectado, una carpeta con grupo asignado, documentos leídos y última prueba de conexión correcta. Incluye el botón **Probar conexión** y un acceso a **Carpetas de Drive**.

### Inteligencia artificial
- **Proveedor:** Claude (Anthropic), OpenAI (ChatGPT), Google Gemini u **Otro (compatible con OpenAI)**.
- **Dirección de la API:** solo aparece con "Otro" (por ejemplo Azure, Ollama o un servidor propio); tiene que empezar con `http://` o `https://`.
- **Clave de API:** se pega y se guarda en el servidor; no se vuelve a mostrar. Para cambiarla, pegá la nueva. **Quitar la clave** la borra.
- **Modelo:** el nombre exacto, como lo muestra tu proveedor. Si lo dejás **vacío** se usa el de cada proveedor: `gemini-2.5-flash` con Gemini (el recomendado: la versión «lite» inventa más) y `claude-haiku-4-5-20251001` con Claude; con OpenAI y «Otro» no hay modelo por defecto y tenés que escribirlo. Lo que escribas nunca se reemplaza. **Probar conexión** lista los disponibles en tu cuenta si el que escribiste no existe.

### Google Drive
- **Cuenta de servicio:** pegá el contenido del archivo `.json` (ver [GOOGLE-DRIVE](GOOGLE-DRIVE.md)). Después se muestra el email con el que compartir las carpetas. **Quitar la clave** la borra.
- Las carpetas se cargan en **Buddy > Carpetas de Drive**: nombre, enlace, **grupos que pueden consultarla**, y **Sincronizar automáticamente**.

### Apariencia y textos
- **Nombre del buddy** (por defecto "Buddy") y **Nombre de la empresa** (por defecto "la empresa"; se usa en el saludo y en las instrucciones a la IA).
- **Subtítulo:** frase corta debajo del nombre (hasta 60 caracteres). Vacío = "Tu guía en Odoo".
- **Saludo:** lo que dice el globito la primera vez (hasta 140). Escribí `{nombre}` para incluir el nombre de la persona. Vacío = saludo estándar.
- **Sugerencias iniciales 1 a 4:** preguntas que aparecen como botones al abrir el chat (hasta 80 caracteres cada una). Si las dejás todas vacías se usan las estándar. Poné preguntas reales de tu empresa.
- **Color principal:** formato `#RRGGBB`. Vacío = celeste original. También tiñe a Kitsune y a Mochi (pelaje, hojita, brillos).
- **Personaje:** elegí entre **Mochi** (bolita blanda kawaii con una hojita, que se achata y se estira; es el que viene por defecto) y **Kitsune** (zorrito espíritu estilo anime: orejas que se mueven, cola esponjosa, ojos grandes, marcas en las mejillas). Los dos son personajes sueltos, sin redondel de fondo. ¿Tu empresa tiene su propia mascota? Se suma como un módulo aparte, sin tocar Buddy: ver [Un buddy propio de tu empresa](#un-buddy-propio-de-tu-empresa).
- **Accesorio:** uno fijo que lleva siempre puesto (**bigote, sombrero de mago, lentes de sol, auriculares, gorrito de fiesta**) o **Ninguno**. Calza en todos los personajes.
- **Vista previa:** es el mismo dibujo que ven tus compañeros, y **cambia al elegir, sin guardar** (personaje, accesorio y color). Con los botones probás las 6 expresiones, el saludo y cada gesto.
- **Ocultar el buddy a todos:** apaga el widget para todos sin desinstalar nada. Cada persona también puede ocultarlo para ella.

### Gestos del buddy
Cuando nadie lo usa, el buddy hace gestos de anime (parpadea, mira alrededor, bosteza, se estira, se duerme con burbuja de moco y «Zzz», le sale una gota de sudor o una venita de enojo, ojos con brillitos o de corazón, lagrimitas, mareo con espiral, signos de pregunta o exclamación, y se pone y se saca un bigote, un sombrero de mago o lentes de sol; Kitsune mueve la cola y las orejas y Mochi rebota y se bambolea).
- **Gestos cuando está inactivo:** activado por defecto.
- **Frecuencia:** *Poco* (1 minuto de quietud y un gesto cada 2 a 4 minutos), *Normal* (30 segundos y un gesto cada 45 a 90 segundos; por defecto) o *Mucho* (12 segundos y un gesto cada 15 a 40 segundos). Con el chat abierto solo actúa si pasó al menos 1 minuto sin usarlo.
- **Qué gestos hace:** tres grupos que se tildan por separado: **Expresiones**, **Accesorios** y **Sueño**.
- Nunca hace gestos si la pestaña está oculta, si el chat está respondiendo o escribiendo, ni si la persona pidió «reducir movimiento» en su sistema; apenas alguien lo usa, el gesto se corta. Cada persona sigue pudiendo ocultar el buddy para ella (sin permisos nuevos).
- Los valores se guardan como identificadores permitidos (lista cerrada): lo que llega al navegador nunca es HTML ni estilos escritos por el administrador. Detalle en [ADMIN-GUIDE](ADMIN-GUIDE.md#personaje-accesorio-y-gestos).

### Instrucciones de la empresa
**Cómo tiene que responder tu buddy:** texto libre que se suma a las instrucciones (tono, vocabulario, a quién derivar, qué no responder). La pantalla acepta hasta 1500 caracteres. Se agrega **después** de las reglas fijas, que no puede contradecir (responder solo con los documentos, no inventar, no pedir contraseñas, derivar cuando no sabe). Para enseñarle cosas de tu empresa, lo más sólido es escribirlas en un documento de Drive.

### Privacidad
**Retención del historial (días):** por defecto 30; cada conversación se guarda hasta N días después de su último mensaje y después se borra sola; 0 = no borrar.

### Avanzado (tildar "Mostrar opciones avanzadas")
- **Sincronizar Drive cada (horas):** de 1 a 168; por defecto 24.
- **Pasajes que lee el buddy por pregunta:** de 1 a 10; por defecto 4. Más pasajes: respuestas más completas, más lentas y más caras.
- **Consultas por minuto y por persona** (por defecto 6) y **Consultas por día y por persona** (por defecto 40; antes 150): mínimo 1. El tope de toda la empresa (por defecto 1000 por día), los precios por millón de tokens, «Guardar preguntas», la retención de las estadísticas y el **Responsable de tickets** están en sus propios bloques de Ajustes: ver [ADMIN-GUIDE](ADMIN-GUIDE.md).
- **Consultas para dejar de ser "nuevo":** por defecto 12; mientras una persona haya hecho menos, Buddy le explica con más detalle.

### Parámetros que no están en la pantalla
Se cambian en **Ajustes > Técnico > Parámetros del sistema** (modo desarrollador):

| Parámetro | Para qué | Por defecto |
|---|---|---|
| `buddy_ia.tickets_per_day` | Tope de tickets por persona y día | 20 |
| `buddy_ia.text_min_score` | Puntaje mínimo de la búsqueda de texto (0 a menos de 1); subirlo descarta coincidencias flojas | 0,05 |

### Contactos de derivación
**Buddy > Contactos**: una fila por área con **código** (it, rrhh, ventas, general...), **área**, **nombre o equipo** y **correo**. Buddy elige el código según la pregunta. Editá los de ejemplo (`@ejemplo.test`) antes de usarlo con gente real; dejá siempre uno con código `general` (es el de respaldo); los códigos son únicos y en minúsculas; podés agregar áreas nuevas y la IA las conoce automáticamente.

## Para desarrolladores

Todo lo siguiente está verificado contra `odoo-addons/buddy_ia`.

### Estructura

| Archivo | Qué contiene |
|---|---|
| `controllers/main.py` | Rutas `/buddy/config`, `/buddy/chat`, `/buddy/ticket`, `/buddy/history`, `/buddy/forget` (JSON, `auth="user"`). Validan tipos y delegan en el motor. Mensajes amables por código de error en `_friendly`. |
| `models/buddy_engine.py` | `buddy.engine` (AbstractModel): cuota, sesión, búsqueda, llamada al modelo, tickets, autodiagnóstico. |
| `models/buddy_provider.py` | `buddy.provider` (AbstractModel): llamadas al proveedor, tabla `PROVIDERS`. |
| `models/prompts.py` | Prompt de sistema, prompt de usuario, validación de la salida JSON. |
| `models/buddy_source.py`, `drive_client.py`, `buddy_chunker.py` | Carpetas, cliente de Drive (Python puro, sin Odoo) y troceado. |
| `models/res_config_settings.py` | Ajustes (parámetros con prefijo `buddy_ia.`). |
| `models/buddy_look.py` | Lista cerrada de personajes, accesorios, frecuencias y grupos de gestos (Python puro) y `public_look()`, lo que `/buddy/config` manda al navegador. |
| `static/src/js/buddy_registry.js` | **El registro de JS**: buddys, accesorios, gestos, frecuencias y la limpieza (`cleanLook`) de todo lo que llega del servidor. |
| `static/src/js/buddy_idle.js` | El programador de gestos en reposo (sin DOM; se prueba con relojes falsos). |
| `static/src/js/buddy_avatar.js`, `static/src/xml/buddy_avatar*.xml` | El componente avatar y un template SVG por buddy (`buddy_avatar_kitsune.xml`, `_mochi.xml`), más `buddy_avatar.xml` (contenedor, accesorios y efectos). Los genera `tools/buddy_art.py`. |
| `static/src/js/buddy_look.js`, `static/src/xml/buddy_look.xml` | Widgets de la pantalla de Ajustes: selector de personaje, botones de opciones y vista previa en vivo. |
| `static/src/js/buddy.js`, `buddy.xml`, `scss/buddy.scss`, `scss/buddy_avatar.scss` | Widget OWL del chat y estilos (el segundo es CSS plano con el aro de estado, los accesorios y los gestos). |

### Extender el motor

`buddy.engine` es un `AbstractModel`: se extiende con herencia de Odoo en tu propio módulo (que dependa de `buddy_ia`):

```python
from odoo import api, models

class BuddyEngine(models.AbstractModel):
    _inherit = "buddy.engine"

    @api.model
    def _retrieve(self, message, cfg):
        chunks = super()._retrieve(message, cfg)
        # reordenar, filtrar o agregar fuentes propias
        return chunks
```

| Método | Para qué | Contrato |
|---|---|---|
| `_cfg()` | Leer y validar la configuración | Devuelve un dict (`chat_model`, `top_k`, cuotas, `buddy_name`, `company`, `extra_instructions`, `text_min_score`, `synonyms`, `timeout`; `top_k` vale 6 por defecto); lanza `BuddyBusy("not_configured")` si es inválida. |
| `_retrieve(message, cfg)` | Cambiar cómo se buscan los fragmentos (por ejemplo, agregar búsqueda semántica) | Lista de dicts `{id, doc_id, title, owner, link, updated, text, score, sequence, neighbor}` (los fragmentos mejores y sus vecinos, en orden de lectura; puede ser `[]`). **Debe respetar los permisos del usuario**: leer con el ORM sin `sudo()`. |
| `_catalog()` | Qué títulos conoce el buddy para charlar y para "¿sobre qué me podés guiar?" | `{folders, titles, total}` con lo que el usuario puede leer (hasta 40 títulos). Son datos no confiables: el prompt los escapa. |
| `_ask_model(message, history, profile, chunks, cfg, catalog=None)` | Cambiar la llamada al modelo (se llama siempre una vez, aunque `chunks` esté vacío) | Dict ya validado con `prompts.parse_model_json`. Los errores se traducen a `BuddyBusy`. |
| `_history(cfg, turns=6)` | Qué historial se envía | Lista de `{role, content}`. |
| `_contact_for(role)` | Cómo se elige el contacto | Dict `{role, label, name, contact}` o `None`. |
| `_used_sources(chunks, used)` | Cuáles de los fragmentos recuperados se muestran como fuentes | `used` es la lista de ids de `<doc>` que dijo usar el modelo (`None` si no la dio: se muestran todos los recuperados; `[]`: ninguno). Devuelve lo mismo que `_dedupe_sources`. |
| `_dedupe_sources(chunks)` | Cómo se arman las fuentes (una por documento) | Lista de `{title, owner, updated, link}`. Mantené `safe_drive_link` para el enlace. |
| `_self_test()` | Qué chequea "Probar conexión" | `(ok, líneas)`. |

Reglas para no romper nada:
- **No uses `sudo()` para leer documentos o fragmentos**: los permisos por grupo dependen de las reglas de registro (`security/buddy_security.xml`).
- **`BuddyBusy(code)`** es lo que el controlador traduce a mensajes amables (`_friendly` en `controllers/main.py`). Un código nuevo necesita su mensaje allí.
- No dejes bloqueos ni transacciones abiertos durante la llamada al proveedor.
- Cambios en la búsqueda, el prompt o la validación deberían llevar pruebas en `tests/` (`python tools/manage.py validate`).

### Cambiar el prompt

`models/prompts.py`: `SYSTEM_PROMPT` define identidad, reglas y formato; `build_user_prompt` arma el contexto. El contrato de salida (`answer`, `expression`, `needs_human`, `contact_role`, `ticket`, `sources_used`) lo valida `validate_model_output`: `answer`, `expression` y `needs_human` son obligatorios; los demás toman su valor por defecto si faltan y las claves que no son del contrato se ignoran. Si lo cambiás hay que cambiar prompt, validador, controlador y widget juntos. El prompt no usa la palabra «Sos» antes del nombre del buddy (el modelo llegó a presentarse como «Sos <nombre>») y la novedad de la persona viaja como una nota privada, no como bandera. **Mantené el escape de `<`, `>`, `&` y `"` (`_inert`) y las etiquetas `<doc>`/`<pregunta>`**: son la defensa contra prompt injection. Expresiones válidas del avatar: `neutral, happy, thinking, surprised, doubt, wink`.

**Orden de las claves y streaming:** el prompt pide `answer` PRIMERA. No es obligatorio para que ande (si viene después, el texto aparece más tarde y la validación final es la misma), pero si lo cambiás el efecto de "escribiendo" se pierde. Si agregás claves al contrato, hay que cambiar también `validate_model_output`; el extractor (`buddy_stream.AnswerExtractor`) solo mira `answer` y salta el resto.

### Proveedores de IA

`buddy.provider` habla con dos protocolos, definidos en `PROVIDERS` (`clave -> (nombre, dirección base, protocolo)`):

- `anthropic`: API nativa de Claude (`POST /messages`, cabeceras `x-api-key` y `anthropic-version`).
- `openai`: `POST /chat/completions` con `Authorization: Bearer`, usado por OpenAI, Gemini (su endpoint compatible) y "Otro".

Cualquier servidor que hable la API de OpenAI sirve con **Otro** y su dirección, sin programar. Para un protocolo distinto, sobrescribí `buddy.provider._chat(model, messages, timeout, max_tokens, temperature)` (devuelve el texto de la respuesta; usa roles `system`/`user`/`assistant`) y, si hace falta, `_list_models`. Para el streaming, sobrescribí también `_chat_stream(model, messages, timeout, max_tokens, temperature, settings)`: un **generador** de fragmentos de texto que respete el plazo total y termine solo si el proveedor cerró la respuesta (si no, `ValueError`). Respetá el `timeout` recibido y **no agregues reintentos automáticos** (duplicarían el gasto). `_call` no sigue redirecciones ni deja pasar respuestas de más de 2 MB: mantené esas defensas. Ojo: la lista de proveedores de la pantalla se arma al cargar el módulo a partir de `PROVIDERS`, así que agregar una opción nueva a la lista requiere cambiar ese diccionario en `buddy_provider.py`.

### Fuentes de documentos

`buddy.source` es Drive. El motor solo lee `buddy.chunk`, así que otra fuente (carpeta de red, wiki) puede cargar `buddy.document` y `buddy.chunk` y heredar las mismas reglas de acceso, siempre asignando una carpeta (`buddy.source`) con grupos. `buddy_chunker.split_text` trocea un texto en fragmentos válidos. `drive_client.DriveClient` no importa Odoo y se puede probar solo.

### Widget

El envío usa `fetch` contra `/buddy/chat_stream` y lee el `ReadableStream` con `SSEParser`; el respaldo es `/buddy/chat` (mismo `request_id`). Si cambiás los eventos del servidor (`delta`, `done`, `error`), cambiá `onStreamEvent`. El proveedor simulado de la demo (`demo/stub_llm_server.py`) acepta `STUB_STREAM_THINK`, `STUB_STREAM_DELAY` y `STUB_STREAM_PIECE` para ajustar el ritmo.

Componente OWL registrado en los componentes principales (`static/src/js/buddy.js`, plantilla `static/src/xml/buddy.xml`, estilos `static/src/scss/buddy.scss`). Se pueden heredar plantilla y estilos desde otro módulo con los mecanismos normales de assets de Odoo 18. El texto del modelo se dibuja con `t-esc` (nunca como HTML): mantenelo si agregás contenido que viene del servidor.

### Agregar un buddy nuevo
Esto es para sumar un buddy **al propio Buddy** (a un fork). Para la mascota de una empresa conviene un módulo aparte: ver [Un buddy propio de tu empresa](#un-buddy-propio-de-tu-empresa).

Un buddy son tres cosas: un dibujo, una entrada en el registro de JS y su id en la lista cerrada de Python.

1. **Dibujo.** Un template OWL `buddy_ia.Avatar.<id>` en `static/src/xml/buddy_avatar_<id>.xml` (agregalo a `assets` en `__manifest__.py`). Copiá `buddy_avatar_mochi.xml` como punto de partida (o agregá una función en `tools/buddy_art.py`, que escribe los dos de fábrica: **ese script pisa `buddy_avatar_kitsune/mochi.xml`, no los tuyos**). Reglas: la raíz es `<g xmlns="http://www.w3.org/2000/svg">` (sin ese `xmlns`, OWL crea los nodos de un `t-call` con el espacio de nombres equivocado y no se ven); lienzo de 200 × 260 con la cabeza cerca de (100, 84); los colores salen de variables CSS (`--bd-primary` y las `--bd-fur…`/`--bd-mochi…` que deriva `themeVars` en `buddy.js`), nunca de un color escrito por el administrador; los ids de gradientes llevan `{{uid}}`; un bloque de cara por cada nombre de `FACES` (`t-if="face === '…'"`): las 6 expresiones del contrato con la IA (`neutral, happy, thinking, surprised, doubt, wink`) más las caras de los gestos; dentro del personaje, `<t t-call="buddy_ia.AvatarAccessories"/>`, y al final `<t t-call="buddy_ia.AvatarFx"/>`. Grupos con nombre fijo, que usan el CSS y los gestos: `bd-char` (todo el personaje), `bd-look` > `bd-eyes` + `bd-brows`, `bd-mouth`, `bd-arm` (el que saluda), `bd-ring` (solo si tiene disco) o `bd-status` (el indicador de estado de los personajes sueltos).
2. **Registro de JS.** En `static/src/js/buddy_registry.js`, `BUDDIES.<id> = { template, disc, view, anchors, acc }`: `disc` (true solo si la cabeza va en un disco oscuro con aro, como un logo redondo), `view` (viewBox de la cabeza sola y del cuerpo entero), `anchors` (dónde van el signo de pregunta, la gota, los Zzz...) y `acc` (dónde va cada accesorio).
3. **Lista cerrada de Python.** `CHARACTERS` en `models/buddy_look.py`: `("<id>", "Nombre que ve el administrador")`. Hay una prueba que falla si la lista de Python y la de JS no coinciden.
4. **Cierre:** correr los tests del widget (`make test`: la prueba del avatar recorre todos los buddys del registro, sus 14 caras y sus accesorios), regenerar `i18n/buddy_ia.pot` (el nombre es un texto traducible) y mirarlo en la vista previa de Ajustes.

### Un buddy propio de tu empresa
Si una empresa quiere **su** mascota (o la del cliente), no hace falta tocar ni «forkear» `buddy_ia`: el personaje va en un **módulo aparte** que depende de `buddy_ia`. Así se actualiza Buddy sin perder la mascota, y el dibujo (que suele ser propiedad de la empresa) queda fuera del repositorio público. Hay una plantilla lista en [`examples/buddy_ia_mascot_example`](../examples/buddy_ia_mascot_example): copiala, renombrala y cambiá el dibujo.

**Qué lleva el módulo** (cuatro archivos chicos, además del `__init__`):

| Archivo | Qué hace |
|---|---|
| `models/buddy_character.py` | `buddy_look.register_character("mi_mascota", "Nombre visible")` (lista blanca del servidor) y `buddy_character = fields.Selection(selection_add=[("mi_mascota", "Nombre visible")])` en `res.config.settings` (para que aparezca en Ajustes). |
| `static/src/xml/buddy_avatar_mi_mascota.xml` | El dibujo: un template OWL `buddy_ia.Avatar.mi_mascota` con las reglas de [Agregar un buddy nuevo](#agregar-un-buddy-nuevo) (lienzo 200 × 260, 14 caras, colores por variables CSS, ids con `{{uid}}`). |
| `static/src/js/mi_mascota.js` | `import { BUDDIES, GESTURES } from "@buddy_ia/js/buddy_registry";` y `BUDDIES.mi_mascota = { template, disc, view, anchors, acc }`. Si quiere un gesto propio: `GESTURES.mi_gesto = { group, ms, weight, buddies: ["mi_mascota"] }` y su animación `.g-mi_gesto` en un `.scss`. |
| `__manifest__.py` | `depends: ["buddy_ia"]` y los tres archivos de arriba en `assets` > `web.assets_backend`. |

El `id` es solo minúsculas, números y `_` (hasta 32 caracteres, empieza con letra): `register_character` lo rechaza si no.

**Instalar:** poné la carpeta del módulo junto a `buddy_ia` (mismo `addons_path`), actualizá la lista de aplicaciones, instalá el módulo, andá a **Ajustes > Buddy IA > Apariencia** y elegí el personaje (si querés, cambiá también el nombre del buddy). Recargá el navegador.

**Qué pasa si se desinstala o falta:** el valor guardado ya no está en la lista blanca y el buddy vuelve solo al personaje por defecto (Mochi); no se rompe nada. El módulo no toca el motor, los permisos ni el contrato con la IA.

**Cómo se verifica:** instalar el módulo en una base de prueba; `/buddy/config` tiene que devolver `"character": "<tu id>"` y la vista previa de Ajustes tiene que mostrar la tarjeta con tu dibujo y sus 14 caras.

#### Caso: la mascota de una empresa («Pi»)
La versión original de este proyecto traía a **Pi**, un orbe que es la mascota de la empresa para la que se hizo. Para publicar el proyecto se sacó del repositorio y quedó como un módulo privado con exactamente la estructura de arriba (`buddy_ia_pi`: registra el id `pi`, un dibujo con disco y aro, y un gesto propio de girar el aro). Para volver a tenerlo en la instancia de la empresa: copiar esa carpeta junto a `buddy_ia`, instalarla y elegir **Pi** en Ajustes. **No se commitea al repositorio público**: el arte es de la empresa.

### Agregar un accesorio o un gesto
- **Accesorio:** dibujo en `accessories_template()` de `tools/buddy_art.py` (espacio canónico: cabeza de radio ~50, ojos separados 48), entrada en `ACCESSORIES` (`slot` y `place`), su posición en el `acc` de cada buddy, y su id en `ACCESSORIES` de `models/buddy_look.py`.
- **Gesto:** una entrada en `GESTURES` (`group`, `ms`, `weight`; opcionales `face`, `fx`, `accessory`, `buddies` para variantes de un solo personaje); las animaciones en `static/src/scss/buddy_avatar.scss` bajo la clase `.g-<id>` (solo `transform` y `opacity`, y que duren menos que `ms`); su texto en `gestureLabels()` de `buddy_look.js`. Si necesita una cara nueva, agregala a `FACES` (registro de JS y `tools/buddy_art.py`) y dibujala en cada buddy; si necesita un efecto, agregalo a `fx_template()` y un ancla en `anchors`. Un grupo nuevo necesita su id en `GESTURE_GROUPS` (JS) y en `GESTURE_GROUPS` (Python), y un campo en Ajustes.
- Regenerar los templates: `python tools/buddy_art.py`.

### Tickets

`buddy.ticket` tiene un `create()` que valida y aplica el tope diario. Si lo extendés (por ejemplo para avisar por correo), llamá a `super()` y mantené esas validaciones. Hoy no usa `mail.thread` (está en el [ROADMAP](ROADMAP.md)).

### Pruebas

`python tools/manage.py validate` corre las pruebas nativas de `buddy_ia` en una base `buddy_test` aislada. Para el widget: `make test`.
