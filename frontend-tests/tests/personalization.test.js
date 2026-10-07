// Personalización, primer uso, markdown seguro, accesibilidad y reintentos del widget.
import { test, before, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountBuddy, flush, calls, destroyApps } from "./helpers.js";

const BUDDY_JS = "../../odoo-addons/buddy_ia/static/src/js/buddy.js";

before(() => setupDom());
afterEach(() => destroyApps());
beforeEach(() => {
    document.body.innerHTML = "";
    sessionStorage.clear();
    localStorage.clear();
});

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const type = async (text) => {
    const input = $(".o_buddy_input");
    input.value = text;
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    await flush();
};
const deferred = () => {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
};
const openPanel = async () => {
    $(".o_buddy_launcher").click();
    await flush();
};
const configWith = (extra) => () => ({
    first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: true, ...extra,
});

// ------------------------------------------------------------------ markdown seguro
test("markdown mínimo: negrita, cursiva, listas y saltos se generan como nodos", async () => {
    fakeBackend({
        "/buddy/chat": () => ({
            answer: "Pasos:\n1. Abrí **Ventas**.\n2. Tocá *Nuevo*.\n\n- uno\n- dos\nlínea A\nlínea B",
            sources: [],
        }),
    });
    await mountBuddy();
    await openPanel();
    $$(".o_buddy_chip")[0].click();
    await flush();
    const text = $$(".o_buddy_text").at(-1);
    assert.equal(text.querySelectorAll("ol > li").length, 2);
    assert.equal(text.querySelector("ol strong").textContent, "Ventas");
    assert.equal(text.querySelector("ol em").textContent, "Nuevo");
    assert.equal(text.querySelectorAll("ul > li").length, 2);
    assert.ok(text.querySelector("p br"), "los saltos de línea simples se conservan");
});

test("markdown: el HTML dentro de negritas, listas y cursivas sigue siendo texto", async () => {
    const evil = '<img src=x onerror="window.PWNED=2">';
    fakeBackend({
        "/buddy/chat": () => ({ answer: `**${evil}**\n- ${evil}\n*<script>window.PWNED=3</script>*`, sources: [] }),
    });
    await mountBuddy();
    await openPanel();
    $$(".o_buddy_chip")[0].click();
    await flush();
    assert.equal($(".o_buddy_messages img"), null);
    assert.equal($(".o_buddy_messages script"), null);
    assert.match($(".o_buddy_messages strong").textContent, /<img src=x/);
    assert.equal(window.PWNED, undefined);
});

test("parseMarkdown: marcadores sin cerrar quedan como texto y las listas numeradas conservan el inicio", async () => {
    const { parseMarkdown } = await import(BUDDY_JS);
    assert.deepEqual(parseMarkdown("2 ** 3 y snake_case_name"), [
        { type: "p", inline: [{ t: "text", v: "2 ** 3 y snake_case_name" }] },
    ]);
    assert.equal(parseMarkdown("3. tres\n4. cuatro")[0].start, 3);
    assert.deepEqual(parseMarkdown("## Licencias\ntexto"), [
        { type: "p", inline: [{ t: "strong", v: "Licencias" }] },
        { type: "p", inline: [{ t: "text", v: "texto" }] },
    ]);
    assert.deepEqual(parseMarkdown("#hashtag sin espacio")[0].inline, [{ t: "text", v: "#hashtag sin espacio" }]);
    assert.deepEqual(parseMarkdown(""), []);
    assert.deepEqual(parseMarkdown(null), []);
});

// ------------------------------------------------------------------ i18n
test("los textos del widget pasan por _t (traducibles)", async () => {
    fakeBackend();
    globalThis.__translations = {
        "Tu guía en Odoo": "Your guide in Odoo",
        "Soy nuevo, ¿por dónde empiezo?": "I'm new, where do I start?",
        "Abrir el chat con %(name)s": "Open the chat with %(name)s",
        "¡Hola, %(name)s!": "Hi, %(name)s!",
    };
    await mountBuddy();
    assert.equal($(".o_buddy_launcher").getAttribute("aria-label"), "Open the chat with Buddy");
    await openPanel();
    assert.equal($(".o_buddy_title span").textContent, "Your guide in Odoo");
    assert.equal($$(".o_buddy_chip")[0].textContent, "I'm new, where do I start?");
    assert.match($(".is-buddy").textContent, /^Hi, Ana!/);
});

