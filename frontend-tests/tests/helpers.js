import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { JSDOM } from "jsdom";

const ADDON = path.resolve(import.meta.dirname, "../../odoo-addons/buddy_ia/static/src");
const mountedApps = [];

export function destroyApps() {
    for (const app of mountedApps.splice(0)) {
        app.destroy();
    }
}

export function setupDom() {
    const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true, url: "http://localhost/" });
    const w = dom.window;
    // Expone al scope global todo lo que OWL espera encontrar en un navegador (DOMTokenList, Node, etc.).
    for (const key of Object.getOwnPropertyNames(w)) {
        if (!(key in globalThis)) {
            Object.defineProperty(globalThis, key, { get: () => w[key], configurable: true });
        }
    }
    for (const key of ["window", "document", "sessionStorage", "localStorage", "requestAnimationFrame", "cancelAnimationFrame"]) {
        Object.defineProperty(globalThis, key, { value: key === "window" ? w : w[key].bind ? w[key].bind(w) : w[key], configurable: true, writable: true });
    }
    Object.defineProperty(globalThis, "document", { value: w.document, configurable: true, writable: true });
    Object.defineProperty(globalThis, "sessionStorage", { value: w.sessionStorage, configurable: true, writable: true });
    Object.defineProperty(globalThis, "localStorage", { value: w.localStorage, configurable: true, writable: true });
    return dom;
}

export function loadTemplates() {
    const inner = (file) => {
        const xml = readFileSync(path.join(ADDON, "xml", file), "utf-8");
        return xml.replace(/<\?xml[^>]*\?>/, "").replace(/<\/?templates[^>]*>/g, "");
    };
    // Every avatar file (the container plus one per buddy), then the widget.
    const avatars = readdirSync(path.join(ADDON, "xml")).filter((file) => /^buddy_avatar.*\.xml$/.test(file)).sort();
    return `<templates>${avatars.map(inner).join("")}${inner("buddy_look.xml")}${inner("buddy.xml")}</templates>`;
}

/** The template of the field widget of the folder form (separate file: it is not part of the chat widget). */
export function loadSyncTemplates() {
    const xml = readFileSync(path.join(ADDON, "xml", "buddy_sync_refresh.xml"), "utf-8");
    return `<templates>${xml.replace(/<\?xml[^>]*\?>/, "").replace(/<\/?templates[^>]*>/g, "")}</templates>`;
}

/** Mounts any component with the real templates (the avatar and the settings widgets, for instance). */
export async function mountComponent(Component, props = {}) {
    const { App } = await import("@odoo/owl");
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = new App(Component, { templates: loadTemplates(), test: true, props });
    mountedApps.push(app);
    const comp = await app.mount(target);
    return { app, target, comp };
}

export const flush = (ms = 60) => new Promise((r) => setTimeout(r, ms));

export function fakeBackend(overrides = {}) {
    globalThis.__rpcCalls = [];
    globalThis.__fetchCalls = [];
    globalThis.fetch = fakeFetch;
    globalThis.__translations = {};
    globalThis.__services = { action: { doAction: (action) => (globalThis.__actions ??= []).push(action) } };
    globalThis.__actions = [];
    globalThis.__rpc = async (route, params) => {
        if (overrides[route]) {
            return overrides[route](params);
        }
        const defaults = {
            "/buddy/config": () => ({
                first_name: "Ana", buddy_name: "Buddy", session_generation: 0,
                enabled: true, greeting: "", tagline: "", suggestions: [], primary_color: "", configured: true,
            }),
            "/buddy/chat": () => ({ answer: "Hola", expression: "happy", ring: "ok", sources: [], ticket: null, contact: null, provider: "gemini" }),
            "/buddy/ticket": () => ({ id: 7, name: "Ticket de prueba" }),
            "/buddy/forget": () => ({ ok: true, session_generation: 1 }),
            "/buddy/history": () => ({ messages: [], session_generation: 0 }),
            "/buddy/feedback": (params) => ({ ok: true, vote: params.vote === "none" ? null : params.vote }),
            "/buddy/journey": () => ({ ok: true, journeys: [], pending: 0 }),
            "/buddy/journey/step": () => ({ ok: true, journeys: [], pending: 0 }),
            "/buddy/suggestions": () => ({ ok: true, suggestions: [] }),
        };
        return defaults[route](params);
    };
}

