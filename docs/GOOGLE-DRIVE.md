# Conectar Google Drive (paso a paso)

Esta guía es para quien lo hace por primera vez y no es técnico. Lleva unos 10 a 15 minutos. Al final, Odoo lee las carpetas de Drive que elijas y Buddy responde con sus documentos.

> **Aviso sobre los nombres de Google.** Google cambia de lugar y de nombre los menús de su consola de vez en cuando. Los nombres de abajo son los más probables en español; **el nombre puede variar**. Si no encontrás un botón, buscá uno parecido o usá el buscador de la consola (arriba, en el centro). Esta guía no se pudo contrastar contra una cuenta real de Google en esta versión: lo que sí está verificado contra el código son los mensajes de error y los límites.

## La idea en 30 segundos

Buddy no entra a tu Drive con tu usuario. Usa una **cuenta de servicio**: una "cuenta robot" de Google con su propio email (algo como `buddy-ia@mi-proyecto.iam.gserviceaccount.com`). Esa cuenta **no ve nada** de tu Drive hasta que vos le compartas una carpeta, igual que le compartirías una carpeta a un compañero. Le compartís **solo lectura**: no puede cambiar ni borrar nada.

Lo que vas a necesitar al final:

- un archivo **`.json`** (la clave de esa cuenta robot), que vas a pegar en Odoo;
- el **email** de esa cuenta, con el que compartís las carpetas;
- el **enlace** de cada carpeta que quieras conectar.

## Los pasos

### 1. Entrá a la consola de Google Cloud

Abrí <https://console.cloud.google.com> con tu cuenta de Google (de trabajo si tu empresa usa Google Workspace). Si es la primera vez, aceptá los términos. **No hace falta cargar tarjeta** para esto (la API de Drive se usa sin costo en uso normal, con cuotas de Google).

### 2. Creá un proyecto

Arriba a la izquierda, al lado del logo de Google Cloud, tocá el **selector de proyectos** (muestra el nombre del proyecto actual o "Seleccionar un proyecto") y después **Proyecto nuevo**. Ponele un nombre fácil, por ejemplo `Buddy IA`, y tocá **Crear**. Esperá unos segundos y asegurate de que el proyecto nuevo quedó **seleccionado** en ese mismo selector. Todo lo que sigue se hace dentro de este proyecto.

### 3. Activá la API de Google Drive

Menú ☰ (arriba a la izquierda) > **APIs y servicios** > **Biblioteca**. En el buscador escribí `Google Drive API`, entrá en el resultado y tocá **Habilitar**.

Atajo: <https://console.cloud.google.com/apis/library/drive.googleapis.com> (elegí tu proyecto si te lo pide). Si ya estaba activada, vas a ver **Administrar** en vez de **Habilitar**: perfecto, seguí.

### 4. Creá la cuenta de servicio

Menú ☰ > **IAM y administración** > **Cuentas de servicio** > **Crear cuenta de servicio**. En "Nombre" poné `buddy-ia`. Tocá **Crear y continuar**. Los pasos de "Otorgar acceso al proyecto" y "Otorgar acceso a los usuarios" son opcionales: **no hace falta darle ningún rol**. Tocá **Continuar** y después **Listo**.

### 5. Creá y descargá la clave JSON

En la lista de cuentas de servicio, tocá el **email** de la cuenta que acabás de crear. Abrí la pestaña **Claves** > **Agregar clave** > **Crear clave nueva** > elegí **JSON** > **Crear**. El navegador descarga un archivo `.json`.

**Cuidalo como una contraseña:** quien tenga ese archivo puede leer todo lo que le compartas a esa cuenta. No lo mandes por mail ni lo subas a un repositorio. Cuando termines el paso 7 podés borrarlo de tu compu.

> **Aviso importante: Google puede tener bloqueada la creación de claves.** Las organizaciones **nuevas** de Google Workspace / Google Cloud traen **bloqueada por defecto** la creación de claves de cuentas de servicio (política de la organización `iam.disableServiceAccountKeyCreation`, en su versión nueva `constraints/iam.managed.disableServiceAccountKeyCreation`). Si al tocar **Crear** aparece un error como **"Service account key creation is disabled"** ("la creación de claves de cuenta de servicio está inhabilitada"), no es un problema de Buddy ni de tu cuenta: es una política de tu organización.
>
> **Cómo se arregla:** alguien con el rol **Administrador de políticas de la organización** (*Organization Policy Administrator*) tiene que desactivar esa restricción **para este proyecto**: Google Cloud > **IAM y administración** > **Políticas de la organización** > buscar **"Inhabilitar la creación de claves de cuenta de servicio"** (*Disable service account key creation*) > **Administrar política** > anular la política heredada del padre / dejarla **no aplicada** para este proyecto > **Guardar**. Después volvé a crear la clave. Otra salida es usar un proyecto que quede fuera de esa política. **El nombre exacto de los menús puede variar.**
>
> Con una **cuenta de Gmail personal** (sin organización) esto no pasa. Fuente de Google: <https://docs.cloud.google.com/resource-manager/docs/secure-by-default-organizations>.
>
> Si en tu empresa esa política **no se puede relajar**, hoy Buddy no tiene otra forma de conectarse a Drive (una conexión con el botón "Conectar con Google" está como idea en el [ROADMAP](ROADMAP.md)).

