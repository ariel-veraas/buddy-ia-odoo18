import { test, before, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountBuddy, flush, calls, destroyApps, sseResponse, frame, isJourneyUrl, journeyFetch } from "./helpers.js";

before(() => setupDom());
afterEach(() => destroyApps());
beforeEach(() => {
    document.body.innerHTML = "";
    sessionStorage.clear();
    localStorage.clear();
});

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const encoder = new TextEncoder();
const FINAL = {
    answer: "Hola **mundo**", expression: "happy", ring: "ok", provider: "llm", error: null, session_generation: 0,
    sources: [{ title: "Manual", owner: "Ops", updated: "2026-01-01", link: "https://docs.google.com/document/d/1" }],
    ticket: null, contact: null,
};

async function ask(text = "¿Cómo hago algo?") {
    $(".o_buddy_launcher").click();
    await flush();
    const input = $(".o_buddy_input");
    input.value = text;
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    await flush();
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await flush();
}

/** Mount with a hand-fed stream: returns the controls of the (single) streamed request. */
async function withStream(options) {
    fakeBackend();
    const stream = sseResponse(options);
    globalThis.fetch = (url, init) => {
        if (isJourneyUrl(url)) {
            return journeyFetch(url, init);
        }
        globalThis.__fetchCalls.push({ url, options: init });
        init.signal?.addEventListener("abort", () => {
            try {
                stream.fail(new DOMException("aborted", "AbortError"));
            } catch {
                // already closed
            }
        });
        return Promise.resolve(stream.response);
    };
    await mountBuddy();
    return stream;
}

const lastBuddy = () => $$(".is-buddy:not(.o_buddy_typing)").at(-1);

// ------------------------------------------------------------------------------------------ parser
test("SSEParser: eventos partidos entre lecturas, UTF-8 partido, CRLF, comentarios", async () => {
    const { SSEParser } = await import("../../odoo-addons/buddy_ia/static/src/js/buddy.js");
    const bytes = encoder.encode(': hola\r\nevent: delta\r\ndata: {"text":"ñandú 😀"}\r\n\r\ndata: a\ndata: b\n\n');
    for (const size of [1, 2, 3, 5, 1000]) {
        const parser = new SSEParser();
        const events = [];
        for (let i = 0; i < bytes.length; i += size) {
            events.push(...parser.feed(bytes.slice(i, i + size)));
        }
        assert.deepEqual(events, [
            { event: "delta", data: '{"text":"ñandú 😀"}' },
            { event: "message", data: "a\nb" },
        ], `tamaño ${size}`);
    }
    const parser = new SSEParser();
    assert.deepEqual(parser.feedText("data: x\r"), []);
    assert.deepEqual(parser.feedText("\n\r"), []);
    assert.deepEqual(parser.feedText("\n"), [{ event: "message", data: "x" }]);
    assert.deepEqual(parser.feedText("data: sin cierre"), []);
});

