import { test, before, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountBuddy, flush, calls, destroyApps } from "./helpers.js";

before(() => setupDom());
afterEach(() => destroyApps());
beforeEach(() => {
    document.body.innerHTML = "";
    sessionStorage.clear();
    localStorage.clear();
    delete globalThis.odoo;
});

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const TOKEN = "Zk3-mQ9_aB7xLw2PqR5tYu"; // 22 URL-safe characters, like secrets.token_urlsafe(16)

const ANSWER = {
    answer: "Son **15 días** hábiles.", expression: "happy", ring: "ok", provider: "llm", error: null, session_generation: 0,
    sources: [{ title: "Guía de reintegros", owner: "Finanzas", updated: "2026-07-01", link: "https://docs.google.com/document/d/1" }],
    ticket: null, contact: null, feedback_token: TOKEN, confidence: "high", conflict: false,
};

const openPanel = async () => {
    $(".o_buddy_launcher").click();
    await flush();
};

async function ask(overrides = {}, handlers = {}, text = "¿En cuánto se reintegra?") {
    destroyApps();
    document.body.innerHTML = "";
    fakeBackend({ "/buddy/chat": () => ({ ...ANSWER, ...overrides }), ...handlers });
    await mountBuddy();
    if (!$(".o_buddy_panel")) {
        await openPanel();
    }
    const input = $(".o_buddy_input");
    input.value = text;
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    await flush();
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await flush(120);
}

const up = () => $("[data-fb-up]");
const down = () => $("[data-fb-down]");
const feedbackCalls = () => calls("/buddy/feedback");

test("las respuestas sobre documentos tienen 👍 y 👎; el saludo y los mensajes del sistema no", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    assert.equal($(".o_buddy_feedback"), null, "el saludo de bienvenida no se vota");
    const input = $(".o_buddy_input");
    input.value = "Hola";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    await flush();
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await flush(120);
    assert.equal($(".o_buddy_feedback"), null, "una respuesta sin código (charla) no se vota");
});

test("un error del servidor no ofrece votar, aunque traiga un código", async () => {
    await ask({ error: "provider_error", answer: "No pude consultar la documentación ahora." });
    assert.equal($(".o_buddy_feedback"), null);
});

test("una respuesta con código muestra los dos botones, accesibles y sin presionar", async () => {
    await ask();
    assert.ok($(".o_buddy_feedback"));
    assert.equal($(".o_buddy_feedback").getAttribute("role"), "group");
    assert.match($(".o_buddy_feedback").getAttribute("aria-label"), /sirvió/);
    assert.equal(up().getAttribute("aria-pressed"), "false");
    assert.equal(down().getAttribute("aria-pressed"), "false");
    assert.match(up().getAttribute("aria-label"), /me sirvió/);
    assert.match(down().getAttribute("aria-label"), /no me sirvió/);
    assert.equal(up().getAttribute("title"), up().getAttribute("aria-label"), "el mismo texto: el lector no lo lee dos veces distinto");
    assert.equal(down().getAttribute("title"), down().getAttribute("aria-label"));
    assert.equal($(".o_buddy_fb_form"), null, "el formulario de motivo no aparece hasta votar 👎");
    assert.equal($$(".o_buddy_feedback i").every((icon) => icon.getAttribute("aria-hidden") === "true"), true);
});

test("el código que no tiene la forma esperada se ignora", async () => {
    await ask({ feedback_token: "corto" });
    assert.equal($(".o_buddy_feedback"), null);
    await ask({ feedback_token: { x: 1 } });
    assert.equal($(".o_buddy_feedback"), null);
});

