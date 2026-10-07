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
const type = async (text) => {
    const input = $(".o_buddy_input");
    input.value = text;
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    await flush();
};

test("se registra como main_component de Odoo", async () => {
    fakeBackend();
    await import("../../odoo-addons/buddy_ia/static/src/js/buddy.js");
    const { registry } = await import("@web/core/registry");
    assert.ok(registry.category("main_components").get("buddy_ia.Buddy"));
});

test("arranca cerrado: solo el lanzador con el avatar", async () => {
    fakeBackend();
    await mountBuddy();
    assert.ok($(".o_buddy_launcher svg.o_buddy_avatar"));
    assert.equal($(".o_buddy_panel"), null);
    assert.equal($(".o_buddy_launcher").getAttribute("aria-expanded"), "false");
});

test("saluda con globito a los ~2s usando el nombre del usuario, y no repite en la sesión", async () => {
    fakeBackend();
    await mountBuddy();
    assert.equal($(".o_buddy_bubble"), null);
    await flush(2200);
    assert.match($(".o_buddy_bubble").textContent, /Hola, Ana! ¿Te ayudo con algo\?/);
    $(".o_buddy_bubble_close").click();
    await flush();
    assert.equal($(".o_buddy_bubble"), null);
    document.body.innerHTML = "";
    await mountBuddy();
    await flush(2200);
    assert.equal($(".o_buddy_bubble"), null, "ya saludó en esta sesión");
});

test("al abrir muestra bienvenida personalizada y sugerencias", async () => {
    fakeBackend();
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    assert.ok($(".o_buddy_panel"));
    assert.match($(".is-buddy").textContent, /Hola, Ana! Soy Buddy/);
    assert.equal($$(".o_buddy_chip").length, 3);
});