// ----------------------------------------------------------------------------------- incremental
test("el texto crece a medida que llega, con el indicador hasta el primer delta, y done lo reemplaza", async () => {
    const stream = await withStream();
    globalThis.odoo = { csrf_token: "tok-123" };
    await ask();
    delete globalThis.odoo;
    const request = globalThis.__fetchCalls[0];
    assert.equal(request.options.headers["X-CSRF-Token"], "tok-123");
    assert.equal(request.url, "/buddy/chat_stream");
    assert.equal(request.options.method, "POST");
    assert.equal(request.options.credentials, "same-origin");
    assert.equal(request.options.headers["Content-Type"], "application/json");
    assert.ok(request.options.signal instanceof AbortSignal);
    const body = JSON.parse(request.options.body);
    assert.equal(body.message, "¿Cómo hago algo?");
    assert.match(body.request_id, /^[0-9a-f-]{36}$/);
    assert.equal(calls("/buddy/chat").length, 0, "no usa la ruta JSON si el stream anda");

    assert.ok($(".o_buddy_typing"), "indicador hasta el primer delta");
    stream.push(": open\n\n");
    await flush();
    assert.ok($(".o_buddy_typing"), "un comentario no es texto");

    stream.push(frame("delta", { text: "Hola " }));
    await flush(80);
    assert.equal($(".o_buddy_typing"), null);
    assert.equal(lastBuddy().querySelector(".o_buddy_text").textContent.trim(), "Hola");
    assert.ok(lastBuddy().classList.contains("is-streaming"));
    assert.equal($(".o_buddy_messages").getAttribute("aria-busy"), "true");
    assert.equal($(".o_buddy_send"), null, "mientras escribe no hay botón de enviar: hay uno para detener");
    assert.ok($(".o_buddy_stop"));

    stream.push(frame("delta", { text: "**mun" }));
    stream.push(frame("delta", { text: "do** y más" }));
    await flush(80);
    assert.equal(lastBuddy().querySelector("strong").textContent, "mundo");
    assert.match(lastBuddy().textContent, /Hola mundo y más/);

    stream.push(frame("done", FINAL));
    stream.end();
    await flush(80);
    const message = lastBuddy();
    assert.equal(message.textContent.includes("y más"), false, "la respuesta final reemplaza al texto en curso");
    assert.equal(message.querySelector("strong").textContent, "mundo");
    assert.equal(message.classList.contains("is-streaming"), false);
    assert.equal($$(".is-buddy:not(.o_buddy_typing)").length, 2, "el saludo y una sola respuesta (no se duplica)");
    assert.equal(message.querySelector(".o_buddy_source a").getAttribute("href"), "https://docs.google.com/document/d/1");
    assert.equal($(".o_buddy_messages").getAttribute("aria-busy"), "false");
    assert.equal($(".o_buddy_send").disabled, true, "sin texto en el cuadro sigue deshabilitado");
});

test("done trae tarjeta de contacto y propuesta de ticket, con la clave del chat", async () => {
    const stream = await withStream();
    await ask();
    stream.push(frame("delta", { text: "No sé." }));
    stream.push(frame("done", {
        ...FINAL, answer: "No sé.", ring: "doubt", expression: "doubt", sources: [],
        ticket: { title: "Consulta: x", description: "d" },
        contact: { role: "general", label: "Mesa", name: "Ana", contact: "ana@example.com" },
    }));
    stream.end();
    await flush(80);
    assert.match(lastBuddy().textContent, /Te conviene hablar con/);
    assert.match(lastBuddy().textContent, /Consulta: x/);
    $(".o_buddy_btn").click();
    await flush();
    assert.equal(calls("/buddy/ticket")[0].params.request_key, JSON.parse(globalThis.__fetchCalls[0].options.body).request_id);
});

test("el markdown del stream se dibuja como nodos, nunca como HTML", async () => {
    const stream = await withStream();
    await ask();
    stream.push(frame("delta", { text: "<img src=x onerror=\"window.PWNED=1\"> **<b>x</b>**" }));
    await flush(80);
    assert.equal($(".o_buddy_text img"), null);
    assert.equal($(".o_buddy_text b"), null);
    assert.equal(window.PWNED, undefined);
    stream.end();
    await flush();
});

test("muchos fragmentos en el mismo instante se dibujan con pocos renders", async () => {
    const stream = await withStream();
    await ask();
    const original = globalThis.requestAnimationFrame;
    let frames = 0;
    globalThis.requestAnimationFrame = (cb) => { frames++; return original(cb); };
    try {
        for (let i = 0; i < 40; i++) {
            stream.push(frame("delta", { text: "x" }));
        }
        await flush(120);
    } finally {
        globalThis.requestAnimationFrame = original;
    }
    assert.ok(frames >= 1 && frames <= 4, `renders: ${frames}`);
    assert.equal(lastBuddy().querySelector(".o_buddy_text").textContent.trim(), "x".repeat(40));
    stream.end();
    await flush();
});

