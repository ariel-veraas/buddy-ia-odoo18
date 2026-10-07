import { test, before, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountBuddy, flush, calls, destroyApps } from "./helpers.js";

before(() => setupDom());
afterEach(() => destroyApps());
beforeEach(() => {
    document.body.innerHTML = "";
    sessionStorage.clear();
    localStorage.clear();
});

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const openPanel = async () => {
    $(".o_buddy_launcher").click();
    await flush();
};
const deferred = () => {
    let resolve, reject;
    const promise = new Promise((res, rej) => ({ resolve, reject } = { resolve: res, reject: rej }));
    return { promise, resolve, reject };
};
const stamp = (date) => date.toISOString().slice(0, 19).replace("T", " ");

const HISTORY = {
    messages: [
        { role: "user", text: "¿Cómo pido vacaciones?", date: stamp(new Date()) },
        {
            role: "assistant", text: "1. Entrá a **Tiempo libre**.", date: stamp(new Date()), expression: "happy", ring: "ok",
            sources: [{ title: "Guía de vacaciones", owner: "RRHH", updated: "2026-07-01", link: "https://docs.google.com/document/d/abc/edit" }],
            ticket: null, contact: null, ticket_key: null,
        },
        {
            role: "user", text: "¿Y el sueldo?", date: stamp(new Date()),
        },
        {
            role: "assistant", text: "Eso no está documentado.", date: stamp(new Date()), expression: "doubt", ring: "doubt",
            sources: [], ticket: { title: "Consulta: sueldo", description: "¿Y el sueldo?" },
            contact: { role: "rrhh", label: "RR.HH.", name: "Laura", contact: "rrhh@example.com" },
            ticket_key: "5d6c5b1e-3f0a-4c55-9a52-0f6a9a3f1b11",
        },
    ],
    session_generation: 4,
};

test("al montar pide el historial y lo muestra sin saludo ni sugerencias", async () => {
    fakeBackend({ "/buddy/history": () => HISTORY });
    await mountBuddy();
    assert.equal(calls("/buddy/history").length, 1);
    await openPanel();
    assert.deepEqual($$(".is-user").map((e) => e.textContent), ["¿Cómo pido vacaciones?", "¿Y el sueldo?"]);
    assert.doesNotMatch($(".o_buddy_messages").textContent, /Soy Buddy/, "sin saludo inicial");
    assert.equal($$(".o_buddy_chip").length, 0, "sin sugerencias");
    assert.match($(".o_buddy_messages").textContent, /Tiempo libre/);
    assert.equal($(".o_buddy_divider"), null, "es del mismo día: sin separador");
});

test("las fuentes, la tarjeta de contacto y la propuesta de ticket se ven como antes", async () => {
    fakeBackend({ "/buddy/history": () => HISTORY });
    await mountBuddy();
    await openPanel();
    const link = $(".o_buddy_source a");
    assert.equal(link.getAttribute("href"), "https://docs.google.com/document/d/abc/edit");
    assert.equal(link.textContent, "Guía de vacaciones");
    assert.match($(".o_buddy_card").textContent, /RR\.HH\./);
    assert.ok($(".o_buddy_btn"), "el botón de crear ticket sigue disponible");
});

test("el ticket de una conversación restaurada reutiliza la clave de su respuesta (no se duplica)", async () => {
    fakeBackend({ "/buddy/history": () => HISTORY });
    await mountBuddy();
    await openPanel();
    $(".o_buddy_btn").click();
    await flush();
    const ticketCall = calls("/buddy/ticket")[0].params;
    assert.equal(ticketCall.request_key, "5d6c5b1e-3f0a-4c55-9a52-0f6a9a3f1b11");
    assert.equal(ticketCall.session_generation, 4);
});

test("la siguiente consulta usa la generación del historial", async () => {
    fakeBackend({ "/buddy/history": () => HISTORY });
    const { comp } = await mountBuddy();
    await comp.send("otra cosa");
    assert.equal(calls("/buddy/chat")[0].params.session_generation, 4);
    assert.equal($$(".is-user").length, 0, "el panel está cerrado");
    assert.equal(comp.state.messages.filter((m) => m.role === "user").length, 3);
});

test("el scroll queda abajo al abrir", async () => {
    fakeBackend({ "/buddy/history": () => HISTORY });
    await mountBuddy();
    // jsdom has no layout: scrollHeight is a stub, so the proof is that the effect assigned it.
    Object.defineProperty(window.HTMLElement.prototype, "scrollHeight", { configurable: true, get: () => 999 });
    try {
        await openPanel();
        assert.equal($(".o_buddy_messages").scrollTop, 999);
    } finally {
        delete window.HTMLElement.prototype.scrollHeight;
    }
});

test("si el último mensaje es de otro día aparece el separador 'Conversación anterior' al final", async () => {
    const old = { ...HISTORY, messages: HISTORY.messages.map((m) => ({ ...m, date: "2026-01-05 12:00:00" })) };
    fakeBackend({ "/buddy/history": () => old });
    const { comp } = await mountBuddy();
    await openPanel();
    const divider = $(".o_buddy_divider");
    assert.ok(divider);
    assert.match(divider.textContent, /Conversación anterior/);
    assert.equal(divider.getAttribute("role"), "separator");
    assert.equal(comp.state.messages.at(-1).role, "divider", "queda debajo: lo nuevo se escribe después");
});

test("el separador pasa por la traducción", async () => {
    const old = { ...HISTORY, messages: HISTORY.messages.map((m) => ({ ...m, date: "2026-01-05 12:00:00" })) };
    fakeBackend({ "/buddy/history": () => old });
    globalThis.__translations["Conversación anterior · %(date)s"] = "Previous conversation · %(date)s";
    await mountBuddy();
    await openPanel();
    assert.match($(".o_buddy_divider").textContent, /Previous conversation/);
});

