# Qué se probó y qué no

Este documento separa con honestidad lo verificado de lo pendiente. Versión: **18.0.5.5.0** (2026-10-07).

> **Resumen corto:** el módulo se instala, se actualiza y se prueba en un Odoo 18 real (Docker) con **proveedor de IA simulado** y **carpetas de demostración que no pasan por Google**; además el director lo usó con su Drive real (7 procedimientos) y Gemini real, y un QA con Gemini real (`gemini-2.5-flash`) encontró los problemas que corrige 18.0.4.8.0. **Claude y OpenAI nunca se probaron con claves reales**, y **la calidad con IA real se volvió a medir parcialmente** (ver «Todavía sin verificar»).

## Resultados de esta versión (corridos de verdad)

| Qué | Cantidad | Resultado |
|---|---|---|
| Pruebas nativas de Odoo 18 (`python tools/manage.py validate`, base descartable `buddy_test`, actualizando desde la versión anterior) | **896** | 0 fallas |
| Python puro (`tools/validate_local.py`: cliente de Drive 58, troceado 24, búsqueda y texto 69, streaming 53, vectores y búsqueda por significado 35, proveedor y motor 88, límites del controlador, traducciones y estructura de las vistas 103, instalador 8, lista blanca del buddy 8, evaluación 62, exportador de casos 15, reglas puras del onboarding 18) | **596** | todas bien |
| Widget (OWL + jsdom, `frontend-tests`) | **264** | todas bien |

`python tools/validate_local.py` + `python tools/manage.py validate` es el par que tiene que quedar en verde antes de entregar. La evolución: 336 nativas / 267 puras / 82 de widget en 18.0.4.4.0; 358 / 304 / 82 en 4.5.0; 436 / 333 / 95 en 4.6.0; 474 / 347 / 106 en 4.7.0; 490 / 360 / 106 en 4.8.0; 494 / 370 / 155 en 4.9.0 (buddys y gestos); 511 / 383 / 155 en 5.0.0 (memoria de conversación, E1); 615 / 418 / 155 en 5.1.0 (búsqueda por significado, E2); 795 / 541 / 218 en 5.3.0 (onboarding por recorridos, E4: `test_journey.py` con las nativas nuevas —modelos, permisos de persona / responsable / administrador, asignación masiva y automática no retroactiva, vencimientos, recordatorios sin duplicar, enlaces seguros, ajustes, rutas HTTP y superficie RPC—, 18 puras de `test_journey_logic.py`, 10 del contrato de las rutas y de la estructura de las vistas, y 36 del widget en `journey.test.js`); 698 / 513 / 182 en 5.2.0 (feedback 👍/👎, contradicciones y confianza, E3: `test_feedback.py` con las pruebas nativas nuevas, 18 puras nuevas entre el contrato, la guarda y la ruta, 62 + 15 de la evaluación y el exportador, y `feedback.test.js` con 27 del widget). La 4.9.0 se miró además a ojo con Chrome en un Odoo descartable.

## Verificado

- **Instalación y actualización en Odoo 18 (Docker):** `python tools/manage.py demo` instala `buddy_ia` en una base limpia y carga la demo; `manage.py validate` instala y **actualiza** (`-u buddy_ia`, con las migraciones) la base descartable. La actualización ya no muestra avisos «Access Rights Inconsistency».
- **Pruebas nativas** (lista completa de módulos en `odoo-addons/buddy_ia/tests`): cliente de Drive con respuestas de Google simuladas, sincronización (incluidos archivos que matan al proceso, NUL y caracteres de control), troceado, búsqueda de texto y su calidad (sinónimos, tipeos contra títulos y contra el texto de los documentos, vecinos), motor (cuotas por minuto, por día y de la empresa, reservas, sesiones, permisos, conversaciones), proveedor y streaming (OpenAI, Anthropic, `finish_reason = length`, tokens, errores de clave), ajustes y lista «Para empezar», tickets con chatter y responsable, estadísticas (privacidad, retención, costos), superficie de llamadas remotas (lista explícita de métodos públicos y `AccessError` por RPC), seguridad y permisos entre usuarios, y los textos de la interfaz.
- **Comprobaciones por HTTP en una instancia descartable** (puerto y base distintos de los del director): un empleado recibe `AccessError` en `stream_prepare`, `chat`, `history`, `chat_stream`, `stream_events` y `get_values`; las pantallas nuevas (Para empezar, ajustes de Drive, formulario de carpeta, tickets con chatter y tarea, preguntas sin respuesta, gráfico por día) se miraron en un navegador real.
- **Con servicios reales, por el director y el QA:** Drive real (7 procedimientos de Word, 79 fragmentos), sincronización, respuestas con fuentes, permisos por grupo, «no sé» con derivación; y Gemini real (`gemini-2.5-flash`): de ahí salieron las correcciones de las respuestas largas (razonamiento dentro del tope de salida), la clave inválida (Gemini responde 400, no 401) y los tipeos.
- **Widget:** pruebas del componente (OWL + jsdom, con dobles de los servicios de Odoo), incluida la respuesta en streaming, el historial, el foco, el botón Detener y los avisos de «no configurado»; revisión visual en navegador de escritorio.
- **Instalador de la demo:** pruebas de generación de claves y de la secuencia de pasos (con Docker sustituido), y ejecución real. **Registro público de Odoo** cerrado (invitación) por el instalador de la demo.

## Todavía sin verificar