test("auto-scroll: sigue el texto solo si la persona estaba abajo", async () => {
    const stream = await withStream();
    await ask();
    const el = $(".o_buddy_messages");
    let top = 0;
    Object.defineProperty(el, "scrollHeight", { get: () => 1000, configurable: true });
    Object.defineProperty(el, "clientHeight", { get: () => 300, configurable: true });
    Object.defineProperty(el, "scrollTop", { get: () => top, set: (value) => { top = value; }, configurable: true });
    top = 700; // abajo
    stream.push(frame("delta", { text: "uno" }));
    await flush(80);
    assert.equal(top, 1000, "seguía abajo: acompaña");
    top = 100; // la persona subió a leer
    stream.push(frame("delta", { text: " dos" }));
    await flush(80);
    assert.equal(top, 100, "no la arrastra");
    stream.end();
    await flush();
});

// ------------------------------------------------------------------------------------------ errors
test("un error a mitad reemplaza el texto parcial por el mensaje amable con Reintentar", async () => {
    const stream = await withStream();
    await ask();
    stream.push(frame("delta", { text: "Texto parcial" }));
    await flush(80);
    stream.push(frame("error", { error: "provider_error", answer: "No pude consultar la documentación ahora." }));
    stream.end();
    await flush(80);
    assert.equal($$(".is-buddy:not(.o_buddy_typing)").length, 2);
    assert.equal(lastBuddy().textContent.includes("parcial"), false);
    assert.match(lastBuddy().textContent, /No pude consultar la documentación ahora\./);
    assert.ok($(".o_buddy_retry_btn"));
    assert.equal($(".o_buddy_send").disabled, true);
});

test("un error antes del primer texto muestra el mensaje y no cae a la ruta JSON", async () => {
    const stream = await withStream();
    await ask();
    stream.push(frame("error", { error: "rate_limited", answer: "Estás consultando muy seguido." }));
    stream.end();
    await flush(80);
    assert.match(lastBuddy().textContent, /consultando muy seguido/);
    assert.ok($(".o_buddy_retry_btn"));
    assert.equal(calls("/buddy/chat").length, 0);
    assert.equal(globalThis.__fetchCalls.length, 1);
});

test("session_changed por stream limpia la conversación y adopta la generación nueva", async () => {
    const stream = await withStream();
    await ask();
    stream.push(frame("delta", { text: "x" }));
    stream.push(frame("error", { error: "session_changed", answer: "Cambió la sesión", session_generation: 5 }));
    stream.end();
    await flush(80);
    assert.equal($$(".is-user").length, 0);
    assert.match(lastBuddy().textContent, /Cambió la sesión/);
});

test("el stream que se corta DESPUÉS de recibir bytes es un error, sin reintentar por JSON", async () => {
    const stream = await withStream();
    await ask();
    stream.push(frame("delta", { text: "Parcial" }));
    await flush(80);
    stream.fail(new Error("connection reset"));
    await flush(80);
    assert.match(lastBuddy().textContent, /Se me cortó la conexión/);
    assert.equal(lastBuddy().textContent.includes("Parcial"), false);
    assert.ok($(".o_buddy_retry_btn"));
    assert.equal(calls("/buddy/chat").length, 0, "reintentar por JSON podría duplicar la consulta");
});

test("un stream que termina sin done es un error", async () => {
    const stream = await withStream();
    await ask();
    stream.push(frame("delta", { text: "Parcial" }));
    stream.end();
    await flush(80);
    assert.match(lastBuddy().textContent, /Se me cortó la conexión/);
});