test("sin mensajes guardados se comporta como siempre: saludo y sugerencias", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    assert.match($(".is-buddy").textContent, /Soy Buddy/);
    assert.equal($$(".o_buddy_chip").length, 3);
});

test("si el historial falla, el widget funciona como una conversación nueva", async () => {
    for (const failing of [
        () => { throw new Error("offline"); },
        () => ({ error: "busy", answer: "Ocupado" }),
        () => ({ messages: "no es una lista", session_generation: 1 }),
        () => ({ messages: [{ role: "user", text: "x" }], session_generation: "1" }),
        () => null,
    ]) {
        document.body.innerHTML = "";
        destroyApps();
        fakeBackend({ "/buddy/history": failing });
        await mountBuddy();
        await openPanel();
        assert.match($(".is-buddy").textContent, /Soy Buddy/);
        assert.equal($$(".o_buddy_chip").length, 3);
    }
});

test("mensajes sin forma válida se ignoran y el texto nunca se interpreta como HTML", async () => {
    const html = '<img src=x onerror="window.PWNED=true">';
    fakeBackend({
        "/buddy/history": () => ({
            session_generation: 2,
            messages: [null, 5, { role: "system", text: "x" }, { role: "user" }, { role: "user", text: html },
                { role: "assistant", text: html, sources: [{ title: html, link: "" }, null, 7], ticket: { title: 3 }, contact: "x" }],
        }),
    });
    await mountBuddy();
    await openPanel();
    assert.match($(".o_buddy_messages").textContent, /<img src=x/);
    assert.equal($(".o_buddy_messages img"), null);
    assert.equal($(".o_buddy_btn"), null, "un ticket inválido no se ofrece");
});

test("abrir el panel mientras llega el historial espera y no muestra un saludo que se pisa", async () => {
    const pending = deferred();
    fakeBackend({ "/buddy/history": () => pending.promise });
    const { comp } = await mountBuddy();
    await openPanel();
    assert.equal(comp.state.messages.length, 0, "todavía no saluda");
    pending.resolve(HISTORY);
    await flush();
    assert.equal($$(".is-user").length, 2);
    assert.doesNotMatch($(".o_buddy_messages").textContent, /Soy Buddy/);
});

test("si el historial tarda y viene vacío, el saludo aparece al terminar", async () => {
    const pending = deferred();
    fakeBackend({ "/buddy/history": () => pending.promise });
    await mountBuddy();
    await openPanel();
    pending.resolve({ messages: [], session_generation: 0 });
    await flush();
    assert.match($(".is-buddy").textContent, /Soy Buddy/);
    assert.equal($$(".o_buddy_chip").length, 3);
});

test("una consulta enviada antes de que llegue el historial espera y no se mezcla", async () => {
    const pending = deferred();
    fakeBackend({ "/buddy/history": () => pending.promise });
    const { comp } = await mountBuddy();
    const sending = comp.send("pregunta rápida");
    await flush();
    assert.equal(calls("/buddy/chat").length, 0, "espera al historial");
    pending.resolve(HISTORY);
    await sending;
    await flush();
    const texts = comp.state.messages.filter((m) => m.role === "user").map((m) => m.text);
    assert.deepEqual(texts, ["¿Cómo pido vacaciones?", "¿Y el sueldo?", "pregunta rápida"]);
    assert.equal(calls("/buddy/chat")[0].params.session_generation, 4);
});

test("un historial que llega después de borrar se descarta", async () => {
    const pending = deferred();
    fakeBackend({ "/buddy/history": () => pending.promise });
    const { comp } = await mountBuddy();
    await comp.forget();
    pending.resolve(HISTORY);
    await flush();
    assert.equal(comp.state.messages.filter((m) => m.role === "user").length, 0);
    assert.equal(comp.sessionGeneration, 1);
});

test("con conversación restaurada no hay globo de saludo", async () => {
    fakeBackend({ "/buddy/history": () => HISTORY });
    await mountBuddy();
    await flush(2300);
    assert.equal($(".o_buddy_bubble"), null);
});

test("'Nueva conversación' está siempre a la vista, pide confirmación y deja el chat limpio", async () => {
    fakeBackend({ "/buddy/history": () => HISTORY });
    const { comp } = await mountBuddy();
    await openPanel();
    const button = $(".o_buddy_new_btn");
    assert.match(button.textContent, /Nueva conversación/);
    assert.equal(button.getAttribute("aria-label"), "Empezar una conversación nueva");
    button.click();
    await flush();
    assert.match($(".o_buddy_confirm").textContent, /Empezar una conversación nueva/);
    assert.equal(calls("/buddy/forget").length, 0);
    $(".o_buddy_confirm_yes").click();
    await flush();
    assert.equal(calls("/buddy/forget").length, 1);
    assert.equal($$(".is-user").length, 0);
    assert.equal(comp.state.restored, false);
    assert.equal(comp.sessionGeneration, 1);
});

test("si la sesión cambió, lo restaurado se reemplaza por el aviso", async () => {
    fakeBackend({
        "/buddy/history": () => HISTORY,
        "/buddy/chat": () => ({ error: "session_changed", answer: "Tus permisos cambiaron.", session_generation: 9 }),
    });
    const { comp } = await mountBuddy();
    await comp.send("hola");
    await flush();
    assert.equal(comp.state.messages.length, 1);
    assert.equal(comp.state.messages[0].text, "Tus permisos cambiaron.");
    assert.equal(comp.sessionGeneration, 9);
});
