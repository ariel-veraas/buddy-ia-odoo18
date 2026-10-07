# Atribución y licencias

## El módulo

`buddy_ia` (autor declarado en el manifiesto: Equipo Buddy) se distribuye bajo **LGPL-3**. Los textos de LGPL-3 y de su licencia base GPL-3 están en `COPYING.LESSER` y `COPYING`.

El módulo es autocontenido: **no incluye ni depende de código de terceros dentro del repositorio** (a partir de 18.0.4.0.0 ya no se usan los módulos `odoo-llm` de Apexive, ni `vendor/`, ni parches; ver [docs/CHANGELOG.md](docs/CHANGELOG.md)).

## Dependencias que se instalan aparte

No están redistribuidas en este repositorio: se instalan desde PyPI según `odoo-addons/requirements.lock` y conservan sus propias licencias. Las licencias de la tabla son las que declaran los proyectos; **verificalas en su sitio antes de redistribuir o de usar el módulo en un producto comercial**.

| Librería | Para qué | Licencia declarada por el proyecto |
|---|---|---|
| pypdf | Leer el texto de los PDF | BSD-3-Clause |
| PyJWT | Firmar el pedido de acceso a Google | MIT |
| packaging | Chequeo de versiones de dependencias | Apache-2.0 o BSD-2-Clause |
| requests, cryptography | Ya vienen con Odoo | Apache-2.0 (requests); Apache-2.0 o BSD (cryptography) |

Todas las librerías que Buddy agrega tienen licencias permisivas (MIT, BSD, Apache), compatibles con la LGPL-3 del módulo. Se evitó a propósito una librería de PDF con licencia AGPL.

## Servicios externos

Google Drive y el proveedor de IA que elijas (Anthropic, OpenAI, Google u otro) son servicios de terceros con sus propios términos y precios. Esta entrega no los incluye ni los representa.

## Recursos gráficos

Los personajes **Kitsune** y **Mochi** (los SVG de `odoo-addons/buddy_ia/static/src/xml/buddy_avatar_*.xml`, generados por `tools/buddy_art.py`) fueron dibujados para este proyecto y se distribuyen bajo la misma licencia LGPL-3 que el módulo. La mascota de la empresa para la que se hizo el proyecto **no** forma parte de este repositorio: es propiedad de esa empresa y vive en un módulo privado aparte (ver «Un buddy propio de tu empresa» en [docs/CUSTOMIZE.md](docs/CUSTOMIZE.md)). Las capturas de `docs/screenshots` muestran la demo con datos ficticios.