// ------------------------------------------------------------------ personalización desde Ajustes
test("sugerencias por defecto: genéricas de onboarding, sin la de cotizaciones", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    const chips = $$(".o_buddy_chip").map((c) => c.textContent);
    assert.equal(chips.length, 3);
    assert.ok(chips.includes("Soy nuevo, ¿por dónde empiezo?"));
    assert.ok(chips.includes("Necesito ayuda de IT"));
    assert.ok(chips.every((c) => !/cotizaci/i.test(c)));
});

test("la configuración del admin personaliza saludo, subtítulo, sugerencias, nombre y color", async () => {
    fakeBackend({
        "/buddy/config": configWith({
            buddy_name: "Lía", greeting: "Buenas, {nombre}. ¿Dudas?", tagline: "Tu compañera de Acme",
            suggestions: ["¿Cómo pido vacaciones?", "  ", "¿Dónde está el wifi?"], primary_color: "#112233",
        }),
    });
    const { comp } = await mountBuddy();
    assert.equal(comp.greetingText, "Buenas, Ana. ¿Dudas?");
    assert.match($(".o_buddy").getAttribute("style"), /--bd-primary: #112233/);
    assert.match($(".o_buddy").getAttribute("style"), /--bd-on-primary: #ffffff/, "texto claro sobre un color oscuro");
    await openPanel();
    assert.equal($(".o_buddy_title span").textContent, "Tu compañera de Acme");
    assert.equal($(".o_buddy_title strong").textContent, "Lía");
    assert.deepEqual($$(".o_buddy_chip").map((c) => c.textContent), ["¿Cómo pido vacaciones?", "¿Dónde está el wifi?"]);
});

test("un color inválido en la configuración se ignora: nunca llega al estilo", async () => {
    fakeBackend({ "/buddy/config": configWith({ primary_color: "red; background:url(javascript:alert(1))" }) });
    await mountBuddy();
    assert.ok(!($(".o_buddy").getAttribute("style") || "").includes("--bd-primary"));
});

test("themeVars calcula texto legible y un acento claro sobre el panel oscuro", async () => {
    const { themeVars } = await import(BUDDY_JS);
    assert.equal(themeVars("nope"), "");
    assert.match(themeVars("#ffff00"), /--bd-on-primary: #04122a/, "amarillo: texto oscuro");
    assert.match(themeVars("#000080"), /--bd-on-primary: #ffffff/, "azul marino: texto blanco");
    const accent = themeVars("#000080").match(/--bd-accent: (#[0-9a-f]{6})/)[1];
    assert.notEqual(accent, "#000080", "el acento se aclara para leerse sobre fondo oscuro");
});

test("renderGreeting completa {nombre} y, sin nombre, lo quita junto con la coma", async () => {
    const { renderGreeting } = await import(BUDDY_JS);
    assert.equal(renderGreeting("¡Hola, {nombre}! ¿Te ayudo?", "Ana"), "¡Hola, Ana! ¿Te ayudo?");
    assert.equal(renderGreeting("¡Hola, {nombre}! ¿Te ayudo?", ""), "¡Hola! ¿Te ayudo?");
    assert.equal(renderGreeting("Hi {name}!", "Bo"), "Hi Bo!");
});

test("si el admin ocultó el buddy a todos no se dibuja nada", async () => {
    fakeBackend({ "/buddy/config": configWith({ enabled: false }) });
    await mountBuddy();
    assert.equal($(".o_buddy"), null);
    await flush(2300);
    assert.equal($(".o_buddy_bubble"), null);
});

// ------------------------------------------------------------------ ocultar
test("Ocultar recuerda la elección en localStorage y deja un botón chico para volver", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    $(".o_buddy_hide").click();
    await flush();
    assert.equal($(".o_buddy_panel"), null);
    assert.equal($(".o_buddy_launcher"), null);
    assert.ok($(".o_buddy_restore"));
    assert.equal(localStorage.getItem("buddy_ia.hidden"), "1");
    // Una carga nueva de la página sigue oculta y no saluda.
    destroyApps();
    document.body.innerHTML = "";
    await mountBuddy();
    assert.ok($(".o_buddy_restore"));
    await flush(2300);
    assert.equal($(".o_buddy_bubble"), null);
    $(".o_buddy_restore").click();
    await flush();
    assert.ok($(".o_buddy_launcher"));
    assert.equal(localStorage.getItem("buddy_ia.hidden"), "0");
    assert.equal(document.activeElement, $(".o_buddy_launcher"), "el foco vuelve al lanzador para quien usa teclado");
});

test("si localStorage está bloqueado el widget igual funciona", async () => {
    fakeBackend();
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
        configurable: true,
        get() { throw new Error("blocked"); },
    });
    try {
        await mountBuddy();
        await openPanel();
        $(".o_buddy_hide").click();
        await flush();
        assert.ok($(".o_buddy_restore"), "se oculta igual en esta página");
    } finally {
        Object.defineProperty(globalThis, "localStorage", descriptor);
    }
});

// ------------------------------------------------------------------ errores y reintento
test("si /buddy/config falla con un motivo, el usuario ve ese mensaje amable (no 'Missing generation')", async () => {
    const answer = "Todavía no estoy configurado. Avisale a tu administrador (Ajustes → Buddy IA).";
    fakeBackend({ "/buddy/config": configWith({ error: "not_configured", answer, session_generation: undefined }) });
    await mountBuddy();
    await openPanel();
    $$(".o_buddy_chip")[0].click();
    await flush();
    assert.equal(calls("/buddy/chat").length, 0);
    assert.equal($$(".is-buddy").at(-1).querySelector(".o_buddy_text").textContent, answer);
    assert.doesNotMatch($(".o_buddy_messages").textContent, /Missing/);
    assert.ok($(".o_buddy_retry_btn"), "puede reintentar cuando el admin termine de configurar");
});

test("Reintentar vuelve a enviar la misma consulta sin duplicarla", async () => {
    let attempt = 0;
    fakeBackend({
        "/buddy/chat": () => {
            if (++attempt === 1) {
                throw new Error("offline");
            }
            return { answer: "Ahora sí", sources: [], ring: "ok" };
        },
    });
    await mountBuddy();
    await openPanel();
    await type("¿Cómo pido vacaciones?");
    $(".o_buddy_send").click();
    await flush();
    assert.match($$(".is-buddy").at(-1).textContent, /Se me cortó la conexión/);
    $(".o_buddy_retry_btn").click();
    await flush();
    const chat = calls("/buddy/chat");
    assert.equal(chat.length, 2);
    assert.equal(chat[1].params.message, "¿Cómo pido vacaciones?");
    assert.notEqual(chat[0].params.request_id, chat[1].params.request_id, "cada intento es una consulta nueva");
    assert.equal($$(".is-user").length, 1, "la pregunta no se duplica");
    assert.match($$(".is-buddy").at(-1).textContent, /Ahora sí/);
    assert.equal($(".o_buddy_retry_btn"), null, "después de responder ya no hay botón");
    assert.doesNotMatch($(".o_buddy_messages").textContent, /Se me cortó/);
});

test("errores recuperables del servidor muestran su mensaje y ofrecen Reintentar; los demás no", async () => {
    let code = "rate_limited";
    fakeBackend({ "/buddy/chat": () => ({ error: code, answer: `Mensaje ${code}`, sources: [] }) });
    await mountBuddy();
    await openPanel();
    $$(".o_buddy_chip")[0].click();
    await flush();
    assert.match($$(".is-buddy").at(-1).textContent, /Mensaje rate_limited/);
    assert.ok($(".o_buddy_retry_btn"));
    code = "invalid_request";
    await type("otra consulta");
    $(".o_buddy_send").click();
    await flush();
    assert.match($$(".is-buddy").at(-1).textContent, /Mensaje invalid_request/);
    assert.equal($(".o_buddy_retry_btn"), null, "reenviar lo mismo no arreglaría una consulta inválida");
});

// ------------------------------------------------------------------ accesibilidad
test("accesibilidad: nombres con el nombre del buddy, aria-controls, 'escribiendo' como status e íconos decorativos", async () => {
    const chat = deferred();
    fakeBackend({ "/buddy/chat": () => chat.promise });
    const { comp } = await mountBuddy();
    assert.equal($(".o_buddy_launcher").getAttribute("aria-label"), "Abrir el chat con Buddy");
    assert.equal($(".o_buddy_launcher").getAttribute("aria-controls"), "o_buddy_panel");
    await openPanel();
    assert.equal($("#o_buddy_panel").getAttribute("role"), "dialog");
    assert.equal($("#o_buddy_panel").getAttribute("aria-label"), "Chat con Buddy");
    assert.equal($(".o_buddy_launcher").getAttribute("aria-label"), "Cerrar el chat con Buddy");
    const input = $(".o_buddy_input");
    assert.equal(input.getAttribute("autocomplete"), "off");
    assert.equal(input.getAttribute("enterkeyhint"), "send");
    comp.send("hola");
    await flush();
    const typing = $(".o_buddy_typing");
    assert.equal(typing.getAttribute("role"), "status");
    assert.ok(typing.getAttribute("aria-label"));
    for (const icon of $$("i.fa")) {
        assert.equal(icon.getAttribute("aria-hidden"), "true", "ícono sin aria-hidden: " + icon.className);
    }
    chat.resolve({ answer: "listo", sources: [] });
    await flush();
});

// ------------------------------------------------------------------ tickets, avatar, borrado
test("el ticket creado se muestra como enlace que abre el formulario dentro de Odoo", async () => {
    fakeBackend({
        "/buddy/chat": () => ({ answer: "Derivo", ticket: { title: "T", description: "D" } }),
        "/buddy/ticket": () => ({ id: 7, name: "TK-0007" }),
    });
    await mountBuddy();
    await openPanel();
    $$(".o_buddy_chip")[0].click();
    await flush();
    $(".o_buddy_btn").click();
    await flush();
    const link = $(".o_buddy_ticket_link");
    assert.equal(link.textContent, "TK-0007");
    assert.equal(link.getAttribute("href"), "/odoo/action-buddy_ia.buddy_ticket_action/7");
    link.click();
    await flush();
    assert.equal(globalThis.__actions.length, 1);
    assert.equal(globalThis.__actions[0].res_model, "buddy.ticket");
    assert.equal(globalThis.__actions[0].res_id, 7);
});

test("el avatar grande (saludando) aparece solo en el globito; el lanzador es el avatar chico", async () => {
    fakeBackend();
    const { comp } = await mountBuddy();
    assert.equal($(".o_buddy_launcher .bd-arm.is-waving"), null, "el lanzador en reposo no saluda");
    assert.equal($(".o_buddy_launcher svg").getAttribute("width"), "79.04");
    comp.state.bubble = "¡Hola!";
    await flush();
    assert.ok($(".o_buddy_bubble .bd-arm.is-waving"));
    assert.equal($(".o_buddy_bubble svg").getAttribute("width"), "48.64");
});

test("cerrar el panel descarta la confirmación de borrado pendiente", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    $(".o_buddy_new_btn").click();
    await flush();
    assert.ok($(".o_buddy_confirm"));
    $(".o_buddy_close").click();
    await flush();
    await openPanel();
    assert.equal($(".o_buddy_confirm"), null);
    assert.equal(calls("/buddy/forget").length, 0);
});