- **Onboarding (E4):** corrido en un navegador real contra el stack v2 (puerto 8070, usuarios descartables que se borraron después) en escritorio y en emulación de celular: el widget completo (punto, bloque, tilde, enlace al documento real, «Preguntarle al buddy» con Gemini real), el formulario de recorridos, la ventana de asignación con sus avisos, asignaciones, avance (pivote y gráfico), pasos atrasados y los ajustes. **Sin verificar:** lector de pantalla real, un celular táctil real (el navegador del entorno es chico), teclado con foco, el modo oscuro de Odoo, varios recorridos o recorridos de 30 pasos en pantalla, los **recordatorios con un servidor de correo real** (en las pruebas se crea la actividad; el aviso por correo depende del correo saliente), el cron diario corriendo solo en un servidor real (se llama al método) y la **evaluación de la línea de etapa con el ajuste apagado** completa (ver [EVALUACION](EVALUACION.md#corrida-e4-contexto-de-etapa-18053-0)).

- **Feedback 👍/👎 y contradicciones (E3):** corridos con Gemini real contra documentos **sintéticos** y probados en un navegador real de escritorio y en emulación de celular (ver [EVALUACION](EVALUACION.md#corrida-e3-contradicciones-confianza-y-feedback-18052-0)); **sin verificar:** lector de pantalla real (NVDA/VoiceOver) y un celular táctil de verdad con los votos y la nota de confianza, uso por personas reales (si el motivo y la casilla se entienden), el panel con volumen real de votos, y la regla de visibilidad del gestor con **el cron diario corriendo en un servidor de producción** (en las pruebas se llama al método). Pendiente anotado: extender la guarda de campos solo-administrador (`buddy.guard`) a `buddy.source` y `buddy.document` ([SECURITY](SECURITY.md#feedback-de-las-respuestas-y-privacidad)).
- **Claude (Anthropic) y OpenAI reales:** nunca se llamó a sus APIs con una clave. El protocolo de Anthropic y el streaming de ambos están probados con servidores locales que imitan sus eventos; los tokens que informa cada proveedor, el costo estimado y el `reasoning_effort` de Gemini se basan en la documentación y en pruebas simuladas.
- **La corrección de 18.0.4.8.0 con Gemini real:** las respuestas largas (`finish_reason = length`), el 400 de clave inválida y los tipeos «bakups»/«tiket»/«cotisación» se prueban con respuestas simuladas; falta repetir la evaluación con la IA real.
- **Búsqueda por significado (E2) con Gemini real:** conjunto dorado completo (79 consultas, 3 corridas) y degradación en vivo (apagada, proveedor caído, sin vectores): ver [EVALUACION](EVALUACION.md#corrida-e2-búsqueda-híbrida-18051-0). **Sin verificar:** embeddings de **OpenAI** y de servidores «Otro» (los nombres y parámetros salen de su documentación y están probados con respuestas simuladas, no con una clave real), **Claude** (no ofrece embeddings: la degradación está probada), y la **carga con un corpus de miles de fragmentos reales**: el techo (unos 20.000 fragmentos de 768 números, ~1 s por pregunta) se midió con vectores sintéticos de tamaño real en el contenedor, no con documentos de una empresa grande.
- **Calidad con IA real** de saludos, fuentes citadas, fuera de tema y tipeos en general (las [20 preguntas](ADMIN-GUIDE.md#20-preguntas-de-prueba) con usuarios comunes). Hay dos casos conocidos de calidad sin arreglo barato: a veces no menciona la opción de pedir soporte por correo, y «¿y quién los gestiona?» da una respuesta parcial más un ticket.
- **Carga y capacidad:** sin prueba con un corpus grande ni con muchos usuarios a la vez; sin probar con `workers > 0` y un nginx real delante.
- **Concurrencia de punta a punta:** el runner HTTP (`tests/e2e/backend_regressions.py`) existe pero no se ejecutó contra esta versión.
- **Modo oscuro**, **dispositivo táctil real** y **lector de pantalla** del widget (las etiquetas y los tamaños de toque están, no se probaron con el aparato).
- **Instalación en un Odoo propio sin Docker:** la guía [INSTALL](INSTALL.md) está escrita a partir del código y del instalador Docker, no recorrida en un servidor real. La actualización con Docker (imagen nueva → parar → `-u buddy_ia` → levantar) se probó con la base descartable.
- **La instancia del director:** todavía no tiene instalada esta versión; hay que actualizarla con `-u buddy_ia` en el orden de [INSTALL](INSTALL.md#actualizar) (y rotar la clave de API si venía de 4.4–4.6).
- **Odoo Enterprise, Odoo.sh** y versiones de PostgreSQL distintas de las de la imagen Docker.
- **Licencias:** la lectura de licencias de dependencias es informativa, no una revisión legal.

## Cómo volver a correr las pruebas

Pruebas nativas (necesitan Docker y Python 3.12):

```text
python tools/manage.py validate
```

Usa exclusivamente la base `buddy_test`. Que termine bien es necesario, pero no reemplaza probar con Google y un proveedor reales.

Pruebas del widget (necesitan Node): `make test`, o `cd frontend-tests && npm ci --ignore-scripts && npm test`.

## Cómo probar con servicios reales (recomendado antes de producción)

1. En un Odoo de prueba, seguí [GOOGLE-DRIVE](GOOGLE-DRIVE.md) con **una carpeta chica** (5 a 10 documentos de cada tipo que uses).
2. Cargá un proveedor real, un modelo, y apretá **Probar conexión**.
3. Corré las [20 preguntas de prueba](ADMIN-GUIDE.md#20-preguntas-de-prueba) con usuarios comunes y anotá los resultados.
4. Mirá el consumo en el proveedor de IA tras una semana de uso piloto.
5. Si algo de las guías no coincide con lo que ves, corregilo: son los puntos menos verificados.
