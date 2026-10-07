# Roadmap

Buddy hoy es un **asistente de documentación interna**: responde con los documentos de carpetas de Google Drive, cita fuentes, respeta permisos por grupo, deriva a un contacto o crea un ticket. Lo que sigue, en orden de prioridad. Son planes e ideas, **no funciones entregadas**.

## 1. Verificar con servicios reales

Probar contra una **cuenta real de Google Drive** (la guía y los mensajes), contra al menos un **proveedor de IA real** (formato de respuesta, calidad, costos), carga y concurrencia de punta a punta, modo oscuro y móvil táctil. Detalle en [VALIDATION](VALIDATION.md).

## 2. Avisar los topes de Drive

El tope de 2000 archivos ya se avisa en la carpeta y no borra lo indexado. Falta avisar también cuando quedan archivos a más de 8 niveles de subcarpetas.

## 3. Seguridad que queda

- **Aislamiento multi-empresa** (reglas por compañía en carpetas, documentos y fragmentos).
- Rate limit propio en `/buddy/config` y `/buddy/forget`.
- Que "Ocultar el buddy" también bloquee `/buddy/chat`.
- Opcional: cifrar las claves guardadas.

## 4. Tickets de verdad

Pasar `buddy.ticket` a `mail.thread`: responsable, seguimiento y aviso por correo.

## 5. Calidad y mantenimiento

- Evaluar con un modelo real el ajuste de la búsqueda por texto (`buddy_ia.text_min_score`, umbral de coincidencias).
- Traducir los textos de Python que faltan.
- Feedback de la gente sobre las respuestas (útil / no útil).
- Panel de costos y uso.
- Cola de consultas asíncrona (no ocupar un worker mientras responde la IA).

## Ideas para más adelante (sin compromiso)

- **Botón "Conectar con Google" (OAuth)** para empresas donde la política de la organización **no permite crear claves de cuenta de servicio** (hoy es obligatoria la clave JSON). Permitiría conectar Drive con el acceso del propio usuario sin descargar claves.
- **OneDrive / SharePoint** como segunda fuente de documentos.
- **Búsqueda semántica opcional** (por significado, con embeddings) para encontrar documentos con otras palabras, como complemento de la búsqueda por texto y sin volver obligatoria ninguna extensión de base de datos.
- **`buddy_onboarding`:** planes de incorporación por puesto, progreso, `mail.activity`, integración con RR.HH. El onboarding completo queda fuera de Buddy actual.
