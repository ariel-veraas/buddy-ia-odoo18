import { test, before, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountBuddy, flush, calls, destroyApps, loadSyncTemplates } from "./helpers.js";

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
const type = async (text) => {
    const input = $(".o_buddy_input");
    input.value = text;
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    await flush();
};
const stamp = (date) => date.toISOString().slice(0, 19).replace("T", " ");

// ---------------------------------------------------------------------------------------------- focus
test("después de enviar con el botón, el cursor vuelve al cuadro de texto", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    await type("¿Cómo pido vacaciones?");
    $(".o_buddy_send").focus();
    assert.equal(document.activeElement, $(".o_buddy_send"));
    $(".o_buddy_send").click();
    await flush();
    assert.equal(document.activeElement, $(".o_buddy_input"), "se puede seguir escribiendo sin tocar el cuadro");
    assert.equal($(".o_buddy_input").value, "");
});

test("después de tocar una sugerencia el foco queda en el cuadro, también cuando llega la respuesta", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    const chip = $$(".o_buddy_chip")[0];
    chip.focus();
    chip.click();
    await flush();
    assert.equal($$(".is-user").length, 1);
    assert.equal(document.activeElement, $(".o_buddy_input"));
});

test("la respuesta no le roba el foco a quien se fue a otro lado a propósito", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    const other = document.createElement("input");
    document.body.appendChild(other);
    await type("hola");
    $(".o_buddy_send").click();
    other.focus(); // la persona hizo clic en otro campo de Odoo mientras esperaba
    await flush();
    assert.equal(document.activeElement, other);
});

// -------------------------------------------------------------------------- ticket remembered after reload
const withTicket = (created) => ({
    messages: [
        { role: "user", text: "¿Y el sueldo?", date: stamp(new Date()) },
        {
            role: "assistant", text: "Eso no está documentado.", date: stamp(new Date()), expression: "doubt", ring: "doubt",
            sources: [], ticket: { title: "Consulta: sueldo", description: "¿Y el sueldo?" },
            contact: null, ticket_key: "5d6c5b1e-3f0a-4c55-9a52-0f6a9a3f1b11", ticket_created: created,
        },
    ],
    session_generation: 4,
});

test("la tarjeta de un ticket ya creado dice «Ticket creado» después de recargar, con su enlace, sin ofrecer crearlo de nuevo", async () => {
    fakeBackend({ "/buddy/history": () => withTicket({ id: 12, name: "Consulta: sueldo" }) });
    await mountBuddy();
    await openPanel();
    assert.match($(".o_buddy_card_ok").textContent, /Ticket creado/);
    const link = $(".o_buddy_ticket_link");
    assert.equal(link.textContent, "Consulta: sueldo");
    assert.match(link.getAttribute("href"), /buddy_ticket_action\/12$/);
    assert.equal($(".o_buddy_btn"), null, "no se ofrece crear otro");
    link.click();
    assert.equal(globalThis.__actions.at(-1).res_id, 12, "el enlace abre el ticket dentro de Odoo");
    assert.equal(calls("/buddy/ticket").length, 0);
});

test("si el servidor no dice que el ticket existe, o manda algo inválido, el botón sigue ahí", async () => {
    for (const created of [null, undefined, {}, { id: "12", name: "x" }, { id: 0, name: "x" }, { id: 3, name: 5 }, "12"]) {
        document.body.innerHTML = "";
        destroyApps();
        fakeBackend({ "/buddy/history": () => withTicket(created) });
        await mountBuddy();
        await openPanel();
        assert.ok($(".o_buddy_btn"), `botón con ${JSON.stringify(created)}`);
        assert.equal($(".o_buddy_card_ok"), null);
    }
});

// ------------------------------------------------------------------------------------------------- limits
test("el tope diario y el de la empresa muestran su mensaje y NO ofrecen Reintentar; el de por minuto sí", async () => {
    const messages = {
        daily_limit: "Llegaste al máximo de consultas de hoy. Mañana podés seguir.",
        system_limit: "Buddy llegó hoy a su tope de consultas para toda la empresa.",
        rate_limited: "Estás consultando muy seguido. Esperá un momento y probá de nuevo.",
    };
    for (const [code, answer] of Object.entries(messages)) {
        document.body.innerHTML = "";
        destroyApps();
        fakeBackend({ "/buddy/chat": () => ({ error: code, answer, sources: [], ticket: null, contact: null }) });
        await mountBuddy();
        await openPanel();
        await type("una consulta");
        $(".o_buddy_send").click();
        await flush();
        assert.equal($$(".is-buddy").at(-1).textContent.includes(answer), true, code);
        assert.equal(Boolean($(".o_buddy_retry_btn")), code === "rate_limited", `reintentar con ${code}`);
    }
});

