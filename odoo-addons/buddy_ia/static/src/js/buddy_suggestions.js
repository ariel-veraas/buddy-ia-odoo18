/** @odoo-module **/

// Suggestions that fit the person for the chips of the widget (E4b): the route that brings them, the filter every value of
// the server goes through before it reaches the screen and the choice of the three or four that are shown. Pure functions and one
// fetch helper: no component, so the tests load it on its own.

const KINDS = new Set(["step", "topic", "next"]);
const MAX_POOL = 8;
const TEXT_CHARS = 200;
const LABEL_CHARS = 80;
// What the panel shows at most: three that fit the person plus the fixed ones of the company until there are four.
export const MAX_CHIPS = 4;
export const MAX_CONTEXTUAL = 3;
// The questions of the steps that are due today or late always come first (at most this many of them).
const URGENT_CHIPS = 2;

// Control characters (C0 and C1) and the invisible ones that make a text different from what it looks like: zero-width spaces, the marks
// that change the direction of the text (U+202A to U+202E, U+2066 to U+2069) and the byte order mark. Same as `clean_text` of the server.
const INVISIBLE = /[\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]+/g;
const text = (value, limit) =>
    typeof value === "string" ? value.replace(/[\u0000-\u001f]+/g, " ").replace(INVISIBLE, "").replace(/\s+/g, " ").trim().slice(0, limit) : "";

/** One question compared with another: capitals, accents, spaces and the question and exclamation marks aside. */
export function normalizeQuestion(value) {
    return String(value ?? "")
        .normalize("NFD")
        .replace(/[̀-ͯ]/g, "")
        .toLowerCase()
        .replace(/[¿?¡!.,;:«»"']/g, " ")
        .replace(/\s+/g, " ")
        .trim();
}

/** Everything the widget keeps from the server's suggestions: only known kinds, bounded texts, an id only when it is a positive integer. */
export function cleanSuggestions(payload) {
    const found = [];
    const seen = new Set();
    for (const raw of Array.isArray(payload?.suggestions) ? payload.suggestions.slice(0, MAX_POOL * 2) : []) {
        if (!raw || typeof raw !== "object" || !KINDS.has(raw.kind)) {
            continue;
        }
        const question = text(raw.text, TEXT_CHARS);
        const key = normalizeQuestion(question);
        if (!question || seen.has(key)) {
            continue;
        }
        seen.add(key);
        const label = text(raw.label, LABEL_CHARS) || (question.length <= LABEL_CHARS ? question : question.slice(0, LABEL_CHARS - 1).trimEnd() + "…");
        const chip = { kind: raw.kind, text: question, label };
        if (Number.isInteger(raw.ask_id) && raw.ask_id > 0 && raw.ask_id < 2 ** 31) {
            chip.askId = raw.ask_id;
        }
        found.push(chip);
        if (found.length >= MAX_POOL) {
            break;
        }
    }
    return found;
}

/**
 * The chips to show. `pool`: what the server offered (already clean); `generic`: the fixed texts of the company (or the defaults);
 * `asked`: what the person already asked in this conversation (never offered again); `rotation`: a number that grows every time the
 * panel opens, so the suggestions that are not urgent take turns.
 *
 * Order: the questions of the steps due today or late (at most two: they are what the person has to do), then the rest of the pool
 * rotating from `rotation` until there are `contextual` of them, then the fixed texts until there are `max` chips. With nothing from
 * the server the fixed texts are shown as they always were.
 */
export function pickChips({ pool = [], generic = [], asked = [], rotation = 0, max = MAX_CHIPS, contextual = MAX_CONTEXTUAL } = {}) {
    const alreadyAsked = new Set(asked.map(normalizeQuestion));
    const fresh = pool.filter((chip) => !alreadyAsked.has(normalizeQuestion(chip.text)));
    const chosen = fresh.filter((chip) => chip.kind === "step").slice(0, URGENT_CHIPS);
    const rest = fresh.filter((chip) => !chosen.includes(chip));
    const room = Math.min(contextual - chosen.length, rest.length);
    const start = rest.length ? ((Number(rotation) || 0) % rest.length + rest.length) % rest.length : 0;
    for (let index = 0; index < room; index++) {
        chosen.push(rest[(start + index) % rest.length]);
    }
    const shown = new Set(chosen.map((chip) => normalizeQuestion(chip.text)));
    const chips = chosen.map((chip) => ({ ...chip }));
    for (const fixed of generic) {
        const key = normalizeQuestion(fixed);
        if (chips.length >= max) {
            break;
        }
        if (!shown.has(key) && !alreadyAsked.has(key)) {
            shown.add(key);
            chips.push({ kind: "generic", text: fixed, label: fixed });
        }
    }
    return chips.slice(0, max);
}

/**
 * POST to /buddy/suggestions (plain HTTP with a JSON body, protected like /buddy/chat_stream: JSON content type and Odoo's CSRF
 * token in a header). Resolves to `{ ok: true, suggestions }` or `{ ok: false, status }`; throws only when the network fails.
 */
export async function loadSuggestions() {
    const response = await fetch("/buddy/suggestions", {
        method: "POST",
        credentials: "same-origin",
        headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRF-Token": globalThis.odoo?.csrf_token || "",
        },
        body: JSON.stringify({}),
    });
    let data = null;
    try {
        data = await response.json();
    } catch {
        data = null;
    }
    if (response.ok && data && data.ok === true) {
        return { ok: true, suggestions: cleanSuggestions(data) };
    }
    return { ok: false, status: response.status };
}
