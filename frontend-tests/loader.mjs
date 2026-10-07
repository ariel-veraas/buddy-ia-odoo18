// Loader de pruebas: resuelve los imports de Odoo (@odoo/owl, @web/...) a stubs/OWL real,
// y agrega la extensión .js a los imports relativos (Odoo no la usa).
import { pathToFileURL, fileURLToPath } from "node:url";
import path from "node:path";
import { existsSync } from "node:fs";

const here = path.dirname(fileURLToPath(import.meta.url));
const MAP = {
    "@odoo/owl": pathToFileURL(path.join(here, "node_modules/@odoo/owl/dist/owl.es.js")).href,
    "@web/core/registry": pathToFileURL(path.join(here, "stubs/registry.js")).href,
    "@web/core/network/rpc": pathToFileURL(path.join(here, "stubs/rpc.js")).href,
    "@web/core/l10n/translation": pathToFileURL(path.join(here, "stubs/translation.js")).href,
    "@web/core/utils/hooks": pathToFileURL(path.join(here, "stubs/hooks.js")).href,
    "@web/views/fields/standard_field_props": pathToFileURL(path.join(here, "stubs/standard_field_props.js")).href,
};

export async function resolve(specifier, context, next) {
    if (MAP[specifier]) {
        return { url: MAP[specifier], shortCircuit: true };
    }
    if (specifier.startsWith(".") && !path.extname(specifier) && context.parentURL) {
        const candidate = new URL(specifier + ".js", context.parentURL);
        if (existsSync(candidate)) {
            return { url: candidate.href, shortCircuit: true };
        }
    }
    return next(specifier, context);
}