test("👍 manda el voto con el código, el JSON y el CSRF, y queda presionado", async () => {
    globalThis.odoo = { csrf_token: "tok-csrf" };
    await ask();
    up().click();
    await flush();
    assert.equal(feedbackCalls().length, 1);
    assert.deepEqual(feedbackCalls()[0].params, { token: TOKEN, vote: "up" });
    assert.equal(feedbackCalls()[0].headers["Content-Type"], "application/json");
    assert.equal(feedbackCalls()[0].headers["X-CSRF-Token"], "tok-csrf");
    assert.equal(up().getAttribute("aria-pressed"), "true");
    assert.equal(down().getAttribute("aria-pressed"), "false");
    assert.match($(".o_buddy_fb_status").textContent, /Gracias/);
    assert.equal($(".o_buddy_fb_status").getAttribute("role"), "status", "una región de estado (ya es «polite»: sin aria-live repetido)");
    assert.equal($(".o_buddy_fb_status").hasAttribute("aria-live"), false);
    assert.equal($(".o_buddy_fb_form"), null, "con 👍 no se pide motivo");
});

test("el mismo botón dos veces retira el voto; el otro lo cambia", async () => {
    await ask();
    up().click();
    await flush();
    up().click();
    await flush();
    assert.deepEqual(feedbackCalls().map((c) => c.params.vote), ["up", "none"]);
    assert.equal(up().getAttribute("aria-pressed"), "false");
    assert.match($(".o_buddy_fb_status").textContent, /Voto retirado/, "quien usa lector de pantalla oye que se retiró");
    up().click();
    await flush();
    down().click();
    await flush();
    assert.deepEqual(feedbackCalls().map((c) => c.params.vote), ["up", "none", "up", "down"]);
    assert.equal(up().getAttribute("aria-pressed"), "false");
    assert.equal(down().getAttribute("aria-pressed"), "true");
});

test("👎 registra el voto al instante y ofrece motivo opcional y la casilla de la pregunta, apagada", async () => {
    await ask();
    down().click();
    await flush();
    assert.deepEqual(feedbackCalls()[0].params, { token: TOKEN, vote: "down" }, "el voto cuenta aunque no llene el formulario");
    assert.ok($(".o_buddy_fb_form"));
    assert.match($(".o_buddy_fb_status").textContent, /Anotamos tu voto/, "se anuncia que el voto ya cuenta");
    assert.ok($(".o_buddy_fb_form [role=radiogroup]"), "los motivos son un grupo de radios con nombre");
    assert.equal($(".o_buddy_fb_form [role=radiogroup]").getAttribute("aria-labelledby"), $(".o_buddy_fb_legend").id);
    assert.deepEqual($$(".o_buddy_fb_form input[type=radio]").map((i) => i.value), ["not_asked", "wrong", "incomplete", "other"]);
    assert.deepEqual($$(".o_buddy_fb_form label span").map((s) => s.textContent).slice(0, 4),
        ["No era lo que pregunté", "Dato incorrecto o desactualizado", "Incompleta", "Otro"]);
    const box = $(".o_buddy_fb_form input[type=checkbox]");
    assert.equal(box.checked, false, "apagada por defecto");
    assert.match($(".o_buddy_fb_form").textContent, /Incluir mi pregunta para mejorar a Buddy/);
    assert.match($(".o_buddy_fb_form").textContent, /anónimo/);
    assert.match($(".o_buddy_fb_form").textContent, /opcional/);
    assert.equal($$(".o_buddy_fb_form input[type=radio]").some((i) => i.checked), false);
});

test("«Enviar» manda el motivo y la pregunta solo si se tildó, y cierra el formulario", async () => {
    await ask();
    down().click();
    await flush();
    const radio = $$(".o_buddy_fb_form input[type=radio]").find((i) => i.value === "wrong");
    radio.checked = true;
    radio.dispatchEvent(new window.Event("change", { bubbles: true }));
    const box = $(".o_buddy_fb_form input[type=checkbox]");
    box.checked = true;
    box.dispatchEvent(new window.Event("change", { bubbles: true }));
    await flush();
    $(".o_buddy_fb_send").click();
    await flush();
    assert.equal(feedbackCalls().length, 2);
    assert.deepEqual(feedbackCalls()[1].params, { token: TOKEN, vote: "down", reason: "wrong", include_question: true });
    assert.equal($(".o_buddy_fb_form"), null);
    assert.match($(".o_buddy_fb_status").textContent, /Gracias/);
    assert.equal(down().getAttribute("aria-pressed"), "true");
});