// -------------------------------------------------------------------------------------------- not configured
const unconfigured = (extra = {}) => ({
    "/buddy/config": () => ({
        first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: true, greeting: "", tagline: "",
        suggestions: [], primary_color: "", configured: false, ...extra,
    }),
});

test("sin configurar, un empleado ve un solo aviso sin pasos de administrador, no ofrece sugerencias ni deja escribir", async () => {
    fakeBackend(unconfigured({ is_admin: false }));
    const { comp } = await mountBuddy();
    await openPanel();
    assert.equal($$(".o_buddy_unconfigured").length, 1, "un solo lugar para el mensaje");
    assert.equal($(".o_buddy_unconfigured").textContent.trim(), "Todavía no estoy listo. Avisale a quien administra Odoo.");
    assert.doesNotMatch($(".o_buddy_panel").textContent, /Ajustes/);
    assert.equal($$(".is-buddy").length, 0, "el aviso no se repite como mensaje del chat, ni hay saludo de bienvenida");
    assert.equal($(".o_buddy_finish_setup"), null, "un empleado no puede terminar la configuración");
    assert.equal($(".o_buddy_input").disabled, true);
    assert.equal($(".o_buddy_send").disabled, true);
    assert.ok($$(".o_buddy_chip").every((chip) => chip.disabled));
    $$(".o_buddy_chip")[0].click();
    await comp.send("hola igual");
    await flush();
    assert.equal(calls("/buddy/chat").length, 0);
    assert.equal(globalThis.__fetchCalls.length, 0);
    assert.equal($$(".is-user").length, 0);
});

test("sin configurar, el administrador ve el botón «Terminar la configuración», que abre la pantalla de Buddy", async () => {
    fakeBackend(unconfigured({ is_admin: true }));
    await mountBuddy();
    await openPanel();
    assert.equal($$(".o_buddy_unconfigured").length, 1);
    assert.match($(".o_buddy_unconfigured").textContent, /falta terminar la configuración/);
    assert.doesNotMatch($(".o_buddy_unconfigured").textContent, /quien administra Odoo/);
    const button = $(".o_buddy_finish_setup");
    assert.equal(button.textContent.trim(), "Terminar la configuración");
    button.click();
    await flush();
    assert.equal(globalThis.__actions.at(-1), "buddy_ia.buddy_settings_action");
});

test("un servidor que no informa «configured» se trata como configurado (compatibilidad)", async () => {
    fakeBackend({
        "/buddy/config": () => ({ first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: true }),
    });
    await mountBuddy();
    await openPanel();
    assert.equal($(".o_buddy_unconfigured"), null);
    assert.equal($(".o_buddy_input").disabled, false);
    assert.match($(".is-buddy").textContent, /Soy Buddy/);
});

test("configurado, no aparece ningún aviso", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    assert.equal($(".o_buddy_unconfigured"), null);
});

// ------------------------------------------------------------------------- folder form: auto-refresh widget
test("shouldRefresh: solo mientras sincroniza y sin cambios sin guardar", async () => {
    const { shouldRefresh, REFRESH_MS } = await import("../../odoo-addons/buddy_ia/static/src/js/buddy_sync_refresh.js");
    assert.equal(REFRESH_MS, 5000);
    for (const state of ["queued", "syncing"]) {
        assert.equal(shouldRefresh(state, false), true, state);
        assert.equal(shouldRefresh(state, true), false, `${state} con cambios sin guardar: recargar los perdería`);
    }
    for (const state of ["ok", "error", "never", undefined, ""]) {
        assert.equal(shouldRefresh(state, false), false, String(state));
    }
});

async function mountSyncField(record) {
    const { App } = await import("@odoo/owl");
    const { BuddySyncRefresh } = await import("../../odoo-addons/buddy_ia/static/src/js/buddy_sync_refresh.js");
    const target = document.createElement("div");
    document.body.appendChild(target);
    const app = new App(BuddySyncRefresh, { templates: loadSyncTemplates(), test: true, props: { name: "state", record } });
    await app.mount(target);
    return { app, target };
}

