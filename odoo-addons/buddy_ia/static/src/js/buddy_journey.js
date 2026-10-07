/** @odoo-module **/

// Onboarding tasks for the widget («Tus tareas de hoy»): the two routes and the filters every value of the server goes through
// before it reaches the screen. Pure functions and one fetch helper: no component, so the tests load it on its own.

import { _t } from "@web/core/l10n/translation";

const KINDS = new Set(["task", "read", "ask"]);
const STATES = new Set(["done", "overdue", "today", "upcoming"]);
// The same shape the server accepts for a document link (Drive's own hosts) and for any other address (plain https).
const DRIVE_LINK = /^https:\/\/(?:docs|drive)\.google\.com\/[^\s\\\x00-\x1f\x7f]*$/;
const BAD_URL_CHARS = /[\x00-\x20\x7f\\]/;
const HOST = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/;
// The last label is a real top-level domain (letters or punycode): no IP addresses in any notation, no internal names.
const TLD = /^(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{1,59})$/;
// An address to write to: something@something, no spaces, quotes or angle brackets (it ends up after «mailto:»).
const EMAIL = /^[^\s<>"'@?&#%,;]+@[^\s<>"'@?&#%,;]+$/;
const MAX_TASKS = 60;
const MAX_JOURNEYS = 5;

/** A plain https link with a real host and no credentials, or "" (the server filters it too: this is the second lock). */
export function safeHttpsUrl(url) {
    if (typeof url !== "string" || !url || url.length > 500 || BAD_URL_CHARS.test(url) || !url.startsWith("https://")) {
        return "";
    }
    try {
        const parsed = new URL(url);
        const ok =
            parsed.protocol === "https:" &&
            !parsed.username &&
            !parsed.password &&
            HOST.test(parsed.hostname) &&
            !parsed.hostname.endsWith(".") &&
            TLD.test(parsed.hostname.split(".").pop());
        return ok ? url : "";
    } catch {
        return "";
    }
}

const text = (value, limit) => (typeof value === "string" ? value.slice(0, limit) : "");
const count = (value, max) => (Number.isInteger(value) && value >= 0 ? Math.min(value, max) : 0);

function cleanLink(link) {
    if (!link || typeof link !== "object") {
        return null;
    }
    const url = link.kind === "doc" ? (DRIVE_LINK.test(link.url) ? link.url : "") : safeHttpsUrl(link.url);
    if (!url) {
        return null;
    }
    return { kind: link.kind === "doc" ? "doc" : "url", label: text(link.label, 120), url };
}

function cleanContact(contact) {
    if (!contact || typeof contact !== "object") {
        return null;
    }
    const label = text(contact.label, 80);
    const email = text(contact.contact, 200);
    return label ? { label, name: text(contact.name, 80), contact: EMAIL.test(email) ? email : "" } : null;
}

export function cleanTask(raw) {
    if (!raw || typeof raw !== "object" || !Number.isInteger(raw.id) || raw.id < 1) {
        return null;
    }
    const title = text(raw.title, 120);
    if (!title) {
        return null;
    }
    return {
        id: raw.id,
        title,
        detail: text(raw.detail, 600),
        kind: KINDS.has(raw.kind) ? raw.kind : "task",
        state: STATES.has(raw.state) ? raw.state : "today",
        daysLate: count(raw.days_late, 3650),
        daysUntil: count(raw.days_until, 3650),
        required: raw.required !== false,
        done: raw.done === true,
        link: cleanLink(raw.link),
        question: text(raw.question, 200).trim(),
        contact: cleanContact(raw.contact),
        busy: false,
    };
}

/** Everything the widget keeps about the person's journeys: only known fields, only sane values. */
export function cleanJourneys(payload) {
    const journeys = [];
    for (const raw of Array.isArray(payload?.journeys) ? payload.journeys.slice(0, MAX_JOURNEYS) : []) {
        if (!raw || typeof raw !== "object" || !text(raw.name, 120)) {
            continue;
        }
        const tasks = (Array.isArray(raw.tasks) ? raw.tasks : []).slice(0, MAX_TASKS).map(cleanTask).filter(Boolean);
        const upcoming = (Array.isArray(raw.upcoming) ? raw.upcoming : []).slice(0, 5).map(cleanTask).filter(Boolean);
        const total = count(raw.total, 10000);
        journeys.push({
            name: text(raw.name, 120),
            day: Math.max(count(raw.day, 100000), 1),
            percent: Math.min(count(raw.percent, 100), 100),
            done: count(raw.done, 10000),
            total,
            pending: count(raw.pending, 10000),
            tasks,
            upcoming,
            upcomingTotal: Math.max(count(raw.upcoming_total, 10000), upcoming.length),
        });
    }
    return { journeys, pending: journeys.reduce((sum, journey) => sum + journey.pending, 0) };
}

/** POST to one of the two onboarding routes (plain HTTP with a JSON body, protected like /buddy/chat_stream: JSON content type
 *  and Odoo's CSRF token in a header). Resolves to `{ ok: true, journeys, pending }` or `{ ok: false, status, message }`. */
async function post(path, body) {
    const response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRF-Token": globalThis.odoo?.csrf_token || "",
        },
        body: JSON.stringify(body),
    });
    let data = null;
    try {
        data = await response.json();
    } catch {
        data = null;
    }
    if (response.ok && data && data.ok === true) {
        return { ok: true, ...cleanJourneys(data) };
    }
    return { ok: false, status: response.status, message: typeof data?.message === "string" ? data.message.slice(0, 300) : "" };
}

export const loadJourneys = () => post("/buddy/journey", {});
export const setStepDone = (id, done) => post("/buddy/journey/step", { id, done });

// ------------------------------------------------------------------ texts (lazy: they need the user's language)
export function summaryText(pending) {
    if (!pending) {
        return _t("Todo al día");
    }
    return pending === 1 ? _t("1 pendiente") : _t("%(count)s pendientes", { count: pending });
}

/** When an upcoming step is due: «mañana», «en 3 días». */
export function untilText(days) {
    return days <= 1 ? _t("mañana") : _t("en %(days)s días", { days });
}

/** The line of the small bubble that tells about the tasks of the day: «Tenés 2 tareas para hoy» (or «pendientes» when one is late). Empty
 *  when nothing is due. `pending` is the number of required steps due today or late; `late` says whether any of them is late. */
export function nudgeText({ pending, late }) {
    const count = Number.isInteger(pending) && pending > 0 ? Math.min(pending, 999) : 0;
    if (!count) {
        return "";
    }
    if (late) {
        return count === 1 ? _t("Tenés 1 tarea pendiente") : _t("Tenés %(count)s tareas pendientes", { count });
    }
    return count === 1 ? _t("Tenés 1 tarea para hoy") : _t("Tenés %(count)s tareas para hoy", { count });
}

/** Whether any required step of the (clean) journeys is late and not done. */
export function hasLateTask(journeys) {
    return (Array.isArray(journeys) ? journeys : []).some((journey) =>
        (journey.tasks || []).some((task) => task.required && !task.done && task.state === "overdue")
    );
}

export function lateText(days) {
    return days === 1 ? _t("Atrasada 1 día") : _t("Atrasada %(days)s días", { days });
}