const encoder = new TextEncoder();
export const frame = (event, data) => `event: ${event}
data: ${JSON.stringify(data)}

`;

/** A fetch Response with an SSE body that the test feeds by hand: push(text|bytes), end(), fail(error). */
export function sseResponse({ status = 200, type = "text/event-stream; charset=utf-8" } = {}) {
    let controller;
    const body = new ReadableStream({ start: (c) => (controller = c) });
    return {
        response: new Response(body, { status, headers: { "content-type": type } }),
        push: (chunk) => controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk),
        end: () => controller.close(),
        fail: (error = new Error("connection lost")) => controller.error(error),
    };
}

export const isJourneyUrl = (url) => url === "/buddy/journey" || url === "/buddy/journey/step" || url === "/buddy/suggestions";

/** The onboarding routes and the suggestions (plain JSON, like /buddy/feedback): the handler of the route answers, `__status` is the HTTP status. */
export function journeyFetch(url, options = {}) {
    const body = JSON.parse(options.body);
    globalThis.__rpcCalls.push({ route: url, params: body, headers: options.headers });
    return (async () => {
        const { __status = 200, ...payload } = await globalThis.__rpc(url, body);
        return new Response(JSON.stringify(payload), { status: __status, headers: { "content-type": "application/json" } });
    })();
}

/**
 * Default fetch of the tests: /buddy/chat_stream answers with what the (overridable) "/buddy/chat" handler returns,
 * as one delta plus `done`/`error`. A handler that throws becomes a stream that opens and then breaks.
 */
function fakeFetch(url, options = {}) {
    if (isJourneyUrl(url)) {
        // The onboarding routes are not «chat» fetches: the tests that count those calls do not see them.
        return journeyFetch(url, options);
    }
    globalThis.__fetchCalls.push({ url, options });
    if (url === "/buddy/feedback") {
        // A plain JSON route: the handler of "/buddy/feedback" answers; `__status` (optional) is the HTTP status.
        const body = JSON.parse(options.body);
        globalThis.__rpcCalls.push({ route: "/buddy/feedback", params: body, headers: options.headers });
        return (async () => {
            const { __status = 200, ...payload } = await globalThis.__rpc("/buddy/feedback", body);
            return new Response(JSON.stringify(payload), { status: __status, headers: { "content-type": "application/json" } });
        })();
    }
    if (url !== "/buddy/chat_stream") {
        return Promise.reject(new Error("unexpected fetch " + url));
    }
    return (async () => {
        const stream = sseResponse();
        stream.push(": open\n\n");
        const body = JSON.parse(options.body);
        // Existing tests count the chat calls through calls("/buddy/chat"): the streamed request is one of them.
        globalThis.__rpcCalls.push({ route: "/buddy/chat", params: body });
        Promise.resolve()
            .then(() => globalThis.__rpc("/buddy/chat", body))
            .then((res) => {
                if (options.signal?.aborted) {
                    return;
                }
                if (res.error) {
                    stream.push(frame("error", res));
                } else {
                    stream.push(frame("delta", { text: String(res.answer ?? "") }));
                    stream.push(frame("done", { ...res, answer: String(res.answer ?? "") }));
                }
                stream.end();
            })
            .catch((error) => stream.fail(error));
        options.signal?.addEventListener("abort", () => {
            try {
                stream.fail(new DOMException("aborted", "AbortError"));
            } catch {
                // already closed
            }
        });
        return stream.response;
    })();
}

export async function mountBuddy() {
    const { App } = await import("@odoo/owl");
    const { Buddy } = await import("../../odoo-addons/buddy_ia/static/src/js/buddy.js");
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = new App(Buddy, { templates: loadTemplates(), test: true });
    mountedApps.push(app);
    const comp = await app.mount(target);
    // The first render compiles the avatar templates (big SVGs): wait for the widget to be ready, not a fixed time.
    for (let waited = 0; !comp.state.ready && waited < 3000; waited += 25) {
        await flush(25);
    }
    await flush();
    return { app, target, comp };
}

export const calls = (route) => globalThis.__rpcCalls.filter((c) => c.route === route);
