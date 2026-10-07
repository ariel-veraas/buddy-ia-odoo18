# Instalar Buddy en tu Odoo 18 Community

Esta guía es para sumar Buddy a un **Odoo 18 Community que ya tenés funcionando, en un servidor Linux, sin Docker**. Si solo querés probarlo en tu compu, andá al [README](../README.md): con `python tools/manage.py demo` queda andando solo.

Buddy es **un solo módulo (`buddy_ia`)** que depende únicamente de `web` y `mail` (los dos vienen con Odoo). Se instala como cualquier addon.

> **No hace falta pgvector ni ninguna extensión de PostgreSQL.** La búsqueda usa la búsqueda de texto que ya trae Postgres (configuración `spanish`), sin extensiones, sin superusuario y sin DBA.

## ¿Mi Odoo puede? (checklist previo)

- [ ] Es **Odoo 18 Community** instalado por vos en un servidor (no hosteado por Odoo). **Odoo Online no sirve** (no admite addons propios). Odoo.sh y Enterprise no se probaron.
- [ ] Tenés **acceso al servidor** (SSH, poder editar `odoo.conf` y reiniciar el servicio) y a `pip` del entorno Python que usa Odoo.
- [ ] PostgreSQL 12 o superior (Odoo 18 ya lo exige). El módulo crea una columna calculada y un índice de texto sobre su propia tabla; no pide permisos especiales.
- [ ] El servidor puede **salir a internet por HTTPS** hacia `oauth2.googleapis.com`, `www.googleapis.com` y el sitio del proveedor de IA que elijas (por ejemplo `api.anthropic.com`).
- [ ] La **fecha y hora del servidor están correctas** (Google rechaza la clave si el reloj está muy corrido).
- [ ] Una cuenta de Google y una clave de API de un proveedor de IA. Podés instalar todo primero y configurarlo después.
- [ ] Probás primero en un **staging** (copia de tu Odoo) y tenés **backup** de la base y del filestore.

## Paso a paso

Los comandos asumen Odoo en `/opt/odoo`, usuario de sistema `odoo`, configuración en `/etc/odoo/odoo.conf` y base `MIBASE`. Ajustalos a tu servidor.

### 1. Backup

```text
sudo -u postgres pg_dump -Fc MIBASE > MIBASE-antes-de-buddy.dump
tar czf filestore-antes-de-buddy.tgz /var/lib/odoo/filestore/MIBASE
```

### 2. Copiar el módulo

Copiá la carpeta `odoo-addons/buddy_ia` a una carpeta que ya esté en el `addons_path` de tu Odoo (o a una nueva, por ejemplo `/opt/buddy/addons/`, que agregás al `addons_path` en `odoo.conf`):

```text
addons_path = ...lo_que_ya_tenias...,/opt/buddy/addons
```

La carpeta tiene que quedar como `/opt/buddy/addons/buddy_ia/__manifest__.py` y ser legible por el usuario `odoo`.

### 3. Instalar las dependencias de Python

Hay que instalarlas en el **mismo entorno Python que usa Odoo** (si Odoo corre en un virtualenv, activalo antes). Son tres: **pypdf** (leer PDF), **PyJWT** (firmar el acceso a Google) y **packaging**. `requests` y `cryptography` ya vienen con Odoo.

```text
pip install -r odoo-addons/requirements.lock
```

- El archivo `requirements.lock` tiene las versiones con las que se probó (Python 3.12). Con otra versión de Python puede que alguna no tenga paquete precompilado: probalo en staging.
- En **Debian/Ubuntu**, si `pip` falla con "Cannot uninstall ..." o "no RECORD file", agregá `--ignore-installed`: `pip install --ignore-installed -r odoo-addons/requirements.lock`. Si usás el Python del sistema, `pip` puede pedir además `--break-system-packages`; lo más prolijo es un virtualenv.
- Sin pypdf el módulo se instala igual, pero **los PDF quedan "Con error"** al sincronizar. Sin PyJWT no se puede hablar con Google Drive. Sobre la licencia de pypdf mirá [NOTICE](../NOTICE.md).