test("sin tildar la casilla ni elegir motivo, «Enviar» no manda la pregunta ni el motivo", async () => {
    await ask();
    down().click();
    await flush();
    $(".o_buddy_fb_send").click();
    await flush();
    assert.deepEqual(feedbackCalls()[1].params, { token: TOKEN, vote: "down" });
});

test("«Ahora no» cierra el formulario y el voto queda", async () => {
    await ask();
    down().click();
    await flush();
    $(".o_buddy_fb_skip").click();
    await flush();
    assert.equal($(".o_buddy_fb_form"), null);
    assert.equal(feedbackCalls().length, 1);
    assert.equal(down().getAttribute("aria-pressed"), "true");
    assert.match($(".o_buddy_fb_status").textContent, /Anotamos tu voto/, "el mensaje no se vacía: el voto cuenta");
    assert.equal(document.activeElement, down(), "el foco vuelve al 👎, no se pierde en el body");
});

test("al enviar el formulario el foco también vuelve al 👎", async () => {
    await ask();
    down().click();
    await flush();
    $(".o_buddy_fb_send").click();
    await flush();
    assert.equal($(".o_buddy_fb_form"), null);
    assert.equal(document.activeElement, down());
});

test("Escape cierra el formulario sin cerrar el chat y devuelve el foco al 👎", async () => {
    await ask();
    down().click();
    await flush();
    assert.ok($(".o_buddy_fb_form"));
    $(".o_buddy_fb_form input").dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await flush();
    assert.equal($(".o_buddy_fb_form"), null);
    assert.ok($(".o_buddy_panel"), "el chat sigue abierto");
    assert.equal(document.activeElement, down());
    assert.equal($(".o_buddy_fb_status").textContent, "Anotamos tu voto.");
});

test("al abrirse el formulario el foco va a su primer campo", async () => {
    await ask();
    down().click();
    await flush();
    assert.equal(document.activeElement, $(".o_buddy_fb_form input"));
});

test("si la empresa guarda todas las preguntas no se ofrece la casilla y se dice la verdad", async () => {
    await ask({}, {
        "/buddy/config": () => ({
            first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: true, greeting: "", tagline: "",
            suggestions: [], primary_color: "", configured: true, keep_questions: true,
        }),
    });
    down().click();
    await flush();
    assert.equal($(".o_buddy_fb_form input[type=checkbox]"), null);
    assert.match($(".o_buddy_fb_form").textContent, /Las preguntas ya se guardan sin tu nombre/);
    $(".o_buddy_fb_send").click();
    await flush();
    assert.equal("include_question" in feedbackCalls()[1].params, false);
});

test("un rechazo del servidor muestra su mensaje y deja el voto anterior", async () => {
    await ask({}, { "/buddy/feedback": () => ({ __status: 429, ok: false, error: "rate_limited", message: "Estás votando muy seguido." }) });
    up().click();
    await flush();
    assert.match($(".o_buddy_fb_error").textContent, /votando muy seguido/);
    assert.equal($(".o_buddy_fb_error").getAttribute("role"), "alert");
    assert.equal(up().getAttribute("aria-pressed"), "false");
    assert.equal($(".o_buddy_fb_form"), null);
});

test("una falla de red muestra un mensaje genérico y se puede reintentar", async () => {
    let fail = true;
    await ask({}, {
        "/buddy/feedback": (params) => {
            if (fail) {
                throw new Error("offline");
            }
            return { ok: true, vote: params.vote };
        },
    });
    up().click();
    await flush();
    assert.match($(".o_buddy_fb_error").textContent, /No pude guardar tu opinión/);
    fail = false;
    up().click();
    await flush();
    assert.equal($(".o_buddy_fb_error"), null);
    assert.equal(up().getAttribute("aria-pressed"), "true");
});