test("el widget se registra como campo y avisa que la pantalla se actualiza sola", async () => {
    await import("../../odoo-addons/buddy_ia/static/src/js/buddy_sync_refresh.js");
    const { registry } = await import("@web/core/registry");
    const entry = registry.category("fields").get("buddy_sync_refresh");
    assert.ok(entry.component);
    assert.deepEqual(entry.supportedTypes, ["selection"]);
    const record = { data: { state: "syncing" }, isDirty: async () => false, load: async () => {} };
    const { app, target } = await mountSyncField(record);
    assert.match(target.textContent, /se actualiza sola/);
    app.destroy();
});

/** Captures the 5 s interval of the widget (and its cancellation) so a test can fire it by hand: no real waiting. */
function spyOnIntervals() {
    const real = { set: globalThis.setInterval, clear: globalThis.clearInterval };
    const taken = [];
    globalThis.setInterval = (fn, ms, ...rest) => {
        if (ms !== 5000) {
            return real.set(fn, ms, ...rest);
        }
        const token = { fn, ms, cleared: false };
        taken.push(token);
        return token;
    };
    globalThis.clearInterval = (token) => {
        if (token && typeof token === "object" && "cleared" in token) {
            token.cleared = true;
        } else {
            real.clear(token);
        }
    };
    return { taken, restore: () => Object.assign(globalThis, { setInterval: real.set, clearInterval: real.clear }) };
}

test("mientras sincroniza recarga el registro cada 5 segundos, y deja de hacerlo al terminar o al cerrar la pantalla", async () => {
    const spy = spyOnIntervals();
    try {
        let loads = 0;
        const record = {
            data: { state: "syncing" }, dirty: false,
            isDirty: async () => record.dirty,
            load: async () => { loads++; },
        };
        const { app } = await mountSyncField(record);
        assert.equal(spy.taken.length, 1, "un solo temporizador, de 5 segundos");
        const [timer] = spy.taken;
        await timer.fn();
        assert.equal(loads, 1);
        record.dirty = true; // la persona está editando el nombre de la carpeta: no se le borra lo escrito
        await timer.fn();
        assert.equal(loads, 1);
        record.dirty = false;
        await timer.fn();
        assert.equal(loads, 2);
        record.data.state = "ok"; // terminó: ya no hay nada que mirar
        await timer.fn();
        assert.equal(loads, 2);
        assert.equal(timer.cleared, false);
        app.destroy();
        assert.equal(timer.cleared, true, "cerrada la pantalla, el temporizador se detiene");
    } finally {
        spy.restore();
    }
});

test("una recarga lenta no se pisa con la siguiente, y si falla se vuelve a intentar en el próximo ciclo", async () => {
    const spy = spyOnIntervals();
    try {
        let attempts = 0;
        let release;
        const record = {
            data: { state: "queued" }, isDirty: async () => false,
            load: () => {
                attempts++;
                if (attempts === 1) {
                    return new Promise((resolve) => (release = resolve)); // el servidor está ocupado
                }
                return attempts === 2 ? Promise.reject(new Error("busy")) : Promise.resolve();
            },
        };
        const { app } = await mountSyncField(record);
        const [timer] = spy.taken;
        const first = timer.fn();
        await flush(5);
        await timer.fn(); // todavía hay una recarga en curso: no se lanza otra
        assert.equal(attempts, 1);
        release();
        await first;
        await timer.fn(); // la segunda falla...
        await timer.fn(); // ...y la tercera sale
        assert.equal(attempts, 3);
        app.destroy();
    } finally {
        spy.restore();
    }
});

// ------------------------------------------------------------------------------------------------ review fixes
test("la bienvenida dice que responde con la documentación de la empresa configurada", async () => {
    fakeBackend({
        "/buddy/config": () => ({ first_name: "Ana", buddy_name: "Lía", session_generation: 0, enabled: true, company: "Acme SA", configured: true }),
    });
    await mountBuddy();
    await openPanel();
    assert.match($(".is-buddy").textContent, /Soy Lía\. Respondo con la documentación de Acme SA/);
});

test("sin nombre de empresa la bienvenida dice «la empresa»", async () => {
    fakeBackend();
    await mountBuddy();
    await openPanel();
    assert.match($(".is-buddy").textContent, /Respondo con la documentación de la empresa/);
});