### 6. Copiá el email de la cuenta de servicio

Es la dirección que termina en `.iam.gserviceaccount.com`. La ves en la lista de cuentas de servicio y también adentro del archivo `.json` (campo `client_email`). **No hace falta anotarla ahora**: después de pegar la clave, Odoo te la muestra.

### 7. Pegá la clave en Odoo

En Odoo, como administrador: **Ajustes > Buddy IA > Google Drive > Cuenta de servicio**. Hay **dos formas** (usá una):

- **Subir el archivo:** tocá **Subir el archivo .json**, elegí el archivo que descargaste y tocá **Guardar** (arriba). Es lo más cómodo: no hay que abrir nada ni copiar nada.
- **Pegar el contenido:** abrí el archivo `.json` con el Bloc de notas, **copiá todo el contenido** (de la `{` del principio a la `}` del final), pegalo en el cuadro de texto y tocá **Guardar**.

En los dos casos la clave se trata igual: se guarda solo lo que Buddy necesita, **no se vuelve a mostrar** y el archivo no queda guardado en Odoo (ni como adjunto). Si subís un archivo que no es una clave de Google (o pesa más de 64 KB), Odoo te lo dice sin mostrar su contenido.

Si todo está bien, ves **"Clave cargada."** y debajo: **"Compartí cada carpeta de Drive (rol «Lector») con este correo: `buddy-ia@...iam.gserviceaccount.com`"**, con un **botón para copiarlo**. Ese es el email del paso 6. En la misma pantalla, **arriba del cuadro de la clave y siempre abierta**, hay una guía paso a paso («Cómo conseguir la clave de Google») por si te perdés; en el formulario de cada carpeta hay otra, corta, con cómo compartirla.

Buddy **solo guarda lo que necesita** de ese archivo (el email, la clave privada, la dirección de Google para pedir acceso y el id del proyecto). La clave no se vuelve a mostrar. Por seguridad, Buddy solo acepta la dirección oficial `https://oauth2.googleapis.com/token`; si tu archivo tiene otra, lo rechaza.

### 8. Compartí la carpeta de Drive con ese email (solo lectura)

En Google Drive, hacé clic derecho sobre la carpeta con los documentos > **Compartir**. En "Agregar personas, grupos y eventos" pegá el **email de la cuenta de servicio**. A la derecha elegí el rol **Lector** (no Editor). Desmarcá "Notificar a las personas" si te lo ofrece (el robot no lee mails) y tocá **Compartir** o **Enviar**.

Google puede advertirte que esa dirección está fuera de tu organización: es esperable, confirmá ("Compartir de todas formas"). Si tu Workspace bloquea compartir con direcciones externas, hay que pedirle al administrador que lo permita.

Repetí con **cada carpeta** que quieras conectar. Compartir una carpeta comparte también todas sus subcarpetas y archivos.

### 9. Copiá el enlace de la carpeta

Abrí la carpeta en Drive y copiá la dirección de la barra del navegador (se ve como `https://drive.google.com/drive/folders/1AbC...`). También sirve **Compartir > Copiar enlace**.

Tiene que ser el enlace de una **carpeta**, no de un archivo. Si pegás el de un archivo, Odoo te avisa: "Ese link es de un archivo, no de una carpeta".

### 10. Conectá la carpeta en Odoo

En Odoo: menú **Buddy > Carpetas de Drive > Nuevo**. En la parte de arriba del formulario ves **el correo de la cuenta de servicio con un botón para copiarlo** y un recordatorio de cómo compartir la carpeta en Drive (si todavía no cargaste la clave, un aviso te manda a Ajustes). La clave privada nunca se muestra, solo ese correo, que es público.

Al guardar, la carpeta empieza a sincronizarse sola. **El estado se actualiza solo cada 5 segundos** mientras está «En cola» o «Sincronizando» (no hace falta recargar la pantalla; si estás editando algo sin guardar, no te lo pisa) y hay un botón **Actualizar estado** por si preferís mirar a mano.