// -------------------------------------------------------------------------------------------- abort
test("'Nueva conversación' aborta el stream y descarta lo que llegue después; cerrar el panel no", async () => {
    const stream = await withStream();
    await ask();
    stream.push(frame("delta", { text: "Escribiendo" }));
    await flush(80);
    const signal = globalThis.__fetchCalls[0].options.signal;
    $(".o_buddy_close").click();
    await flush();
    assert.equal(signal.aborted, false, "cerrar el panel no aborta");
    $(".o_buddy_launcher").click();
    await flush();
    assert.match(lastBuddy().textContent, /Escribiendo/, "el texto siguió creciendo con el panel cerrado");
    $(".o_buddy_new_btn").click();
    await flush();
    $(".o_buddy_confirm_yes").click();
    await flush(100);
    assert.equal(signal.aborted, true);
    assert.equal($$(".is-user").length, 0);
    assert.match($(".is-buddy").textContent, /borré nuestra conversación/);
    assert.equal($(".o_buddy_typing"), null);
    assert.equal($(".o_buddy_send").disabled, true);
});

// ------------------------------------------------------------------------------------------ fallback
async function fallbackCase(fetchImpl, prepare) {
    fakeBackend({ "/buddy/chat": () => ({ ...FINAL, answer: "Por JSON" }) });
    globalThis.fetch = (url, init) => {
        if (isJourneyUrl(url)) {
            return journeyFetch(url, init);
        }
        globalThis.__fetchCalls.push({ url, options: init });
        return fetchImpl(url, init);
    };
    await mountBuddy();
    if (prepare) {
        prepare();
    }
    await ask();
    await flush(100);
}

test("respaldo: 404, 415 o una página de login (no es event-stream) reintentan UNA vez por JSON con el mismo request_id", async () => {
    for (const make of [
        () => new Response("nope", { status: 404 }),
        () => new Response("x", { status: 415, headers: { "content-type": "text/plain" } }),
        () => new Response("<html>login</html>", { status: 200, headers: { "content-type": "text/html" } }),
    ]) {
        document.body.innerHTML = "";
        destroyApps();
        await fallbackCase(async () => make());
        assert.equal(globalThis.__fetchCalls.length, 1);
        assert.equal(calls("/buddy/chat").length, 1);
        assert.equal(calls("/buddy/chat")[0].params.request_id, JSON.parse(globalThis.__fetchCalls[0].options.body).request_id);
        assert.match(lastBuddy().textContent, /Por JSON/);
    }
});

test("respaldo: fetch que falla antes de conectar, o un stream que se corta sin un solo byte", async () => {
    await fallbackCase(async () => { throw new TypeError("Failed to fetch"); });
    assert.equal(calls("/buddy/chat").length, 1);
    assert.match(lastBuddy().textContent, /Por JSON/);
    document.body.innerHTML = "";
    destroyApps();
    await fallbackCase(async () => {
        const stream = sseResponse();
        queueMicrotask(() => stream.fail(new Error("proxy cut")));
        return stream.response;
    });
    assert.equal(calls("/buddy/chat").length, 1);
    assert.match(lastBuddy().textContent, /Por JSON/);
});

test("respaldo: navegador sin streaming (sin response.body o sin fetch)", async () => {
    await fallbackCase(async () => ({ ok: true, headers: new Headers({ "content-type": "text/event-stream" }), body: null }));
    assert.equal(calls("/buddy/chat").length, 1);
    assert.match(lastBuddy().textContent, /Por JSON/);
    document.body.innerHTML = "";
    destroyApps();
    await fallbackCase(async () => { throw new Error("no se llama"); }, () => { globalThis.fetch = undefined; });
    assert.equal(calls("/buddy/chat").length, 1);
    assert.match(lastBuddy().textContent, /Por JSON/);
});

test("el respaldo es de UNA sola vez: si la ruta JSON también falla, error con Reintentar", async () => {
    fakeBackend({ "/buddy/chat": () => { throw new Error("network"); } });
    globalThis.fetch = async () => new Response("x", { status: 502 });
    await mountBuddy();
    await ask();
    await flush(100);
    assert.equal(calls("/buddy/chat").length, 1);
    assert.match(lastBuddy().textContent, /Se me cortó la conexión/);
    assert.ok($(".o_buddy_retry_btn"));
});