### 4. Instalar el módulo

**Desde la pantalla** (lo más fácil): reiniciá el servicio de Odoo, entrá como administrador, activá el **modo desarrollador**, andá a **Aplicaciones**, apretá **Actualizar lista de aplicaciones**, buscá "Buddy IA" (sacá el filtro "Aplicaciones" si no aparece) y apretá **Activar**.

**Por línea de comandos**:

```text
sudo -u odoo odoo -c /etc/odoo/odoo.conf -d MIBASE -i buddy_ia --without-demo=all --stop-after-init
sudo systemctl restart odoo
```

Qué se instala: los modelos de Buddy, el menú **Buddy** (Tickets, Carpetas de Drive, Contactos), la sección **Buddy IA** en Ajustes, el widget flotante para usuarios internos y **tres tareas programadas**:

| Tarea | Frecuencia | Para qué |
|---|---|---|
| Buddy: sincronizar carpetas de Drive | cada 30 minutos | Despierta seguido, pero cada carpeta decide si le toca (por defecto, una vez por día) |
| Buddy: borrar conversaciones vencidas | 1 vez por día | Retención del historial (30 días por defecto) |
| Buddy: limpiar reservas de consumo vencidas | 1 vez por día | Mantenimiento de las cuotas |

> Las tareas programadas solo corren con Odoo funcionando normalmente. Si arrancaste con `--max-cron-threads=0`, o solo con `--stop-after-init`, **no se sincroniza nada** (el botón "Sincronizar ahora" tampoco).

Revisá una cosa de seguridad: en **Ajustes > Usuarios y compañías** la "Invitación de usuarios" (registro público) tiene que estar en **por invitación**, no en "registro libre". En un Odoo existente **no tocamos tu configuración**: revisalo vos.

### 5. Configurar

Todo se hace en **Ajustes > Buddy IA** (solo administradores). Arriba hay una lista **"Para empezar"** de 6 pasos que se tilda sola (proveedor de IA, Drive, carpeta con grupo, documentos leídos, contactos con correo real y conexión probada):