1. **Nombre:** cómo la llamás vos ("RR.HH.", "Procesos de Ventas").
2. **Carpeta de Drive:** pegá el enlace del paso 9.
3. **Quiénes pueden consultarla:** elegí uno o más grupos de Odoo. Para que la vea cualquier empleado, elegí **Usuario interno**. **Sin grupos no se puede guardar** (nadie recibiría respuestas de esa carpeta).
4. Tocá **Guardar** y después **Sincronizar ahora**. Se pone "En cola" y arranca en un instante. Va a tardar unos minutos según cuántos documentos haya: actualizá la pantalla (F5) para ver el avance.

Cuando termine, el estado dice **Al día**, ves el resumen ("12 nuevos o actualizados, 1 sin texto o no compatibles...") y en la pestaña **Documentos** la lista de lo que leyó.

### 11. Probalo

Volvé a **Ajustes > Buddy IA** y tocá **Probar conexión** (necesitás haber cargado también el proveedor de IA: ver [INSTALL](INSTALL.md#5-configurar)). Después entrá con un usuario común que pertenezca a uno de los grupos de la carpeta y hacele una pregunta a Buddy.

## Unidades compartidas (Shared Drives)

Si la carpeta está en una **Unidad compartida** (antes "Team Drive"), Buddy también la lee. Hay dos formas de darle acceso a la cuenta de servicio:

- **Compartir solo la carpeta** con el email (paso 8), igual que en "Mi unidad". Es lo más prolijo: la cuenta ve solo esa carpeta.
- **Agregar la cuenta como miembro de la unidad compartida** (en Drive: clic derecho sobre la unidad > **Administrar miembros**, rol **Lector** o equivalente). Entonces ve **toda** la unidad: usalo solo si es lo que querés.

En Workspace, a veces las unidades compartidas **no permiten agregar a personas de afuera de la organización**; si no te deja, es una política que cambia el administrador de Google. El enlace de una carpeta dentro de una unidad compartida se copia igual que en el paso 9.

## Qué archivos lee Buddy

| Se lee | Cómo |
|---|---|
| **Google Docs** | Se exporta como texto |
| **Google Sheets** | Se exporta como CSV (Google exporta solo la **primera hoja** del libro) |
| **Google Slides** | Se exporta como texto (el texto de las diapositivas) |
| **PDF con texto** | Texto de cada página (con pypdf) |
| **.docx** (Word) | El texto de los párrafos del cuerpo (incluidas las celdas de tablas); no encabezados ni pies de página |
| **.txt, .md, .csv** | Texto plano |
| **.html, .htm** | El texto de la página, sin scripts ni estilos |

**No se lee** (queda marcado **"Sin texto o tipo no compatible"** en la pestaña Documentos, o se saltea): PDFs escaneados (fotos de papel, sin texto), PDFs con contraseña, Word viejo (.doc), Excel (.xlsx/.xls), PowerPoint (.pptx/.ppt), imágenes, audio, video, Google Forms o Dibujos, y los **accesos directos** a otros archivos (se ignoran). Para esos casos, convertí el documento a Google Doc o a PDF con texto.

Los archivos en la **papelera** de Drive se ignoran.

## Límites

| Límite | Valor | Qué pasa al pasarse |
|---|---|---|
| Archivos por carpeta conectada | **2000** | Buddy lee los primeros 2000 que encuentra (recorre primero la carpeta principal y después las subcarpetas, nivel por nivel) y **el resto se ignora**: el **Resultado** de la carpeta te avisa («tope de archivos alcanzado») y lo ya indexado no se borra. |
| Niveles de subcarpetas | **8** | Lo que esté más profundo se ignora, también sin aviso. |
| Tamaño de un archivo | **20 MB** | Ese archivo queda "Con error" ("El archivo pesa más de 20 MB y lo salteo") y se sigue con los demás. Los Google Docs/Sheets/Slides muy grandes pueden fallar porque Google limita lo que exporta ("demasiado grande para que Google lo exporte"). |
| Texto por archivo | 1.000.000 de caracteres y 400 fragmentos | Se lee solo el principio. |

**Importante sobre los 2000 archivos:** cuando una carpeta llega al tope, el campo **Resultado** de la carpeta lo dice. Si es enorme, comparalá la **Cantidad de documentos** de la lista de carpetas con lo que esperás. Lo recomendable es **conectar carpetas más chicas y específicas** (por área) en vez de una carpeta gigante con todo.

## Qué NO hace Buddy en tu Drive

- **No escribe nada**: no crea, edita, mueve ni borra archivos. La clave se pide con el permiso `drive.readonly` (solo lectura) y el código solo hace consultas de lectura.
- **No ve nada que no le compartas.** Si dejás de compartir una carpeta, Buddy ya no puede leerla (la próxima sincronización falla con "No encuentro la carpeta" y lo que ya había leído se conserva hasta que lo borres).
- **No manda tus documentos completos a nadie.** Qué sale al proveedor de IA: [SECURITY](SECURITY.md#qué-sale-del-servidor-y-a-dónde).

## Errores típicos

Los mensajes aparecen en rojo en la ficha de la carpeta (**Buddy > Carpetas de Drive**) y al guardar la clave en Ajustes.

| Mensaje (resumido) | Qué significa | Cómo se arregla |
|---|---|---|
| **"No encuentro la carpeta. ¿La compartiste con ...?"** | Drive dice que esa carpeta no existe para la cuenta de servicio. | Compartí la carpeta con el email que indica el mensaje (rol Lector) y revisá que el enlace sea de esa carpeta. A veces tarda un minuto en notarse: probá **Sincronizar ahora** de nuevo. |
| **"La cuenta ... no tiene permiso para leer esa carpeta. Compartila con rol Lector."** | La cuenta la ve pero no tiene acceso suficiente (o a un archivo puntual). | Compartí con rol Lector. Si es una unidad compartida, mirá la sección de arriba. |
| **"Activá la API de Google Drive en tu proyecto de Google Cloud..."** | Falta el paso 3 (o es otro proyecto). | Abrí el enlace del mensaje con la cuenta dueña del proyecto y tocá **Habilitar**. Puede tardar unos minutos en aplicarse. Asegurate de haber creado la clave en el **mismo proyecto** donde activaste la API. |
| **"La clave JSON no es válida..."** (varias versiones) | El texto pegado no es el archivo completo, no es de tipo cuenta de servicio, le falta `client_email` o `private_key`, o la clave privada está dañada. | Abrí el `.json`, copiá **todo** (de `{` a `}`) sin modificar nada y pegalo de nuevo. Si no funciona, creá una clave nueva (paso 5). |
| **"El campo token_uri no es el oficial de Google"** | El archivo no es el original de Google. | Usá el `.json` tal cual lo descargaste. Por seguridad Buddy solo acepta `https://oauth2.googleapis.com/token`. |
| **"Google no aceptó la clave de la cuenta de servicio (...). Revisá que la fecha y hora del servidor estén bien..."** | La clave fue borrada o reemplazada en Google Cloud, o el reloj del servidor está corrido. | Verificá en Google Cloud (cuenta > pestaña Claves) que la clave siga existiendo; si no, creá una nueva y pegala. Revisá la hora del servidor (idealmente con sincronización automática). |
| **"No pude firmar con la clave de la cuenta de servicio..."** | La clave no se puede usar para firmar (o falta PyJWT en el servidor). | Pegá una clave nueva completa. Si pasa con cualquier clave, mirá [INSTALL](INSTALL.md#3-instalar-las-dependencias-de-python). |
| **"Google está limitando los pedidos. Probá de nuevo en unos minutos."** | Se pasó una cuota de Google. | Esperá. Buddy reintenta solo en la próxima pasada. |
| **"No pude conectarme con Google Drive..."** | El servidor no sale a internet o Google no responde. | Revisá que el servidor llegue por HTTPS a `oauth2.googleapis.com` y `www.googleapis.com` (firewall, proxy). |
| **"No pude sacar el ID de la carpeta..."** / **"Ese link es de un archivo, no de una carpeta..."** | El enlace pegado no es el de una carpeta. | Abrí la carpeta (no un archivo) y copiá su enlace (paso 9). |
| **"Elegí quiénes pueden consultar la carpeta ..."** | Falta asignar grupos. | Agregá al menos un grupo; **Usuario interno** = todos los empleados. |
| **"Falta la librería pypdf en el servidor, así que no puedo leer PDFs"** | No se instaló la dependencia. | Ver [INSTALL](INSTALL.md#3-instalar-las-dependencias-de-python). |
| **"El archivo pesa más de 20 MB y lo salteo."** (en un documento) | El archivo es muy grande. | Dividilo o reducí su peso. |
| **"Falló la sincronización por un error inesperado. Revisá el registro del servidor."** | Un error que Buddy no previó. | Mirá el log de Odoo (busca "Buddy: sincronización") y avisá a quien mantiene el servidor. |
| **"Primero cargá la clave de Google Drive en Ajustes > Buddy IA."** | Falta el paso 7. | Pegá la clave. |

**Cómo se reintenta solo:** una carpeta con error vuelve a intentarse sola después de 1 hora (si tiene "Sincronizar automáticamente"). Un archivo con error se reintenta en cada sincronización. Si las credenciales, la API o la red fallan, se frena **toda** la carpeta (no tiene sentido seguir archivo por archivo).

## Rotar o quitar la clave

- **Cambiarla:** creá una clave nueva (paso 5), pegala en Ajustes > Buddy IA (reemplaza a la anterior) y **después** borrá la vieja en Google Cloud (cuenta de servicio > Claves).
- **Quitarla:** Ajustes > Buddy IA > **Quitar la clave**. Las carpetas dejan de sincronizarse y lo ya leído se conserva.
