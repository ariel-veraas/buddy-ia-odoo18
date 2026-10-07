/** @odoo-module **/

/**
 * Registro de buddys, accesorios y gestos: el ÚNICO lugar de JS que hay que tocar para sumar uno nuevo
 * (el otro es la lista de valores permitidos de Python, models/buddy_look.py). Ver docs/CUSTOMIZE.md.
 *
 * Todo lo que llega del servidor pasa por `cleanLook()`: solo se aceptan ids que estén acá. El servidor NUNCA
 * manda HTML, SVG ni estilos: manda ids, y el dibujo vive en los templates de static/src/xml/buddy_avatar_*.xml.
 */

export const RINGS = ["idle", "listening", "thinking", "ok", "doubt"];

/** Las expresiones del contrato con la IA (el campo `expression` del JSON). No se tocan. */
export const EXPRESSIONS = ["neutral", "happy", "thinking", "surprised", "doubt", "wink"];

/** Caras extra que solo usan los gestos del cliente (la IA nunca las pide). */
export const GESTURE_FACES = ["sparkle", "heart", "tears", "dizzy", "mad", "yawn", "sleep", "squint"];
export const FACES = [...EXPRESSIONS, ...GESTURE_FACES];

export const DEFAULT_BUDDY = "mochi";
export const DEFAULT_ACCESSORY = "none";
export const DEFAULT_FREQUENCY = "normal";

/**
 * Buddys. `disc`: true = la cabeza va dentro de un disco oscuro con borde (para personajes de empresa que lo piden, sumados desde otro módulo); false = personaje suelto,
 * sin redondel de fondo (Kitsune, Mochi). `view`: viewBox [x, y, ancho, alto] de la cabeza sola y del cuerpo entero
 * (el lienzo de todos es de 200 de ancho: `size` mide ese ancho, el alto sale de la proporcion). `anchors`: dónde van los efectos (signo de pregunta, gota, Zzz...), y `acc`: dónde va cada accesorio,
 * en las coordenadas del propio dibujo (lienzo de 200 x 260). Accesorios: [x, y, escala, rotación] o, para
 * los auriculares, [x, y, escalaX, escalaY].
 * Otro módulo puede sumar su personaje con `BUDDIES.<id> = {…}` (y gestos propios en `GESTURES`): ver docs/CUSTOMIZE.md.
 * Kitsune y Mochi se dibujan en un grupo desplazado (translate 0 -8 / 0 -2): los números ya lo tienen en cuenta.
 */
export const BUDDIES = {
    kitsune: {
        template: "buddy_ia.Avatar.kitsune",
        disc: false,
        view: { head: [18, -8, 172, 136], full: [18, -8, 172, 208] },
        anchors: { top: [100, 22], temple: [150, 76], forehead: [76, 64], zzz: [160, 28], nose: [100, 112] },
        acc: { hat: [100, 50, 0.8, 0], eyes: [100, 97, 1, 0], mouth: [100, 114, 0.72, 0], phones: [100, 92, 1.1, 1.0] },
    },
    mochi: {
        template: "buddy_ia.Avatar.mochi",
        disc: false,
        view: { head: [24, 0, 152, 140], full: [24, 0, 152, 148] },
        anchors: { top: [80, 8], temple: [150, 62], forehead: [66, 58], zzz: [152, 22], nose: [110, 104] },
        acc: { hat: [100, 40, 0.78, 0], eyes: [100, 90, 0.9, 0], mouth: [100, 106, 0.6, 0], phones: [100, 88, 1.22, 1.0] },
    },
};

/**
 * Accesorios. `slot`: lo que ocupan en el cuerpo (dos accesorios del mismo slot no se pisan: un gesto de accesorio
 * temporal no se muestra si el fijo ya ocupa su slot). `place`: cuál de las posiciones de `acc` usan.
 */
export const ACCESSORIES = {
    none: { slot: null },
    mustache: { slot: "mouth", place: "mouth" },
    wizard: { slot: "head", place: "hat" },
    shades: { slot: "eyes", place: "eyes" },
    headphones: { slot: "head", place: "phones" },
    party: { slot: "head", place: "hat" },
};

export const GESTURE_GROUPS = ["expressions", "accessories", "sleep"];

/**
 * Gestos de reposo. `ms`: cuánto dura (el CSS de cada gesto dura menos). `weight`: qué tan seguido sale.
 * `face`: cara que reemplaza a la expresión mientras dura. `fx`: efectos dibujados (buddy_avatar.xml).
 * `accessory`: accesorio temporal. `buddies`: si está, solo ese/esos buddys lo hacen (variantes propias).
 */