test("enviar con Enter llama a /buddy/chat, muestra respuesta con fuentes y pone el aro en verde", async () => {
    fakeBackend({
        "/buddy/chat": () => ({
            answer: "1. Abrí Ventas.", expression: "happy", ring: "ok", provider: "gemini",
            sources: [{ title: "Cómo crear una cotización", owner: "Ops", updated: "2026-07-22" }],
            ticket: null, contact: null,
        }),
    });
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    await type("¿Cómo creo una cotización?");
    $(".o_buddy_input").dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await flush();
    const chatParams = calls("/buddy/chat")[0].params;
    assert.equal(chatParams.message, "¿Cómo creo una cotización?");
    assert.equal(chatParams.session_generation, 0);
    assert.match(chatParams.request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    assert.ok($$(".is-user").some((e) => e.textContent.includes("cotización")));
    assert.match($$(".is-buddy").at(-1).textContent, /Abrí Ventas/);
    assert.match($(".o_buddy_source").textContent, /Cómo crear una cotización/);
    assert.ok($(".o_buddy_header svg").classList.contains("ring-ok"));
    assert.equal($(".o_buddy_chips"), null, "las sugerencias desaparecen tras la primera consulta");
    assert.equal($(".o_buddy_input").value, "");
});

test("el botón enviar está deshabilitado con el campo vacío", async () => {
    fakeBackend();
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    assert.equal($(".o_buddy_send").disabled, true);
    await type("hola");
    assert.equal($(".o_buddy_send").disabled, false);
});

test("human in the loop: deriva a una persona y el ticket se crea SOLO al hacer clic", async () => {
    fakeBackend({
        "/buddy/chat": () => ({
            answer: "No tengo esa información.", expression: "doubt", ring: "doubt", provider: "gemini", sources: [],
            ticket: { title: "Consulta: capital", description: "¿capital?" },
            contact: { label: "Mesa de ayuda", name: "Mesa de ayuda interna", contact: "ayuda@ejemplo.test", role: "general" },
        }),
    });
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    $$(".o_buddy_chip")[0].click();
    await flush();
    assert.match($(".o_buddy_link").getAttribute("href"), /^mailto:ayuda@ejemplo\.test$/);
    assert.ok($(".o_buddy_header svg").classList.contains("ring-doubt"));
    assert.equal(calls("/buddy/ticket").length, 0, "no se crea nada sin confirmación");
    $(".o_buddy_btn").click();
    await flush();
    assert.equal(calls("/buddy/ticket").length, 1);
    const ticketParams = calls("/buddy/ticket")[0].params;
    assert.equal(ticketParams.title, "Consulta: capital");
    assert.equal(ticketParams.description, "¿capital?");
    assert.equal(ticketParams.session_generation, 0);
    assert.match(ticketParams.request_key, /^[0-9a-f-]{36}$/i);
    assert.match($(".o_buddy_card_ok").textContent, /Ticket creado: Ticket de prueba/);
    assert.equal($(".o_buddy_btn"), null, "no se puede crear dos veces");
});

test("si falla la creación del ticket avisa y permite reintentar", async () => {
    fakeBackend({
        "/buddy/chat": () => ({ answer: "x", expression: "doubt", ring: "doubt", sources: [], contact: null, provider: "gemini", ticket: { title: "T", description: "d" } }),
        "/buddy/ticket": () => ({ error: "missing_title" }),
    });
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    $$(".o_buddy_chip")[0].click();
    await flush();
    $(".o_buddy_btn").click();
    await flush();
    assert.match($(".o_buddy_card_error").textContent, /No pude crear el ticket/);
    assert.ok($(".o_buddy_btn"), "el botón sigue disponible");
});

test("si cae la conexión muestra un mensaje amable y no se rompe", async () => {
    fakeBackend({ "/buddy/chat": () => { throw new Error("network"); } });
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    $$(".o_buddy_chip")[0].click();
    await flush();
    assert.match($$(".is-buddy").at(-1).textContent, /Se me cortó la conexión/);
    assert.equal($(".o_buddy_typing"), null);
    await type("otra vez");
    assert.equal($(".o_buddy_send").disabled, false, "puede volver a intentar");
});

// Deleting history now asks first: trash button -> inline confirmation -> "Borrar".
const confirmForget = async () => {
    $(".o_buddy_new_btn").click();
    await flush();
    $(".o_buddy_confirm_yes").click();
    await flush();
};

test("borrar historial pide confirmación en línea y recién ahí llama a /buddy/forget", async () => {
    fakeBackend();
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    $$(".o_buddy_chip")[0].click();
    await flush();
    assert.ok($$(".is-user").length >= 1);
    $(".o_buddy_new_btn").click();
    await flush();
    assert.ok($(".o_buddy_confirm"), "aparece la confirmación");
    assert.equal(calls("/buddy/forget").length, 0, "todavía no se borró nada");
    $(".o_buddy_confirm_no").click();
    await flush();
    assert.equal($(".o_buddy_confirm"), null);
    assert.equal(calls("/buddy/forget").length, 0, "cancelar no borra");
    assert.ok($$(".is-user").length >= 1);
    await confirmForget();
    assert.equal($(".o_buddy_confirm"), null);
    assert.equal(calls("/buddy/forget").length, 1);
    assert.equal($$(".is-user").length, 0);
    assert.match($(".is-buddy").textContent, /borré nuestra conversación/);
});

test("Escape cierra el panel", async () => {
    fakeBackend();
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    $(".o_buddy_input").dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flush();
    assert.equal($(".o_buddy_panel"), null);
});

test("cada expresión dibuja ojos distintos y los ids del SVG son únicos por instancia", async () => {
    fakeBackend({ "/buddy/chat": () => ({ answer: "x", expression: "surprised", ring: "ok", sources: [], ticket: null, contact: null, provider: "gemini" }) });
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    const eyes = () => $(".o_buddy_header .bd-eyes").innerHTML;
    const happy = eyes(); // al abrir queda en 'happy' (arcos)
    assert.match(happy, /<path/);
    $$(".o_buddy_chip")[0].click();
    await flush();
    assert.match(eyes(), /ry="9\.2"/, "sorprendido = ojos más grandes");
    const ids = $$("svg [id]").map((e) => e.id);
    assert.equal(new Set(ids).size, ids.length, "ids duplicados entre lanzador y cabecera");
});

test("REGRESIÓN: si se escribe y se envía en el mismo cuadro de render, el campo igual se vacía", async () => {
    // Bug real encontrado en Chromium: estado "texto" -> "" antes de que OWL renderice, así que el DOM no se actualizaba.
    fakeBackend();
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    const input = $(".o_buddy_input");
    input.value = "pegado y enviado enseguida";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })); // sin esperar render
    await flush();
    assert.equal(calls("/buddy/chat").length, 1);
    assert.equal($(".o_buddy_input").value, "", "el texto enviado no debe quedar en el campo");
});

const deferred = () => {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
};

test("REGRESIÓN: un borrado fallido preserva la vista y nunca declara éxito", async () => {
    fakeBackend({ "/buddy/forget": () => { throw new Error("offline"); } });
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    $$(".o_buddy_chip")[0].click();
    await flush();
    const oldText = $(".o_buddy_messages").textContent;
    await confirmForget();
    assert.equal($(".o_buddy_messages").textContent, oldText);
    assert.match($("[role=alert]").textContent, /No pude confirmar el borrado/);
    assert.doesNotMatch($(".o_buddy_messages").textContent, /borré nuestra conversación/);
    assert.equal($(".o_buddy_new_btn").disabled, false);
});