test("mientras un voto está en vuelo los botones se marcan ocupados (sin perder el foco) y no se duplica", async () => {
    let release;
    const gate = new Promise((resolve) => (release = resolve));
    await ask({}, {
        "/buddy/feedback": async (params) => {
            await gate;
            return { ok: true, vote: params.vote };
        },
    });
    up().focus();
    up().click();
    await flush();
    assert.equal(up().getAttribute("aria-disabled"), "true");
    assert.equal(up().disabled, false, "un botón con foco que pasa a «disabled» lo pierde: se usa aria-disabled");
    assert.equal(document.activeElement, up());
    up().click();
    down().click();
    await flush();
    release();
    await flush();
    assert.equal(feedbackCalls().length, 1);
    assert.equal(up().getAttribute("aria-disabled"), "false");
});

test("la respuesta del servidor con un voto raro no se acepta", async () => {
    await ask({}, { "/buddy/feedback": () => ({ ok: true, vote: "<img onerror=1>" }) });
    up().click();
    await flush();
    assert.equal(up().getAttribute("aria-pressed"), "false");
    assert.match($(".o_buddy_fb_error").textContent, /No pude guardar/);
});

test("el historial devuelve el voto dado y el código, y se puede cambiar", async () => {
    const stamp = new Date().toISOString().slice(0, 19).replace("T", " ");
    fakeBackend({
        "/buddy/history": () => ({
            session_generation: 3,
            messages: [
                { role: "user", text: "¿Cuánto tarda?", date: stamp },
                { role: "assistant", text: "15 días.", date: stamp, expression: "happy", ring: "ok", sources: [], ticket: null, contact: null,
                  ticket_key: null, feedback_token: TOKEN, feedback: "down", confidence: "high", conflict: false },
                { role: "user", text: "¿Y el sueldo?", date: stamp },
                { role: "assistant", text: "Otra respuesta.", date: stamp, expression: "happy", ring: "ok", sources: [], ticket: null, contact: null,
                  ticket_key: null, feedback_token: null, feedback: "up", confidence: null, conflict: false },
            ],
        }),
    });
    await mountBuddy();
    await openPanel();
    assert.equal($$(".o_buddy_feedback").length, 1, "la respuesta sin código no se vota (aunque diga que tiene voto)");
    assert.equal(down().getAttribute("aria-pressed"), "true", "el voto de antes se ve");
    assert.equal($(".o_buddy_fb_form"), null, "el formulario no reaparece solo");
    up().click();
    await flush();
    assert.equal(feedbackCalls()[0].params.vote, "up");
    assert.equal(feedbackCalls()[0].params.token, TOKEN);
    assert.equal(up().getAttribute("aria-pressed"), "true");
    assert.equal(down().getAttribute("aria-pressed"), "false");
});

test("un voto raro del historial se descarta", async () => {
    const stamp = new Date().toISOString().slice(0, 19).replace("T", " ");
    fakeBackend({
        "/buddy/history": () => ({
            session_generation: 3,
            messages: [
                { role: "user", text: "¿Cuánto tarda?", date: stamp },
                { role: "assistant", text: "15 días.", date: stamp, expression: "happy", ring: "ok", sources: [], ticket: null, contact: null,
                  ticket_key: null, feedback_token: TOKEN, feedback: "<b>", confidence: "alta", conflict: "yes" },
            ],
        }),
    });
    await mountBuddy();
    await openPanel();
    assert.equal(up().getAttribute("aria-pressed"), "false");
    assert.equal(down().getAttribute("aria-pressed"), "false");
    assert.equal($(".o_buddy_confidence"), null);
});