test("el globito de saludo sale una vez por día y nunca más si la persona ya abrió el chat", async () => {
    fakeBackend();
    await mountBuddy();
    await flush(2200);
    assert.ok($(".o_buddy_bubble"), "primera vez");
    $(".o_buddy_bubble_close").click();
    await flush();
    sessionStorage.clear(); // otra pestaña: la sesión es nueva, el navegador es el mismo
    document.body.innerHTML = "";
    await mountBuddy();
    await flush(2200);
    assert.equal($(".o_buddy_bubble"), null, "ya saludó hoy en este navegador, aunque sea otra pestaña");
    localStorage.setItem("buddy_ia.greeted", "2020-01-01"); // otro día
    document.body.innerHTML = "";
    await mountBuddy();
    await flush(2200);
    assert.ok($(".o_buddy_bubble"), "al día siguiente vuelve a saludar (si nunca abrió el chat)");
    $(".o_buddy_launcher").click();
    await flush();
    localStorage.setItem("buddy_ia.greeted", "2020-01-01");
    document.body.innerHTML = "";
    await mountBuddy();
    await flush(2200);
    assert.equal($(".o_buddy_bubble"), null, "quien abrió el chat una vez no vuelve a ver el globito");
});

test("«Ticket creado»: ctrl, cmd o botón del medio dejan al navegador abrirlo en otra pestaña; el clic normal abre un diálogo", async () => {
    const created = { id: 12, name: "Consulta: sueldo" };
    fakeBackend({ "/buddy/history": () => withTicket(created) });
    await mountBuddy();
    await openPanel();
    const link = $(".o_buddy_ticket_link");
    for (const init of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { button: 1 }]) {
        const event = new window.MouseEvent("click", { bubbles: true, cancelable: true, ...init });
        link.dispatchEvent(event);
        assert.equal(event.defaultPrevented, false, JSON.stringify(init));
    }
    assert.equal(globalThis.__actions.length, 0, "ninguna de esas abrió el diálogo");
    const plain = new window.MouseEvent("click", { bubbles: true, cancelable: true });
    link.dispatchEvent(plain);
    assert.equal(plain.defaultPrevented, true);
    assert.equal(globalThis.__actions.at(-1).target, "new", "en la computadora: un diálogo, el chat queda donde estaba");
    assert.equal($(".o_buddy_panel") !== null, true);
});

test("en el celular el ticket ocupa la pantalla y el panel se cierra", async () => {
    window.matchMedia = () => ({ matches: true });
    try {
        fakeBackend({ "/buddy/history": () => withTicket({ id: 12, name: "Consulta: sueldo" }) });
        await mountBuddy();
        await openPanel();
        $(".o_buddy_ticket_link").click();
        await flush();
        assert.equal(globalThis.__actions.at(-1).target, "current");
        assert.equal($(".o_buddy_panel"), null);
    } finally {
        delete window.matchMedia;
    }
});

test("la tarjeta de ticket muestra el detalle en un desplegable, solo si dice algo más que el asunto", async () => {
    fakeBackend({ "/buddy/history": () => withTicket(null) });
    await mountBuddy();
    await openPanel();
    assert.match($(".o_buddy_ticket_details summary").textContent, /Ver el detalle/);
    assert.match($(".o_buddy_ticket_details").textContent, /¿Y el sueldo\?/);
    const same = withTicket(null);
    same.messages[1].ticket = { title: "Igual", description: "Igual" };
    document.body.innerHTML = "";
    destroyApps();
    fakeBackend({ "/buddy/history": () => same });
    await mountBuddy();
    await openPanel();
    assert.equal($(".o_buddy_ticket_details"), null);
});

test("si el servidor rechaza el ticket (tope por día) se muestra el motivo, no un error genérico", async () => {
    fakeBackend({
        "/buddy/history": () => withTicket(null),
        "/buddy/ticket": () => ({ error: "ticket_rejected", message: "Llegaste al máximo de tickets por día." }),
    });
    await mountBuddy();
    await openPanel();
    $(".o_buddy_btn").click();
    await flush();
    assert.match($(".o_buddy_card_error").textContent, /Llegaste al máximo de tickets por día/);
    assert.doesNotMatch($(".o_buddy_card_error").textContent, /Probá de nuevo/);
    fakeBackend({ "/buddy/history": () => withTicket(null), "/buddy/ticket": () => { throw new Error("red"); } });
    document.body.innerHTML = "";
    destroyApps();
    await mountBuddy();
    await openPanel();
    $(".o_buddy_btn").click();
    await flush();
    assert.match($(".o_buddy_card_error").textContent, /No pude crear el ticket/);
});

