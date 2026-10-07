# Riesgos y límites conocidos

Lo que hay que saber antes de depender de Buddy. Para lo que sí se probó, ver [VALIDATION](VALIDATION.md).

| Tema | Estado |
|---|---|
| **Google Drive real** | **No probado contra una cuenta real.** La sincronización se probó con respuestas simuladas de Google y la guía de [GOOGLE-DRIVE](GOOGLE-DRIVE.md) no se recorrió contra la consola real. Esperá algún ajuste en el primer uso. |
| **Proveedor de IA real** | **No probado de punta a punta.** Se usó un servidor simulado. Formato JSON con modelos reales, calidad, costos y límites de cada proveedor hay que comprobarlos (usá **Probar conexión** y las [20 preguntas](ADMIN-GUIDE.md#20-preguntas-de-prueba)). Gemini y "Otro" usan el modo compatible con OpenAI, que cada proveedor implementa a su manera. |
| Claves de cuenta de servicio bloqueadas | Las organizaciones nuevas de Google Workspace/Cloud bloquean por defecto la creación de claves. Hace falta un administrador de políticas de la organización. Si no se puede relajar, hoy no hay otra forma de conectar Drive (ver [ROADMAP](ROADMAP.md)). |
| Calidad de las respuestas | El formato está controlado; la exactitud depende del modelo y de tus documentos. Un modelo puede equivocarse o ser manipulado por un documento (prompt injection). Cada respuesta muestra sus fuentes para poder verificarla. |
| Datos de la empresa | Los fragmentos permitidos, la pregunta y el historial reciente salen al proveedor de IA (el nombre de la persona no se envía, solo si es "nueva"; sí puede ir un nombre que esté escrito en la pregunta o en un documento). El tratamiento depende de tu contrato. Ver [SECURITY](SECURITY.md#qué-sale-del-servidor-y-a-dónde). |
| **Tope de 2000 archivos y 8 niveles** | Si una carpeta tiene más, Buddy lee los primeros 2000 (nivel por nivel), lo avisa en el **Resultado** de la carpeta («tope de archivos alcanzado») y **no borra lo ya indexado**. Lo que queda a más de 8 niveles de subcarpetas se ignora sin aviso. Mitigación: conectar carpetas chicas y específicas. |
| Topes por documento | Máximo 400 fragmentos (~las primeras 340.000 letras), 1.000.000 de caracteres y 500 páginas por PDF. Cuando se corta, el documento lo dice en su **Detalle** y la carpeta en su **Resultado** («N documentos truncados»); lo que queda después del corte no se consulta. |
| Archivos que matan al proceso | Si un archivo hace caer al proceso de sincronización, la pasada siguiente lo marca «Con error» y no lo reintenta hasta que cambie en Drive. Mientras tanto ese archivo no responde nada. |
| Calidad de la búsqueda | Busca **por palabras** (español, sin acentos, con raíces), no por significado: una pregunta con otras palabras que el documento puede no encontrarlo, y no hay sinónimos. Con una raíz compartida puede devolver una fuente tangencial. Sin búsqueda semántica por ahora (idea en el roadmap). |
| Archivos que no se leen | PDFs escaneados, Word viejo, Excel, PowerPoint, imágenes: quedan como "Sin texto o tipo no compatible". Google Sheets: solo la primera hoja. |
| Multi-empresa | **No hay aislamiento por compañía.** Ninguna regla filtra por `company_id`. |
| Permisos de Buddy ≠ permisos de Drive | Buddy usa los grupos de Odoo. Alguien puede ver fragmentos de una carpeta por su grupo sin tener acceso en Drive al original. |
| Permisos por carpeta, no por archivo | Una subcarpeta "confidencial" dentro de una carpeta conectada se lee igual y la ve el mismo público. |
| Documentos borrados o desconectados | Si se quita el acceso a una carpeta entera, lo ya leído **se conserva** y sigue respondiendo hasta que se archive o borre la carpeta en Odoo. |
| Primera sincronización larga | Con muchos documentos tarda varias pasadas (unos 100 segundos de trabajo por pasada, cada 30 minutos o apretando el botón). No se midió con un corpus grande. |
| Carga y concurrencia | **No hay prueba de carga** ni de concurrencia con navegadores reales. Hay un tope global de consultas simultáneas (por defecto 3, en Ajustes > Capacidad): las que lo superan reciben un aviso amable y no gastan cuota. Con `workers = 0` una consulta lenta ocupa a Odoo; en producción usá `workers` > 0 y ajustá el tope a los procesos libres. Sin capacidad verificada para cientos de usuarios. |
| Claves en la base | La clave de API y la de Drive están en parámetros del sistema, sin cifrar; los backups las incluyen y un administrador con modo desarrollador las puede ver. |
| Límites de uso | Por persona (consultas, tickets) y un tope global de consultas simultáneas; no un tope monetario. Sin rate limit propio en `/buddy/config` y `/buddy/forget` (el límite global va en el proxy). |
| Ocultar el buddy | `buddy_ia.hidden` solo oculta el widget; no bloquea `/buddy/chat`. |
| Móvil táctil real y modo oscuro | Sin verificar. El panel del widget tiene su propio fondo oscuro. |
| Traducciones | Español como idioma fuente y plantilla `.pot` (sin traducción al inglés a propósito: mezclaría idiomas); varios textos de Python (motor, tickets) no están marcados para traducir. |
| Tickets | Registro simple en Odoo con estados. Sin aviso por correo, responsable ni integración con otro helpdesk (en el roadmap). |
| Demo | Usa una IA simulada y no se sincroniza con Google: sus respuestas son el texto del documento sin redactar. No usarla en producción. |
| Backups y borrado | El botón de olvidar conversación no elimina backups ni tickets. Definí la política en el servidor. |
| Costos | Buddy no cobra; el proveedor de IA sí. Ver [ADMIN-GUIDE](ADMIN-GUIDE.md#costos). |