1. **Google Drive:** crear la cuenta de servicio, **subir su archivo `.json`** (o pegar su contenido) y conectar carpetas. Es lo más largo la primera vez: [GOOGLE-DRIVE.md](GOOGLE-DRIVE.md); una guía corta también está en la propia pantalla de Ajustes (bloque «Google Drive»). Después de guardar, Ajustes te muestra el **correo de la cuenta de servicio con un botón para copiarlo** (el mismo aparece en cada carpeta): con ese correo se comparte cada carpeta de Drive.
2. **Proveedor de IA:** elegí Claude (Anthropic), OpenAI, Google Gemini u "Otro (compatible con OpenAI)" (en ese caso aparece el campo "Dirección de la API"). Pegá la **clave de API** (se guarda en el servidor y no se vuelve a mostrar) y, solo con OpenAI u «Otro», escribí el **nombre del modelo** tal como lo muestra tu proveedor (con Gemini se usa `gemini-2.5-flash` y con Claude `claude-haiku-4-5-20251001` si dejás el cuadro vacío; con Gemini evitá la versión «lite», que inventa más). Si no sabés cuál, probá la conexión: el mensaje te lista los modelos disponibles.
3. **Contactos:** menú **Buddy > Contactos**. Vienen de ejemplo con correos `@ejemplo.test`: **reemplazalos por los reales antes de abrirlo a la gente** (o borrá los que no uses). El paso 5 de «Para empezar» se tilda solo cuando no queda ningún correo de ejemplo (`.test`, `.example`, `example.com`, `ejemplo.*`). Dejá siempre uno con código `general`.
4. **Responsable de tickets** (opcional pero recomendado): bloque «Tickets» de Ajustes. Esa persona recibe una tarea cuando se crea un ticket y obtiene el permiso «Gestor de Buddy».
5. **Búsqueda por significado** (opcional, apagada por defecto): bloque «Búsqueda por significado». Entiende preguntas que usan otras palabras que el documento, pero para eso manda al proveedor el texto de **todos** los documentos de **todas** las carpetas (también las restringidas): leé el aviso de la pantalla y la [guía de administración](ADMIN-GUIDE.md#búsqueda-por-significado-opcional) antes de activarla. Con Claude no está disponible. No necesita instalar nada más (ni pgvector ni librerías).
6. **Probar conexión:** consulta de verdad al proveedor de IA y **lista los modelos que tu cuenta puede usar, aunque el cuadro «Modelo» esté vacío** (así encontrás el nombre exacto sin salir de Odoo), hace una pregunta de prueba y revisa que haya clave de Drive y que cada carpeta esté sincronizada, con grupos y con documentos. El resultado **queda escrito en «Para empezar»** (paso 6), línea por línea, aunque la notificación desaparezca. **No se conecta a Drive en ese momento**: el estado de las carpetas sale de la última sincronización. Hacelo siempre que cambies algo.

Mientras algo falte, el widget le dice a la gente «Todavía no estoy configurado. Avisale a tu administrador» en vez de fallar en la primera pregunta.

Después, para organizar las carpetas y evaluar la calidad: [ADMIN-GUIDE.md](ADMIN-GUIDE.md).

### Streaming de las respuestas: workers y proxy

Las respuestas se muestran a medida que se escriben (`POST /buddy/chat_stream`, Server-Sent Events). Dos cosas para tener en cuenta:

- **Cada respuesta en curso ocupa un worker de Odoo hasta que termina** (como máximo unos 35 segundos, igual que antes ocupaba la llamada sin stream). Dimensioná `workers` pensando en consultas simultáneas, no solo en páginas por segundo.
- **Proxy:** nginx guarda las respuestas en un buffer por defecto y el texto llegaría de golpe. La respuesta ya manda `X-Accel-Buffering: no`, que nginx respeta; si tu configuración lo ignora, agregá para esa ruta:

```nginx
location /buddy/chat_stream {
    proxy_pass http://odoo;          # el mismo upstream que usás para Odoo
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 60s;          # un poco más que los 35 s del plazo de la consulta
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Host $host;
}
```

Con otros proxies, buscá "desactivar buffering" (Caddy envía los eventos sin buffer con `flush_interval -1`; Apache: `flushpackets=on`). Si el proxy no permite streaming o corta la conexión antes de enviar nada, el widget reintenta solo por la ruta sin stream: no se rompe, solo no se ve el efecto. Una CDN o WAF que cachee o recomprima respuestas tampoco debe tocar esa ruta.

### Votos 👍/👎 (`POST /buddy/feedback`)

Es una ruta común de Odoo: JSON de menos de 1 KB, sin streaming ni nada especial para el proxy. Dos recomendaciones: dejá un límite de pedidos por IP en el proxy para `/buddy/*` (la ruta ya limita por persona, pero un código inventado igual llega hasta Odoo, aunque cuesta una sola consulta) y, si tu proxy cambia el tope de cuerpo, que no sea menor a 1 KB para esa ruta.

### Onboarding (`POST /buddy/journey` y `POST /buddy/journey/step`)

Son rutas comunes de Odoo: JSON de menos de 256 bytes, sin streaming ni nada especial para el proxy (valen las mismas recomendaciones que para los votos: un límite de pedidos por IP para `/buddy/*`). El módulo crea solo un **cron diario** («Buddy: onboarding (asignación automática y recordatorios)», a las 11:00 UTC) que asigna los recorridos «solo a las personas nuevas» y crea o cierra las actividades de recordatorio; necesita que el cron de Odoo esté andando (con `workers = 0` corre dentro del proceso web; con workers, en el proceso de cron). Si tu Odoo no tiene un servidor de correo saliente, los recordatorios igual aparecen en la bandeja de actividades de Odoo (solo no llega el aviso por correo).

## Actualizar

Se actualiza **sobre la misma base**: no hace falta desinstalar ni reinstalar, y no se pierden las carpetas, los documentos leídos, los tickets, los contactos ni los ajustes (los contactos de ejemplo se cargan una sola vez; las claves y la configuración se conservan).

1. **Backup** de la base y del filestore (paso 1). Probá en una copia si podés, sobre todo en la primera actualización.
2. **Leé el [CHANGELOG](CHANGELOG.md)** de las versiones que te saltás: dice si hay algún paso extra y qué cambia.
3. Reemplazá la carpeta `buddy_ia` por la versión nueva (con una copia de la anterior a mano) y repetí el paso 3 de la instalación si cambió `requirements.lock`.
4. Actualizá el módulo **con el servicio detenido o sin tráfico** (así los procesos viejos no siguen con el código anterior):

```text
sudo systemctl stop odoo
sudo -u odoo odoo -c /etc/odoo/odoo.conf -d MIBASE -u buddy_ia --stop-after-init
sudo systemctl start odoo
```

   **Con Docker, el orden exacto importa** (el módulo viaja DENTRO de la imagen: una imagen nueva sobre una base vieja arranca con el código nuevo y la base sin actualizar, y falla —columnas, vistas o grupos que faltan—):

   1. **Backup** de la base y del volumen de datos (paso 1).
   2. `docker compose build odoo` (construye la imagen con el módulo nuevo; todavía no cambia nada en marcha).
   3. `docker compose stop odoo` (el servicio web, no la base).
   4. `docker compose run --rm --no-deps odoo odoo -d MIBASE -u buddy_ia --stop-after-init` (actualiza la base con la imagen nueva; mirá el final del registro: no tiene que haber errores).
   5. Recién ahora `docker compose up -d odoo`.

   **No corras `docker compose up -d` con la imagen nueva antes del paso 4** (lo recrea y lo arranca ya con el código nuevo sobre la base vieja). **No uses `down -v`**: borra la base. Si ya arrancó así, `stop odoo` y seguí desde el paso 4: la actualización se puede correr después.
5. Pedile a cada persona que **recargue el navegador** (Ctrl+F5): el widget se sirve como archivo estático y, si no, puede quedar la versión anterior en caché.
6. Entrá a **Ajustes > Buddy IA** y corré **Probar conexión**. Revisá también las carpetas: las que tengan documentos truncados o con error lo dicen en su lista.

**Si algo sale mal:** restaurá el backup y volvé a poner la carpeta anterior del módulo. El backup es la única vuelta atrás garantizada.

**Desde 18.0.5.4.0:** Odoo crea solo la tabla de resúmenes semanales (vacía), un tipo de actividad y el cron de los lunes; **no hay migración ni paso manual**. Las sugerencias a medida, el aviso de tareas y el resumen semanal nacen **encendidos** (Ajustes > Buddy IA > «Sugerencias, avisos y resumen semanal»); nada de eso llama al proveedor de IA. Recargá el navegador (Ctrl+F5).

**Desde 18.0.5.3.0:** Odoo crea solo las tablas del onboarding (recorridos, pasos, asignaciones y avance, vacías), el grupo **Responsable de onboarding**, el tipo de actividad «Onboarding», el cron diario y un recorrido de ejemplo **archivado** («Ejemplo: primeros 30 días»); **no hay migración ni paso manual** y no se asigna nada a nadie. Los recordatorios nacen encendidos (solo se mandan si hay recorridos asignados) y el contexto de etapa para el modelo, apagado. Para dar acceso a RR.HH.: Ajustes > Usuarios > la persona > «Buddy IA» > «Responsable de onboarding». Como siempre, recargá el navegador (Ctrl+F5): hay archivos de widget nuevos.

**Desde 18.0.5.1.0:** Odoo crea solo la tabla de vectores (`buddy.embedding`, vacía) y dos columnas de estadística en `buddy.insight`; **no hay migración ni paso manual**, y la **búsqueda por significado queda apagada** (no se manda nada nuevo al proveedor) hasta que un administrador la active en Ajustes. No cambian `requirements.txt` ni la imagen.

**Desde 18.0.4.7.0 (leer si venís de 18.0.4.4.0 a 18.0.4.6.0): rotá la clave de API del proveedor de IA.** En esas versiones cualquier empleado podía leerla llamando al motor por la ruta genérica de Odoo (detalle en [SECURITY](SECURITY.md#qué-se-puede-llamar-desde-el-navegador-rpc--y-la-rotación-de-claves-de-18070)). Creá una clave nueva en el proveedor, cargala en **Ajustes > Buddy IA** y revocá la vieja. La migración corrige las estadísticas viejas (quitan al empleado de `create_uid`/`write_uid`), «Administración» deja de implicar el grupo Gestor, y aparece el menú **Buddy > Configuración**. Pedí Ctrl+F5.

**Desde 18.0.4.6.0:** Odoo crea solo la tabla de estadísticas (`buddy.insight`), el grupo **Gestor de Buddy** (en esa versión lo recibían los administradores por implicación; desde 18.0.4.7.0 ya no) y el proceso diario que borra estadísticas vencidas; los tickets pasan a tener chatter y actividades (los existentes siguen igual, sin responsable). Un paso de migración baja el tope por persona **de 150 a 40 consultas por día solo si estaba en el valor anterior por defecto** (un valor que elegiste vos se respeta). Los **administradores empiezan una conversación nueva una vez** (reciben un grupo y la huella de permisos cambia). Después de actualizar: pedí **Ctrl+F5** a todos, elegí el **Responsable de tickets**, revisá el paso 5 «Contactos» y mirá **Buddy > Uso y preguntas** al cabo de unos días. **El tope de la empresa por día es 1000 consultas**: subilo si tu equipo es grande. Nuevo en 18.0.4.6.0: **no se necesita ninguna dependencia nueva**.

**Desde 18.0.4.5.0:** no cambia el esquema de la base ni hace falta ningún paso manual. Si el cuadro **Modelo** quedó vacío, ahora se usa el modelo por defecto del proveedor (Gemini: `gemini-2.5-flash`; Claude: `claude-haiku-4-5-20251001`); si ya tenías uno escrito, se respeta tal cual (si es `gemini-2.5-flash-lite`, considerá pasarlo a `gemini-2.5-flash`). Las respuestas dejan de empezar con «¡Hola!» salvo que la persona salude, y las fuentes que se muestran son solo los documentos que la IA usó.

**Desde 18.0.4.4.0:** Buddy agrega a cada carpeta una marca del archivo en proceso y a cada documento un indicador de «procesamiento interrumpido» (Odoo crea esas columnas solo, sin tocar lo que ya hay); los campos técnicos de los documentos pasan a ser visibles solo para administradores; aparece el ajuste **Capacidad > Consultas simultáneas** (3 por defecto); y las carpetas nuevas empiezan a sincronizarse solas si la clave de Drive ya está cargada. No requiere pasos manuales.

**Viniendo de una versión 18.0.3.x** (la que usaba odoo-llm y pgvector): la arquitectura cambió por completo y **no hay migración de datos**. Desinstalá esa versión y sus módulos auxiliares en una copia, o instalá `buddy_ia` 18.0.4.0.0 en una base nueva, y cargá las carpetas de Drive de cero. Esa ruta de actualización no se probó.

## Desinstalar

1. Backup. Al desinstalar `buddy_ia` **se borran** los tickets, las carpetas, los documentos y los fragmentos leídos. Exportá los tickets antes si los necesitás.
2. En **Ajustes > Buddy IA**, apretá **Quitar la clave** en la clave de API y en la de Drive.
3. En **Aplicaciones**, desinstalá "Buddy IA".
4. Desde 18.0.4.4.0, al desinstalar se **borran solos** `buddy_ia.api_key` y `buddy_ia.drive_key`. El resto de los parámetros `buddy_ia.*` (nombre, colores, instrucciones...) queda: si no los querés, borralos a mano en **Ajustes > Técnico > Parámetros del sistema** (modo desarrollador). Los backups hechos antes todavía contienen las claves.
5. En Google Cloud, **borrá la clave** de la cuenta de servicio (o la cuenta entera) y, si querés, dejá de compartir las carpetas. En el proveedor de IA, revocá la clave de API.
6. Sacá la carpeta del `addons_path` y reiniciá.

## Backup y restauración

Todo lo que importa está en la **base de datos**: documentos leídos, fragmentos, tickets y ajustes, **incluidas las claves de API y de Drive** (guardadas en parámetros del sistema, sin cifrar a nivel de base). Tratá los backups como datos sensibles. Restaurar en otro servidor no requiere nada especial (no hay extensiones): instalá las dependencias del paso 3 y listo. Después de restaurar en otro entorno, **no dejes dos Odoo sincronizando las mismas carpetas con la misma clave** sin necesitarlo, y volvé a sincronizar.

## Si algo falla

| Síntoma | Qué pasa y qué hacer |
|---|---|
| No aparece "Buddy IA" en Aplicaciones | La carpeta no está en el `addons_path`, o no actualizaste la lista (modo desarrollador > Actualizar lista de aplicaciones), o el usuario `odoo` no la puede leer. Revisá el log de Odoo. |
| Al sincronizar: "Falta la librería pypdf en el servidor" o "No pude firmar con la clave de la cuenta de servicio" (con una clave que sabés que es buena) | Faltan dependencias en el Python que usa Odoo (las importa recién al usarlas, no al instalar). Repetí el paso 3 **en ese mismo entorno** y reiniciá el servicio. |
| `pip` falla con "Cannot uninstall ..." o "no RECORD file" | Usá `--ignore-installed` (paso 3). |
| Error de `tsvector` / `spanish` al instalar | Es muy raro: tu PostgreSQL no trae la configuración de texto `spanish` o es anterior a la 12. Revisá la versión (`SELECT version();`). |
| "Primero cargá la clave de Google Drive en Ajustes" | Falta pegar la clave JSON. Ver [GOOGLE-DRIVE.md](GOOGLE-DRIVE.md). |
| **Google no me deja crear la clave** ("Service account key creation is disabled") | **Aviso importante:** las organizaciones nuevas de Google Workspace/Cloud traen bloqueada por defecto la creación de claves de cuenta de servicio (política `iam.disableServiceAccountKeyCreation` / `constraints/iam.managed.disableServiceAccountKeyCreation`). Alguien con el rol *Administrador de políticas de la organización* tiene que desactivarla para el proyecto (Google Cloud > IAM y administración > Políticas de la organización > "Inhabilitar la creación de claves de cuenta de servicio" > Administrar política > no aplicada), o usá un proyecto fuera de esa política. Con Gmail personal no pasa. El nombre exacto de los menús puede variar. Detalle en [GOOGLE-DRIVE.md](GOOGLE-DRIVE.md#5-creá-y-descargá-la-clave-json). |
| La carpeta queda "Con error" | Leé el mensaje en rojo de la carpeta; los más comunes están explicados en [GOOGLE-DRIVE.md](GOOGLE-DRIVE.md#errores-típicos). |
| La carpeta queda "En cola" y no avanza | La tarea programada no está corriendo (Odoo apagado, `--max-cron-threads=0`, o `workers` mal configurado). Revisá en **Ajustes > Técnico > Tareas programadas** que "Buddy: sincronizar carpetas de Drive" esté activa. |
| Buddy contesta "Todavía no estoy configurado" | Falta la clave de API o el nombre del modelo, o hay un valor inválido. Revisá y guardá. |
| Buddy siempre responde "no encontré información suficiente" | La carpeta no tiene grupos asignados, no se sincronizó, o la persona no pertenece a esos grupos. Probá **Probar conexión** y revisá la carpeta. |
| "Probar conexión" dice que el modelo no existe | El nombre del modelo está mal escrito. El mensaje lista los disponibles: copiá uno tal cual. |
| El widget no aparece | Refrescá con Ctrl+F5. Solo aparece a usuarios internos, y no si "Ocultar el buddy a todos" está activo o la persona lo ocultó. |
| Demo: "Docker no responde" | Docker Desktop está apagado o arrancando. Abrilo, esperá a que diga que está funcionando y repetí. En Windows puede faltar WSL 2. |
| Demo: el puerto 8069 está ocupado | Otro programa (otro Odoo) lo usa. Cerralo y repetí el comando. |