test("la tarjeta de contacto con correo ofrece el enlace y «Copiar correo»; sin correo no ofrece ninguno", async () => {
    const withContact = (email) => ({
        messages: [
            { role: "user", text: "¿Quién ve esto?", date: stamp(new Date()) },
            { role: "assistant", text: "No está documentado.", date: stamp(new Date()), expression: "doubt", ring: "doubt",
              sources: [], ticket: null, contact: { role: "it", label: "Mesa de IT", name: "Equipo de IT", contact: email }, ticket_key: null },
        ],
        session_generation: 1,
    });
    const written = [];
    Object.defineProperty(globalThis.navigator ?? window.navigator, "clipboard", {
        configurable: true, value: { writeText: async (text) => written.push(text) },
    });
    fakeBackend({ "/buddy/history": () => withContact("it@miempresa.com") });
    await mountBuddy();
    await openPanel();
    assert.equal($(".o_buddy_card .o_buddy_link").getAttribute("href"), "mailto:it@miempresa.com");
    $(".o_buddy_copy").click();
    await flush();
    assert.deepEqual(written, ["it@miempresa.com"]);
    assert.match($(".o_buddy_copy").textContent, /Copiado/);
    document.body.innerHTML = "";
    destroyApps();
    fakeBackend({ "/buddy/history": () => withContact("") });
    await mountBuddy();
    await openPanel();
    assert.match($(".o_buddy_card").textContent, /Mesa de IT/);
    assert.equal($(".o_buddy_card .o_buddy_link"), null, "sin correo real no hay enlace de mailto");
    assert.equal($(".o_buddy_copy"), null);
});

test("mientras responde hay un botón «Detener» que corta la respuesta y deja lo escrito con «Reintentar»", async () => {
    const { sseResponse } = await import("./helpers.js");
    const stream = sseResponse();
    fakeBackend();
    globalThis.fetch = (url, options) => {
        options.signal.addEventListener("abort", () => {
            try {
                stream.fail(new DOMException("aborted", "AbortError"));
            } catch {
                // ya cerrado
            }
        });
        return Promise.resolve(stream.response);
    };
    await mountBuddy();
    await openPanel();
    await type("una consulta larga");
    $(".o_buddy_send").click();
    await flush();
    assert.equal($(".o_buddy_send"), null);
    const stop = $(".o_buddy_stop");
    assert.ok(stop, "hay botón para detener");
    assert.equal(stop.getAttribute("aria-label"), "Detener la respuesta");
    stream.push(": open\n\n");
    stream.push(`event: delta\ndata: ${JSON.stringify({ text: "Empiezo a contestar" })}\n\n`);
    await flush();
    stop.click();
    await flush();
    assert.match($$(".is-buddy").at(-1).textContent, /Empiezo a contestar/, "lo escrito hasta ahí se conserva");
    assert.ok($(".o_buddy_retry_btn"), "y se puede volver a preguntar");
    assert.equal($(".o_buddy_stop"), null);
    assert.ok($(".o_buddy_send"));
    assert.doesNotMatch($(".o_buddy_messages").textContent, /Se me cortó/, "detener no es un error");
});

test("cada mensaje dice quién habla para un lector de pantalla, sin cambiar el texto visible", async () => {
    fakeBackend({
        "/buddy/config": () => ({ first_name: "Ana", buddy_name: "Lía", session_generation: 0, enabled: true, configured: true }),
        "/buddy/history": () => withTicket(null),
    });
    await mountBuddy();
    await openPanel();
    assert.equal($(".is-user").getAttribute("aria-label"), "Vos escribiste");
    assert.equal($(".is-buddy").getAttribute("aria-label"), "Lía responde");
    assert.equal($(".is-user").textContent, "¿Y el sueldo?");
});

test("ya no queda código del «modo demo» sin IA", async () => {
    const { readFileSync } = await import("node:fs");
    const source = readFileSync(new URL("../../odoo-addons/buddy_ia/static/src/js/buddy.js", import.meta.url), "utf-8");
    const template = readFileSync(new URL("../../odoo-addons/buddy_ia/static/src/xml/buddy.xml", import.meta.url), "utf-8");
    assert.doesNotMatch(source + template, /offlineMode|Modo demo/);
});