export const GESTURES = {
    blink: { group: "expressions", ms: 1400, weight: 4 },
    look: { group: "expressions", ms: 2800, weight: 3 },
    sparkle: { group: "expressions", ms: 2400, weight: 2, face: "sparkle", fx: ["sparkles"] },
    heart: { group: "expressions", ms: 2800, weight: 2, face: "heart", fx: ["hearts"] },
    sweat: { group: "expressions", ms: 2600, weight: 2, face: "doubt", fx: ["sweat"] },
    vein: { group: "expressions", ms: 2000, weight: 1, face: "mad", fx: ["vein"] },
    tears: { group: "expressions", ms: 2800, weight: 1, face: "tears" },
    dizzy: { group: "expressions", ms: 2800, weight: 1, face: "dizzy", fx: ["stars"] },
    question: { group: "expressions", ms: 2400, weight: 2, face: "doubt", fx: ["question"] },
    exclaim: { group: "expressions", ms: 2000, weight: 2, face: "surprised", fx: ["exclaim"] },
    tail: { group: "expressions", ms: 2800, weight: 3, buddies: ["kitsune"] },
    ears: { group: "expressions", ms: 2400, weight: 3, buddies: ["kitsune"] },
    bounce: { group: "expressions", ms: 2400, weight: 3, face: "happy", buddies: ["mochi"] },
    jiggle: { group: "expressions", ms: 2200, weight: 2, buddies: ["mochi"] },
    mustache: { group: "accessories", ms: 4000, weight: 2, accessory: "mustache" },
    wizard: { group: "accessories", ms: 4000, weight: 2, accessory: "wizard" },
    shades: { group: "accessories", ms: 4000, weight: 2, accessory: "shades" },
    yawn: { group: "sleep", ms: 2800, weight: 2, face: "yawn" },
    stretch: { group: "sleep", ms: 2600, weight: 2, face: "squint" },
    sleep: { group: "sleep", ms: 6200, weight: 1, face: "sleep", fx: ["zzz", "bubble"] },
};

/**
 * El gesto suave con el que el buddy acompaña el aviso de las tareas del día (el globito lo dice, el gesto solo llama la atención sin
 * sobresaltar): mirar alrededor, que hacen todos los buddys. `null` cuando el administrador apagó ese grupo de gestos: el globito sale igual,
 * sin movimiento.
 */
export const NUDGE_GESTURE = "look";

/** Cada cuánto actúa el buddy (segundos). Está documentado en docs/ADMIN-GUIDE.md: si cambia acá, cambia allá. */
export const IDLE_FREQUENCIES = {
    // idle: segundos de quietud antes de poder actuar; gap: rango (con azar) entre un gesto y el siguiente.
    low: { idle: 60, gap: [120, 240] },
    normal: { idle: 30, gap: [45, 90] },
    high: { idle: 12, gap: [15, 40] },
};

/** Con el chat abierto el buddy solo actúa si pasó este tiempo sin que nadie lo use. */
export const OPEN_QUIET_SECONDS = 60;

const has = (table, key) => typeof key === "string" && Object.hasOwn(table, key);

export const cleanBuddy = (value) => (has(BUDDIES, value) ? value : DEFAULT_BUDDY);
export const cleanAccessory = (value) => (has(ACCESSORIES, value) ? value : DEFAULT_ACCESSORY);
export const cleanFace = (value) => (FACES.includes(value) ? value : "neutral");
/** What the AI may ask for: only the six expressions of the contract (the extra faces belong to the client gestures). */
export const cleanExpression = (value) => (EXPRESSIONS.includes(value) ? value : "neutral");
export const cleanFrequency = (value) => (has(IDLE_FREQUENCIES, value) ? value : DEFAULT_FREQUENCY);
export const cleanRing = (value) => (RINGS.includes(value) ? value : "idle");

/**
 * Lo que `/buddy/config` dice sobre el aspecto, ya filtrado. Un servidor viejo (sin estos campos) o un valor
 * desconocido dan Mochi, sin accesorio y gestos activados con frecuencia normal.
 */
export function cleanLook(cfg) {
    const idle = cfg && typeof cfg.idle === "object" && cfg.idle ? cfg.idle : {};
    const groups = Array.isArray(idle.groups) ? idle.groups.filter((group) => GESTURE_GROUPS.includes(group)) : GESTURE_GROUPS;
    return {
        buddy: cleanBuddy(cfg?.character),
        accessory: cleanAccessory(cfg?.accessory),
        idle: {
            enabled: idle.enabled !== false,
            frequency: cleanFrequency(idle.frequency),
            groups: [...new Set(groups)],
        },
    };
}

/** Gestos que un buddy con ese accesorio puede hacer con los grupos permitidos. */
export function availableGestures({ buddy, accessory, groups }) {
    const taken = ACCESSORIES[cleanAccessory(accessory)].slot;
    return Object.entries(GESTURES)
        .filter(([, gesture]) => {
            if (!groups.includes(gesture.group)) {
                return false;
            }
            if (gesture.buddies && !gesture.buddies.includes(buddy)) {
                return false;
            }
            const slot = gesture.accessory ? ACCESSORIES[gesture.accessory].slot : null;
            return !(slot && slot === taken);
        })
        .map(([id]) => id);
}

export function nudgeGesture({ buddy, accessory, groups }) {
    return availableGestures({ buddy: cleanBuddy(buddy), accessory, groups: Array.isArray(groups) ? groups : [] }).includes(NUDGE_GESTURE) ? NUDGE_GESTURE : null;
}

/** Elige un gesto al azar (con pesos), sin repetir el último. `random` es inyectable para las pruebas. */
export function pickGesture({ buddy, accessory, groups, last = null, random = Math.random }) {
    let ids = availableGestures({ buddy: cleanBuddy(buddy), accessory, groups });
    if (ids.length > 1) {
        ids = ids.filter((id) => id !== last);
    }
    const total = ids.reduce((sum, id) => sum + GESTURES[id].weight, 0);
    if (!total) {
        return null;
    }
    let roll = random() * total;
    for (const id of ids) {
        roll -= GESTURES[id].weight;
        if (roll < 0) {
            return id;
        }
    }
    return ids.at(-1);
}