test("REGRESIÓN: un resultado de borrado sin ok:true tampoco declara éxito", async () => {
    fakeBackend({ "/buddy/forget": () => ({ok:false, session_generation:1}) });
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    await confirmForget();
    assert.ok($("[role=alert]"));
    assert.doesNotMatch($(".o_buddy_messages").textContent, /borré nuestra conversación/);
});

test("REGRESIÓN: olvidar descarta respuestas antiguas y bloquea acciones mientras borra", async () => {
    const oldChat = deferred();
    const deleting = deferred();
    let nextChat = false;
    fakeBackend({
        "/buddy/chat": () => nextChat ? {answer:"Respuesta nueva", sources:[]} : oldChat.promise,
        "/buddy/forget": () => deleting.promise,
    });
    const {comp} = await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    const sending = comp.send("Pregunta antigua");
    await flush();
    const forgetting = comp.forget();
    await flush();
    assert.equal($(".o_buddy_new_btn").disabled, true);
    assert.equal($(".o_buddy_input").disabled, true);
    assert.equal($(".o_buddy_panel").getAttribute("aria-busy"), "true");
    await comp.send("No debe enviarse");
    assert.equal(calls("/buddy/chat").length, 1);
    deleting.resolve({ok:true, session_generation:1});
    await forgetting;
    oldChat.resolve({answer:"Respuesta antigua", ticket:{title:"Ticket viejo", description:"x"}});
    await sending;
    await flush();
    assert.doesNotMatch($(".o_buddy_messages").textContent, /Respuesta antigua/);
    assert.equal($(".o_buddy_btn"), null);
    nextChat = true;
    await comp.send("Consulta nueva");
    await flush();
    assert.equal(calls("/buddy/chat")[1].params.session_generation, 1);
    assert.match($(".o_buddy_messages").textContent, /Respuesta nueva/);
});

test("una propuesta eliminada no puede crear tickets desde una referencia antigua", async () => {
    fakeBackend({ "/buddy/chat": () => ({answer:"Derivo", ticket:{title:"T", description:"D"}}) });
    const {comp} = await mountBuddy();
    await comp.send("consulta");
    const oldMessage = comp.state.messages.at(-1);
    await comp.forget();
    await comp.createTicket(oldMessage);
    assert.equal(calls("/buddy/ticket").length, 0);
});

test("un cambio remoto de sesión limpia conversación y no reenvía automáticamente", async () => {
    fakeBackend({
        "/buddy/chat": () => ({error:"session_changed", answer:"Cambió la sesión", session_generation:4,
            ticket:{title:"No usar", description:"x"}, sources:[{title:"No usar"}]}),
    });
    const {comp} = await mountBuddy();
    await comp.send("consulta con permisos anteriores");
    assert.equal(comp.sessionGeneration, 4);
    assert.equal(comp.state.messages.length, 1);
    assert.equal(comp.state.messages[0].text, "Cambió la sesión");
    assert.equal(comp.state.messages[0].ticket, null);
    assert.equal(comp.state.loading, false);
    assert.equal(calls("/buddy/chat").length, 1);
});

test("una configuración fallida no envía chat con generación ausente", async () => {
    fakeBackend({ "/buddy/config": () => { throw new Error("offline"); } });
    const {comp} = await mountBuddy();
    await comp.send("consulta");
    assert.equal(calls("/buddy/chat").length, 0);
    assert.match(comp.state.messages.at(-1).text, /No pude iniciar la conversación/);
    assert.equal(comp.state.loading, false);
});

test("espera configuración inicial antes de enviar y usa la generación recibida", async () => {
    const config = deferred();
    fakeBackend({ "/buddy/config": () => config.promise });
    const {comp} = await mountBuddy();
    const sending = comp.send("consulta temprana");
    await flush();
    assert.equal(calls("/buddy/chat").length, 0);
    config.resolve({first_name:"Ana", buddy_name:"Buddy", session_generation:8});
    await sending;
    assert.equal(calls("/buddy/config").length, 1);
    assert.equal(calls("/buddy/chat")[0].params.session_generation, 8);
});

test("REGRESIÓN: Enter de composición IME no envía y Shift+Enter conserva salto de línea", async () => {
    fakeBackend();
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    await type("texto en composición");
    $(".o_buddy_input").dispatchEvent(new window.KeyboardEvent("keydown", {
        key:"Enter", isComposing:true, bubbles:true, cancelable:true,
    }));
    $(".o_buddy_input").dispatchEvent(new window.KeyboardEvent("keydown", {
        key:"Enter", shiftKey:true, bubbles:true, cancelable:true,
    }));
    await flush();
    assert.equal(calls("/buddy/chat").length, 0);
    assert.equal($(".o_buddy_input").value, "texto en composición");
});