test("«Nueva conversación» saca las respuestas viejas: ya no se puede votar", async () => {
    await ask();
    assert.ok(up());
    $(".o_buddy_new_btn").click();
    await flush();
    $(".o_buddy_confirm_yes").click();
    await flush(120);
    assert.equal($(".o_buddy_feedback"), null);
    assert.equal(feedbackCalls().length, 0);
});

// ---------------------------------------------------------------------------- contradicciones y confianza
test("documentos que se contradicen: nota visible, calma y accesible, y el contacto para confirmar", async () => {
    await ask({
        conflict: true, confidence: "medium", ring: "doubt",
        answer: "Encontré información distinta: el PR 12 dice 15 días y el PR 20 dice 30.",
        contact: { role: "general", label: "Mesa de ayuda", name: "Ana", contact: "ana@example.com" },
    });
    const note = $(".o_buddy_confidence");
    assert.ok(note);
    assert.equal(note.getAttribute("role"), "note");
    assert.match(note.textContent, /no coinciden/);
    assert.match(note.textContent, /persona de contacto/, "con tarjeta de contacto, la nota la nombra");
    assert.equal(note.querySelector("i").getAttribute("aria-hidden"), "true");
    assert.match($(".o_buddy_card_title").textContent, /Para confirmarlo, hablá con/);
    assert.equal($(".o_buddy_msg.is-buddy [role=alert]"), null, "no es una alarma");
});

test("sin contacto cargado la nota no manda a «la persona de contacto»", async () => {
    await ask({ conflict: true, confidence: "medium", ring: "doubt", contact: null });
    assert.match($(".o_buddy_confidence").textContent, /Confirmalo antes de usarlo/);
    assert.doesNotMatch($(".o_buddy_confidence").textContent, /persona de contacto/);
    await ask({ confidence: "low", ring: "doubt", contact: null });
    assert.match($(".o_buddy_confidence").textContent, /Confirmalo antes de usarlo/);
});

test("evidencia floja: «poco respaldo», sin alarma", async () => {
    await ask({ confidence: "low", ring: "doubt", contact: { role: "general", label: "Mesa", name: "Ana", contact: "" } });
    assert.match($(".o_buddy_confidence").textContent, /poco respaldo/);
    assert.match($(".o_buddy_card_title").textContent, /Para confirmarlo/);
});

test("confianza alta o media y respuestas normales no muestran nota", async () => {
    await ask({ confidence: "high" });
    assert.equal($(".o_buddy_confidence"), null);
    await ask({ confidence: "medium" }, {}, "otra");
    assert.equal($(".o_buddy_confidence"), null);
    await ask({ confidence: null, conflict: false }, {}, "otra más");
    assert.equal($(".o_buddy_confidence"), null);
    assert.equal($(".o_buddy_card"), null);
});

test("la nota de confianza no se muestra en un mensaje de error aunque traiga las claves", async () => {
    await ask({ error: "provider_error", conflict: true, confidence: "low", feedback_token: TOKEN });
    assert.equal($(".o_buddy_confidence"), null);
    assert.equal($(".o_buddy_feedback"), null);
});

test("el historial restaura la nota de contradicción", async () => {
    const stamp = new Date().toISOString().slice(0, 19).replace("T", " ");
    fakeBackend({
        "/buddy/history": () => ({
            session_generation: 3,
            messages: [
                { role: "user", text: "¿Plazo?", date: stamp },
                { role: "assistant", text: "Encontré información distinta.", date: stamp, expression: "doubt", ring: "doubt", sources: [],
                  ticket: null, contact: { role: "general", label: "Mesa", name: "Ana", contact: "" }, ticket_key: null,
                  feedback_token: TOKEN, feedback: null, confidence: "medium", conflict: true },
            ],
        }),
    });
    await mountBuddy();
    await openPanel();
    assert.match($(".o_buddy_confidence").textContent, /no coinciden/);
});