test("Escape desde un botón del panel cierra y devuelve foco al lanzador", async () => {
    fakeBackend();
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    const button = $(".o_buddy_close");
    button.focus();
    button.dispatchEvent(new window.KeyboardEvent("keydown", {key:"Escape", bubbles:true, cancelable:true}));
    await flush();
    assert.equal($(".o_buddy_panel"), null);
    assert.equal(document.activeElement, $(".o_buddy_launcher"));
});

test("REGRESIÓN: al perder respuesta del ticket el reintento conserva request_key", async () => {
    let attempt = 0;
    fakeBackend({
        "/buddy/chat": () => ({answer:"Derivo", ticket:{title:"T", description:"D"}}),
        "/buddy/ticket": () => {
            if (++attempt === 1) { throw new Error("response lost after server commit"); }
            return {id:19, name:"Ticket recuperado"};
        },
    });
    const {comp} = await mountBuddy();
    await comp.send("consulta");
    const proposal = comp.state.messages.at(-1);
    await comp.createTicket(proposal);
    assert.equal(proposal.ticketResult.error, true);
    await comp.createTicket(proposal);
    assert.equal(proposal.ticketResult.id, 19);
    const ticketCalls = calls("/buddy/ticket");
    assert.equal(ticketCalls.length, 2);
    assert.equal(ticketCalls[0].params.request_key, ticketCalls[1].params.request_key);
    assert.equal(ticketCalls[1].params.session_generation, 0);
});

test("el contenido potencialmente HTML del modelo se muestra como texto", async () => {
    const html = '<img src=x onerror="window.PWNED=true">';
    fakeBackend({ "/buddy/chat": () => ({answer:html, sources:[{title:html, owner:html}]}) });
    await mountBuddy();
    $(".o_buddy_launcher").click();
    await flush();
    $$(".o_buddy_chip")[0].click();
    await flush();
    assert.match($(".o_buddy_messages").textContent, /<img src=x/);
    assert.equal($(".o_buddy_messages img"), null);
});

test("una respuesta antigua no apaga loading de la consulta nueva después de borrar", async () => {
    const oldChat = deferred(), newChat = deferred();
    let count = 0;
    fakeBackend({ "/buddy/chat": () => ++count === 1 ? oldChat.promise : newChat.promise });
    const {comp} = await mountBuddy();
    const first = comp.send("antes de borrar");
    await flush();
    await comp.forget();
    const second = comp.send("después de borrar");
    await flush();
    oldChat.resolve({answer:"Texto obsoleto", ring:"ok", expression:"happy"});
    await first;
    assert.equal(comp.state.loading, true);
    assert.equal(comp.state.ring, "thinking");
    assert.ok(comp.state.messages.every(m => m.text !== "Texto obsoleto"));
    newChat.resolve({answer:"Texto vigente", ring:"ok"});
    await second;
    assert.equal(comp.state.loading, false);
    assert.equal(comp.state.messages.at(-1).text, "Texto vigente");
});

test("un error tardío de chat borrado no aparece en la conversación nueva", async () => {
    const oldChat = deferred();
    fakeBackend({ "/buddy/chat": () => oldChat.promise });
    const {comp} = await mountBuddy();
    const sending = comp.send("antes de borrar");
    await flush();
    await comp.forget();
    oldChat.reject(new Error("connection lost"));
    await sending;
    assert.equal(comp.state.messages.length, 1);
    assert.match(comp.state.messages[0].text, /borré nuestra conversación/);
    assert.equal(comp.state.loading, false);
});

test("genera UUID válidos sin randomUUID y no reutiliza la clave entre propuestas", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    const nativeCrypto = globalThis.crypto;
    Object.defineProperty(globalThis, "crypto", {
        configurable:true, value:{getRandomValues:nativeCrypto.getRandomValues.bind(nativeCrypto)},
    });
    try {
        fakeBackend({ "/buddy/chat": () => ({answer:"Derivo", ticket:{title:"T", description:"D"}}) });
        const {comp} = await mountBuddy();
        await comp.send("primera consulta");
        await comp.createTicket(comp.state.messages.at(-1));
        await comp.send("segunda consulta");
        await comp.createTicket(comp.state.messages.at(-1));
        const uuids = [...calls("/buddy/chat").map(c => c.params.request_id),
            ...calls("/buddy/ticket").map(c => c.params.request_key)];
        for (const uuid of uuids) {
            assert.match(uuid, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
        }
        // The ticket key is the chat request id of the answer that proposed it (so a reload cannot duplicate it).
        assert.equal(new Set(uuids).size, 2);
        assert.deepEqual(calls("/buddy/ticket").map(c => c.params.request_key), calls("/buddy/chat").map(c => c.params.request_id));
    } finally {
        Object.defineProperty(globalThis, "crypto", descriptor);
    }
});
