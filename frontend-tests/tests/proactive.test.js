// Proactividad suave (E4b): sugerencias a medida en los chips y el aviso discreto de las tareas del día con los gestos en reposo.
// Las pruebas del aviso usan relojes falsos (setTimeout y Date), como las de los gestos.
import { test, before, beforeEach, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountComponent, destroyApps, calls } from "./helpers.js";

const ADDON = "../../odoo-addons/buddy_ia/static/src/js/";

before(() => setupDom());
beforeEach(() => {
    document.body.innerHTML = "";
    sessionStorage.clear();
    localStorage.clear();
    delete globalThis.odoo;
    mock.timers.enable({ apis: ["setTimeout", "Date"], now: new Date(2026, 9, 6, 14, 0, 0) });
});
afterEach(() => {
    destroyApps();
    mock.timers.reset();
    delete window.matchMedia;
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
});

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const S = 1000;
const frames = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
const tick = async (ms) => {
    mock.timers.tick(ms);
    await Promise.resolve();
};
const chipTexts = () => $$(".o_buddy_chip").map((chip) => chip.textContent);
// Advance the clock in steps until the reminder shows up (it goes away by itself after 15 s, so a single look at the end could miss it).
async function seenWithin(total, step = 5 * 1000) {
    for (let waited = 0; waited < total; waited += step) {
        await tick(step);
        await frames();
        if (document.querySelector(".o_buddy_nudge")) {
            return true;
        }
    }
    return false;
}

// ------------------------------------------------------------------------------------------ the pure part
test("normalizeQuestion: mayúsculas, tildes, signos y espacios no distinguen una pregunta de otra", async () => {
    const { normalizeQuestion } = await import(ADDON + "buddy_suggestions.js");
    assert.equal(normalizeQuestion("¿Cómo  pido VACACIONES?"), "como pido vacaciones");
    assert.equal(normalizeQuestion("como pido vacaciones"), "como pido vacaciones");
    assert.equal(normalizeQuestion(null), "");
    assert.equal(normalizeQuestion("¿Qué dice «PR 38»?"), "que dice pr 38");
});

test("cleanSuggestions: solo tipos conocidos, textos acotados, sin repetidos y un id solo si es un entero positivo", async () => {
    const { cleanSuggestions } = await import(ADDON + "buddy_suggestions.js");
    assert.deepEqual(cleanSuggestions(null), []);
    assert.deepEqual(cleanSuggestions({ suggestions: "x" }), []);
    const clean = cleanSuggestions({ suggestions: [
        { kind: "step", text: "¿Cómo pido un día libre?", label: "¿Cómo pido un día libre?", ask_id: 41 },
        { kind: "hack", text: "no" }, { kind: "topic" }, { kind: "topic", text: "" }, { kind: "topic", text: 5 }, null, "x", 7,
        { kind: "topic", text: "¿COMO pido un dia libre?" },
        { kind: "next", text: "A".repeat(500), ask_id: "7" },
        { kind: "topic", text: "Con\u0000control\nde línea", ask_id: true },
        { kind: "step", text: "Otra", ask_id: 2 ** 40 },
    ] });
    assert.deepEqual(clean.map((chip) => chip.kind), ["step", "next", "topic", "step"]);
    assert.equal(clean[0].askId, 41);
    assert.equal(clean[1].text.length, 200);
    assert.ok(clean[1].label.length <= 80 && clean[1].label.endsWith("…"));
    assert.equal(clean[2].text, "Con control de línea");
    for (const chip of [clean[1], clean[2], clean[3]]) {
        assert.equal("askId" in chip, false, chip.text.slice(0, 10));
    }
});

test("cleanSuggestions: los caracteres invisibles (marcas de dirección, espacios de ancho cero) se quitan: el botón dice lo que se manda", async () => {
    const { cleanSuggestions } = await import(ADDON + "buddy_suggestions.js");
    const [chip] = cleanSuggestions({ suggestions: [{ kind: "topic", text: "¿Qué dice\u202e «uno\u200bdos»\ufeff?\u0085", label: "x\u2066y" }] });
    assert.equal(chip.text, "¿Qué dice «unodos»?");
    assert.equal(chip.label, "xy");
});

test("cleanSuggestions: como mucho ocho", async () => {
    const { cleanSuggestions } = await import(ADDON + "buddy_suggestions.js");
    const many = Array.from({ length: 30 }, (_, index) => ({ kind: "topic", text: "tema " + index }));
    assert.equal(cleanSuggestions({ suggestions: many }).length, 8);
});

const chip = (kind, text, extra = {}) => ({ kind, text, label: text, ...extra });
const POOL = [chip("step", "Paso uno"), chip("step", "Paso dos"), chip("step", "Paso tres"), chip("topic", "Tema A"), chip("topic", "Tema B"), chip("topic", "Tema C"), chip("next", "Luego")];
const GENERIC = ["Soy nuevo", "¿A quién le pregunto?", "Necesito IT"];

test("pickChips: lo urgente primero (dos pasos como mucho), tres a medida y completa con los fijos hasta cuatro", async () => {
    const { pickChips } = await import(ADDON + "buddy_suggestions.js");
    const shown = pickChips({ pool: POOL, generic: GENERIC, rotation: 0 });
    assert.equal(shown.length, 4);
    assert.deepEqual(shown.slice(0, 2).map((c) => c.text), ["Paso uno", "Paso dos"]);
    assert.equal(shown.filter((c) => c.kind !== "generic").length, 3);
    assert.deepEqual(shown.at(-1), { kind: "generic", text: "Soy nuevo", label: "Soy nuevo" });
});

test("pickChips: las no urgentes rotan con cada vuelta y todas tienen su turno", async () => {
    const { pickChips } = await import(ADDON + "buddy_suggestions.js");
    const seen = new Set();
    const turns = [];
    for (let rotation = 0; rotation < 6; rotation++) {
        const shown = pickChips({ pool: POOL, generic: GENERIC, rotation });
        turns.push(shown.filter((c) => c.kind !== "generic").map((c) => c.text).join("|"));
        shown.forEach((c) => seen.add(c.text));
        assert.deepEqual(shown.slice(0, 2).map((c) => c.text), ["Paso uno", "Paso dos"], "lo urgente no rota");
    }
    assert.ok(new Set(turns).size > 1, "cambia de una vuelta a otra");
    for (const text of ["Paso tres", "Tema A", "Tema B", "Tema C", "Luego"]) {
        assert.ok(seen.has(text), text + " alguna vez se muestra");
    }
});

test("pickChips: nunca repite lo que la persona ya preguntó en la conversación (tildes y signos aparte)", async () => {
    const { pickChips } = await import(ADDON + "buddy_suggestions.js");
    const shown = pickChips({ pool: POOL, generic: GENERIC, asked: ["¿paso UNO?", "tema a", "soy nuevo"], rotation: 0 });
    const texts = shown.map((c) => c.text);
    for (const text of ["Paso uno", "Tema A", "Soy nuevo"]) {
        assert.ok(!texts.includes(text), text);
    }
    assert.ok(texts.includes("Paso dos"));
    assert.ok(shown.length <= 4);
});

test("pickChips: sin nada del servidor son los fijos de siempre; sin repetir un fijo que ya es una sugerencia a medida", async () => {
    const { pickChips } = await import(ADDON + "buddy_suggestions.js");
    assert.deepEqual(pickChips({ pool: [], generic: GENERIC }).map((c) => c.text), GENERIC);
    const shown = pickChips({ pool: [chip("topic", "Necesito IT")], generic: GENERIC });
    assert.deepEqual(shown.map((c) => c.text), ["Necesito IT", "Soy nuevo", "¿A quién le pregunto?"]);
    assert.deepEqual(pickChips({ pool: [], generic: [] }), []);
});

test("pickChips: nunca más de cuatro, aunque haya mucho para mostrar, y una rotación rara no rompe nada", async () => {
    const { pickChips, MAX_CHIPS } = await import(ADDON + "buddy_suggestions.js");
    assert.equal(MAX_CHIPS, 4);
    for (const rotation of [0, 1, 7, -3, 1e9, NaN, undefined, "x"]) {
        const shown = pickChips({ pool: POOL, generic: GENERIC, rotation });
        assert.ok(shown.length <= 4 && shown.length >= 3, String(rotation));
    }
});

test("loadSuggestions: POST con JSON, token CSRF en la cabecera y cuerpo vacío; la respuesta pasa por el filtro", async () => {
    const { loadSuggestions } = await import(ADDON + "buddy_suggestions.js");
    globalThis.odoo = { csrf_token: "tok-123" };
    let seen = null;
    globalThis.fetch = async (url, options) => {
        seen = { url, options };
        return new Response(JSON.stringify({ ok: true, suggestions: [{ kind: "topic", text: "Tema" }, { kind: "x", text: "no" }] }), { status: 200 });
    };
    const result = await loadSuggestions();
    assert.equal(seen.url, "/buddy/suggestions");
    assert.equal(seen.options.method, "POST");
    assert.equal(seen.options.headers["Content-Type"], "application/json");
    assert.equal(seen.options.headers["X-CSRF-Token"], "tok-123");
    assert.equal(seen.options.body, "{}");
    assert.deepEqual(result, { ok: true, suggestions: [{ kind: "topic", text: "Tema", label: "Tema" }] });
    globalThis.fetch = async () => new Response("no es json", { status: 500 });
    assert.deepEqual(await loadSuggestions(), { ok: false, status: 500 });
    globalThis.fetch = async () => new Response(JSON.stringify({ ok: false }), { status: 429 });
    assert.deepEqual(await loadSuggestions(), { ok: false, status: 429 });
});

test("nudgeText: «Tenés N tareas para hoy», o «pendientes» cuando alguna está atrasada; nada si no hay", async () => {
    const { nudgeText, hasLateTask } = await import(ADDON + "buddy_journey.js");
    assert.equal(nudgeText({ pending: 2, late: false }), "Tenés 2 tareas para hoy");
    assert.equal(nudgeText({ pending: 1, late: false }), "Tenés 1 tarea para hoy");
    assert.equal(nudgeText({ pending: 3, late: true }), "Tenés 3 tareas pendientes");
    assert.equal(nudgeText({ pending: 1, late: true }), "Tenés 1 tarea pendiente");
    for (const pending of [0, -1, null, undefined, "2", 1.5, NaN]) {
        assert.equal(nudgeText({ pending, late: false }), "", String(pending));
    }
    assert.equal(nudgeText({ pending: 5000, late: false }), "Tenés 999 tareas para hoy");
    const task = (extra) => ({ required: true, done: false, state: "today", ...extra });
    assert.equal(hasLateTask([{ tasks: [task()] }]), false);
    assert.equal(hasLateTask([{ tasks: [task(), task({ state: "overdue" })] }]), true);
    assert.equal(hasLateTask([{ tasks: [task({ state: "overdue", done: true }), task({ state: "overdue", required: false })] }]), false);
    assert.equal(hasLateTask(null), false);
});

test("el gesto del aviso es suave (mirar alrededor) y se apaga con el grupo de expresiones", async () => {
    const { nudgeGesture, NUDGE_GESTURE, GESTURES } = await import(ADDON + "buddy_registry.js");
    assert.equal(NUDGE_GESTURE, "look");
    assert.ok(GESTURES.look.ms <= 3000 && !GESTURES.look.fx && !GESTURES.look.face, "sin efectos ni caras: solo mira");
    for (const buddy of ["kitsune", "mochi"]) {
        assert.equal(nudgeGesture({ buddy, accessory: "none", groups: ["expressions", "accessories", "sleep"] }), "look", buddy);
        assert.equal(nudgeGesture({ buddy, accessory: "shades", groups: ["expressions"] }), "look", buddy);
        assert.equal(nudgeGesture({ buddy, accessory: "none", groups: ["sleep", "accessories"] }), null, buddy);
        assert.equal(nudgeGesture({ buddy, accessory: "none", groups: [] }), null, buddy);
    }
    assert.equal(nudgeGesture({ buddy: "mochi", accessory: "none", groups: "expressions" }), null, "una lista que no es lista no vale");
});

// ------------------------------------------------------------------------------------------ the scheduler with a nudge
async function scheduler(overrides = {}) {
    const { IdleScheduler } = await import(ADDON + "buddy_idle.js");
    const log = [];
    const idle = new IdleScheduler({
        config: () => ({ frequency: "normal" }),
        canPlay: () => true,
        quietSeconds: () => 30,
        pick: () => "blink",
        onGesture: (id) => log.push(["gesture", id, Date.now()]),
        onEnd: () => log.push(["end", Date.now()]),
        random: () => 0,
        ...overrides,
    });
    return { idle, log };
}

test("el programador usa su turno para el aviso: lo pide, lo muestra y hace el gesto suave en vez de uno cualquiera", async () => {
    const offers = [];
    const { idle, log } = await scheduler({ nudge: () => ({ gesture: "look", text: "hola" }), onNudge: (offer) => offers.push([offer.text, Date.now()]) });
    idle.start();
    await tick(29 * S);
    assert.deepEqual(offers, [], "antes de la quietud, nada");
    await tick(1 * S);
    assert.equal(offers.length, 1);
    assert.deepEqual(log.map((e) => e.slice(0, 2)), [["gesture", "look"]], "el gesto del aviso, no «blink»");
    const { GESTURES } = await import(ADDON + "buddy_registry.js");
    await tick(GESTURES.look.ms);
    assert.deepEqual(log.map((e) => e[0]), ["gesture", "end"]);
});

test("si el aviso no trae gesto se muestra igual y el turno sigue con su intervalo normal", async () => {
    const offers = [];
    const { idle, log } = await scheduler({ nudge: () => ({ gesture: null }), onNudge: () => offers.push(1) });
    idle.start();
    await tick(30 * S);
    assert.equal(offers.length, 1);
    assert.deepEqual(log, []);
    assert.equal(idle.current, null);
    await tick(45 * S - 1);
    assert.equal(offers.length, 1);
    await tick(1);
    assert.equal(offers.length, 2, "el próximo turno vuelve a preguntar (quien lo da decide si hay algo que decir)");
});

test("el aviso pasa por los mismos filtros que cualquier gesto: no se pide si no está permitido actuar ni con la pausa", async () => {
    let asked = 0;
    let allowed = false;
    const { idle } = await scheduler({ canPlay: () => allowed, nudge: () => (asked++, { gesture: "look" }) });
    idle.start();
    await tick(30 * S);
    assert.equal(asked, 0, "con el chat ocupado ni se pregunta");
    idle.pause();
    allowed = true;
    await tick(10 * 60 * S);
    assert.equal(asked, 0, "en pausa (pestaña oculta, ocultar, reducir movimiento) tampoco");
    idle.resume();
    await tick(30 * S);
    assert.equal(asked, 1);
});

test("un aviso con un gesto inventado no hace un gesto raro; sin la función de aviso todo sigue como antes", async () => {
    const { idle, log } = await scheduler({ nudge: () => ({ gesture: "__proto__" }) });
    idle.start();
    await tick(30 * S);
    assert.deepEqual(log, []);
    const plain = await scheduler();
    plain.idle.start();
    await tick(30 * S);
    assert.deepEqual(plain.log.map((e) => e.slice(0, 2)), [["gesture", "blink"]]);
});

// ------------------------------------------------------------------------------------------ in the widget
const task = (extra = {}) => ({
    id: 11, title: "Conocé a tu equipo", detail: "", kind: "task", state: "today", due: "2026-10-06", days_late: 0,
    required: true, done: false, link: null, question: "", contact: null, ...extra,
});
const journey = (tasks, extra = {}) => ({
    name: "Primeros 30 días", description: "", start: "2026-10-06", day: 1, percent: 0, done: 0, total: tasks.length, pending: tasks.length, state: "active",
    tasks, upcoming: [], upcoming_total: 0, ...extra,
});
const TODAY = { ok: true, journeys: [journey([task({ id: 11 }), task({ id: 12, title: "Revisá tus accesos" })])], pending: 2 };
const LATE = { ok: true, journeys: [journey([task({ id: 11, state: "overdue", days_late: 3 }), task({ id: 12 })])], pending: 2 };
const NONE = { ok: true, journeys: [], pending: 0 };
const config = (extra = {}) => ({
    first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: true, configured: true,
    idle: { enabled: true, frequency: "high", groups: ["expressions", "accessories", "sleep"] }, ...extra,
});

async function widget({ cfg = {}, tasks = TODAY, suggestions = [], overrides = {}, prepare = () => {}, greeted = true } = {}) {
    fakeBackend({ "/buddy/config": () => config(cfg), "/buddy/journey": () => tasks, "/buddy/suggestions": () => ({ ok: true, suggestions }), ...overrides });
    if (greeted) {
        localStorage.setItem("buddy_ia.opened", "1"); // no greeting bubble in the way
    }
    prepare();
    const { Buddy } = await import(ADDON + "buddy.js");
    const mounted = await mountComponent(Buddy);
    for (let i = 0; i < 200 && !mounted.comp.state.ready; i++) {
        await frames();
    }
    for (let i = 0; i < 10; i++) {
        await frames();
    }
    return mounted;
}

const DAY = "2026-10-06";

test("con tareas para hoy el buddy avisa, una vez, con un globito breve y un gesto suave", async () => {
    const { comp } = await widget();
    assert.equal(comp.state.pending, 2);
    assert.equal($(".o_buddy_nudge"), null, "nada antes de la quietud");
    await tick(16 * S);
    await frames();
    const bubble = $(".o_buddy_nudge");
    assert.ok(bubble, "el globito del aviso");
    assert.equal(bubble.getAttribute("role"), null, "lo que se anuncia es la región viva de siempre, no el globito que nace con su texto");
    assert.equal($('.o_buddy [role="status"]').textContent, "Tenés 2 tareas para hoy", "la región viva lo dice para los lectores de pantalla");
    assert.equal($(".o_buddy_nudge .o_buddy_bubble_text").textContent, "Tenés 2 tareas para hoy");
    assert.match($(".o_buddy_nudge_cta").textContent, /Ver mis tareas/);
    assert.equal($(".o_buddy_bubble_avatar"), null, "sin el avatar grande del saludo");
    assert.equal(comp.state.gesture, "look", "un gesto suave: mira alrededor");
    assert.equal(localStorage.getItem("buddy_ia.nudged"), DAY, "queda anotado el día en que salió");
});

test("con alguna tarea atrasada dice «pendientes»", async () => {
    await widget({ tasks: LATE });
    await tick(16 * S);
    await frames();
    assert.equal($(".o_buddy_nudge .o_buddy_bubble_text").textContent, "Tenés 2 tareas pendientes");
});

test("un solo aviso por día: ni al cerrarlo, ni al recargar la página, y al otro día vuelve", async () => {
    let mounted = await widget();
    await tick(16 * S);
    await frames();
    assert.ok($(".o_buddy_nudge"));
    $(".o_buddy_nudge .o_buddy_bubble_close").click();
    await frames();
    assert.equal($(".o_buddy_nudge"), null);
    assert.equal(localStorage.getItem("buddy_ia.greeted"), null, "cerrar el aviso no cuenta como haber visto el saludo");
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null, "en toda la tarde no vuelve");
    mounted.app.destroy();
    document.body.innerHTML = "";
    mounted = await widget();
    await tick(60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null, "una página nueva el mismo día tampoco");
    mounted.app.destroy();
    document.body.innerHTML = "";
    await tick(24 * 60 * 60 * S);
    mounted = await widget();
    await tick(16 * S);
    await frames();
    assert.ok($(".o_buddy_nudge"), "al día siguiente, sí");
});

test("el globito espera mientras se lo señala o tiene el teclado encima, y se va 6 segundos después de soltarlo", async () => {
    await widget();
    await tick(16 * S);
    await frames();
    const bubble = () => $(".o_buddy_nudge");
    bubble().dispatchEvent(new window.Event("mouseenter"));
    await tick(60 * S);
    await frames();
    assert.ok(bubble(), "con el mouse encima no se va, ni en un minuto");
    bubble().dispatchEvent(new window.Event("mouseleave"));
    await tick(5 * S);
    await frames();
    assert.ok(bubble(), "a los 5 s de soltarlo sigue");
    await tick(1 * S + 100);
    await frames();
    assert.equal(bubble(), null, "a los 6 s se va");
});

test("Escape cierra el aviso; si el teclado estaba en él, el foco vuelve al botón del buddy", async () => {
    const { comp } = await widget();
    await tick(16 * S);
    await frames();
    $(".o_buddy_nudge_btn").focus();
    $(".o_buddy_nudge_btn").dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await frames();
    await frames();
    assert.equal($(".o_buddy_nudge"), null);
    assert.equal(comp.state.bubbleAction, null);
    assert.equal(document.activeElement, $(".o_buddy_launcher"), "el foco no cae a la página");
    assert.equal(localStorage.getItem("buddy_ia.nudged"), DAY, "y queda anotado igual: no vuelve hoy");
});

test("no avisa mientras la persona trabaja en la página: una tecla, un clic o la rueda lo posponen 20 segundos", async () => {
    await widget();
    await tick(10 * S);
    document.body.dispatchEvent(new window.KeyboardEvent("keydown", { key: "a", bubbles: true }));
    await tick(8 * S); // the first turn (at about 12 to 15 s) finds the page busy
    await frames();
    assert.equal($(".o_buddy_nudge"), null);
    assert.ok(await seenWithin(90 * S), "en un turno posterior, con la página quieta, sí");
});

test("no avisa si el cursor está en un campo de la página (un formulario, el chatter, un buscador)", async () => {
    const field = document.createElement("textarea");
    document.body.appendChild(field);
    const { comp } = await widget();
    field.focus();
    assert.equal(comp.typingElsewhere(), true);
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null, "escribiendo en otra parte, ni en diez minutos");
    field.blur();
    assert.equal(comp.typingElsewhere(), false);
    assert.ok(await seenWithin(2 * 60 * S), "al soltar el campo, el siguiente turno avisa");
    const own = $(".o_buddy_launcher");
    own.focus();
    assert.equal(comp.typingElsewhere(), false, "el foco en el propio buddy no cuenta");
});

test("el globito se va solo a los 15 segundos", async () => {
    await widget();
    await tick(16 * S);
    await frames();
    assert.ok($(".o_buddy_nudge"));
    await tick(14 * S);
    await frames();
    assert.ok($(".o_buddy_nudge"), "a los 14 s sigue");
    await tick(1 * S + 100);
    await frames();
    assert.equal($(".o_buddy_nudge"), null);
});

test("tocar el globito abre el chat con «Tus tareas de hoy» desplegado", async () => {
    const { comp } = await widget();
    await tick(16 * S);
    await frames();
    $(".o_buddy_nudge_btn").click();
    await frames();
    assert.equal(comp.state.open, true);
    assert.equal(comp.state.tasksOpen, true);
    assert.equal($(".o_buddy_nudge"), null);
    assert.ok($("#o_buddy_tasks_body"), "la lista de tareas está abierta");
    assert.equal($(".o_buddy_tasks_head").getAttribute("aria-expanded"), "true");
});

test("sin movimiento no hay aviso: con «reducir movimiento» el programador no corre y queda el puntito del botón", async () => {
    const query = { matches: true, addEventListener: () => {}, removeEventListener: () => {} };
    const { comp } = await widget({ prepare: () => (window.matchMedia = () => query) });
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null);
    assert.equal(comp.state.gesture, null);
    assert.ok($(".o_buddy_dot"), "el punto ámbar (quieto) sigue avisando");
});

test("no avisa con la pestaña oculta ni con el buddy oculto por la persona ni deshabilitado", async () => {
    let mounted = await widget();
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new window.Event("visibilitychange"));
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null, "pestaña oculta");
    mounted.app.destroy();
    document.body.innerHTML = "";
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    mounted = await widget({ prepare: () => localStorage.setItem("buddy_ia.hidden", "1") });
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null, "el buddy está oculto");
    mounted.app.destroy();
    document.body.innerHTML = "";
    mounted = await widget({ cfg: { enabled: false } });
    await tick(10 * 60 * S);
    assert.equal($(".o_buddy_nudge"), null, "el administrador lo ocultó a todos");
});

test("con los gestos apagados por el administrador no hay aviso; con el grupo de expresiones apagado el globito sale sin movimiento", async () => {
    let mounted = await widget({ cfg: { idle: { enabled: false, frequency: "high", groups: ["expressions"] } } });
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null);
    mounted.app.destroy();
    document.body.innerHTML = "";
    mounted = await widget({ cfg: { idle: { enabled: true, frequency: "high", groups: ["sleep"] } } });
    await tick(16 * S);
    await frames();
    assert.ok($(".o_buddy_nudge"), "sale igual");
    assert.equal(mounted.comp.state.gesture, null, "pero sin gesto: el administrador apagó las expresiones");
});

test("el administrador puede apagar el aviso (y las sugerencias) desde Ajustes", async () => {
    const { comp } = await widget({ cfg: { proactive: { suggestions: false, nudge: false } }, suggestions: [{ kind: "topic", text: "Tema" }] });
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null);
    assert.equal(comp.state.proactive.nudge, false);
    assert.equal(calls("/buddy/suggestions").length, 0, "ni siquiera pide las sugerencias");
    $(".o_buddy_launcher").click();
    await frames();
    assert.equal(chipTexts().length, 3, "los fijos de siempre");
    assert.ok(!chipTexts().includes("Tema"));
});

test("un servidor que no dice nada de esto (versión anterior) deja todo encendido", async () => {
    const { comp } = await widget();
    assert.deepEqual(comp.state.proactive, { suggestions: true, nudge: true });
});

test("no avisa si no hay tareas, si la persona ya cerró la lista hoy, si el chat está abierto o si el saludo está en pantalla", async () => {
    let mounted = await widget({ tasks: NONE });
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null, "sin tareas");
    mounted.app.destroy();
    document.body.innerHTML = "";
    mounted = await widget({ prepare: () => localStorage.setItem("buddy_ia.tasks_closed", new Date().toISOString().slice(0, 10)) });
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null, "la persona ya cerró «Tus tareas de hoy» hoy: ya sabe");
    mounted.app.destroy();
    document.body.innerHTML = "";
    mounted = await widget();
    mounted.comp.toggle();
    await frames();
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null, "con el chat abierto");
    mounted.app.destroy();
    document.body.innerHTML = "";
    localStorage.clear(); // the greeting is for a person who never opened the chat and was not greeted today
    mounted = await widget({ greeted: false });
    await tick(3 * S);
    await frames();
    assert.ok($(".o_buddy_bubble"), "primero el saludo");
    assert.equal($(".o_buddy_nudge"), null);
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null, "un solo globito por visita: el aviso no sigue al saludo");
});

test("el aviso solo aparece si el chat se cierra: al abrirlo se va y no vuelve a salir ese día", async () => {
    const { comp } = await widget();
    await tick(16 * S);
    await frames();
    assert.ok($(".o_buddy_nudge"));
    comp.toggle();
    await frames();
    assert.equal($(".o_buddy_nudge"), null);
    comp.toggle();
    await tick(10 * 60 * S);
    await frames();
    assert.equal($(".o_buddy_nudge"), null);
});

test("el texto del aviso es texto: nunca HTML", async () => {
    const hostile = { ok: true, journeys: [journey([task({ id: 11, title: "<img src=x onerror=alert(1)>" })])], pending: 1 };
    await widget({ tasks: hostile });
    await tick(16 * S);
    await frames();
    assert.equal($(".o_buddy_nudge .o_buddy_bubble_text").textContent, "Tenés 1 tarea para hoy");
    assert.equal($(".o_buddy_nudge img"), null);
});

// ------------------------------------------------------------------------------------------ the chips
const POOL_FROM_SERVER = [
    { kind: "step", text: "¿Cómo pido un día libre?", label: "¿Cómo pido un día libre?", ask_id: 41 },
    { kind: "topic", text: "¿Qué dice «PR 38 Backups»?", label: "¿Qué dice «PR 38 Backups»?" },
    { kind: "topic", text: "¿Qué dice «Guía de ingreso»?", label: "¿Qué dice «Guía de ingreso»?" },
    { kind: "next", text: "¿Dónde pido mi notebook?", label: "¿Dónde pido mi notebook?" },
];

test("los chips son un grupo con nombre y los cortados dicen su pregunta entera a los lectores de pantalla", async () => {
    const long = "¿Cómo se carga una factura de proveedor en Odoo cuando el monto supera el tope de compras que aprobó finanzas este año?";
    await widget({ suggestions: [{ kind: "step", text: long }, { kind: "topic", text: "Tema corto" }] });
    $(".o_buddy_launcher").click();
    await frames();
    const group = $(".o_buddy_chips");
    assert.equal(group.getAttribute("role"), "group");
    assert.equal(group.getAttribute("aria-label"), "Preguntas sugeridas");
    const [cut, short] = $$(".o_buddy_chip");
    assert.equal(cut.getAttribute("aria-label"), long);
    assert.equal(short.getAttribute("aria-label"), null, "uno que no se cortó se lee como está");
    assert.equal(short.getAttribute("title"), null);
    assert.equal(cut.getAttribute("title"), long);
});

test("los chips son los que corresponden: tres a medida y un fijo; el texto que se manda es el de la sugerencia", async () => {
    const { comp } = await widget({ suggestions: POOL_FROM_SERVER });
    assert.equal(calls("/buddy/suggestions").length, 1);
    $(".o_buddy_launcher").click();
    await frames();
    const texts = chipTexts();
    assert.equal(texts.length, 4);
    assert.equal(texts[0], "¿Cómo pido un día libre?", "lo urgente va primero");
    assert.equal(texts.filter((text) => /^¿Qué dice|^¿Dónde pido|^¿Cómo pido/.test(text)).length, 3);
    assert.equal(texts[3], "Soy nuevo, ¿por dónde empiezo?");
    $$(".o_buddy_chip")[1].click();
    await frames();
    assert.equal(comp.state.messages.find((m) => m.role === "user").text, texts[1]);
    assert.equal(calls("/buddy/chat").at(-1).params.message, texts[1]);
});

test("un chip largo muestra su comienzo y guarda la pregunta entera para mandarla", async () => {
    const long = "¿Cómo se carga una factura de proveedor en Odoo cuando el monto supera el tope de compras que aprobó finanzas este año?";
    const { comp } = await widget({ suggestions: [{ kind: "step", text: long }] });
    $(".o_buddy_launcher").click();
    await frames();
    const button = $$(".o_buddy_chip")[0];
    assert.ok(button.textContent.length <= 80 && button.textContent.endsWith("…"));
    assert.equal(button.getAttribute("title"), long);
    button.click();
    await frames();
    assert.equal(calls("/buddy/chat").at(-1).params.message, long);
    assert.ok(comp.state.messages.some((m) => m.text === long));
});

test("las sugerencias rotan cada vez que se abre el panel, y lo urgente no se mueve", async () => {
    const pool = [
        { kind: "step", text: "Urgente" },
        ...["A", "B", "C", "D", "E"].map((letter) => ({ kind: "topic", text: "Tema " + letter })),
    ];
    const { comp } = await widget({ suggestions: pool });
    const turns = new Set();
    for (let i = 0; i < 5; i++) {
        $(".o_buddy_launcher").click();
        await frames();
        const texts = chipTexts();
        assert.equal(texts[0], "Urgente");
        turns.add(texts.slice(1, 3).join("|"));
        comp.toggle();
        await frames();
    }
    assert.ok(turns.size >= 3, "cambian de una apertura a otra: " + [...turns]);
});

test("los chips son texto y nada más, aunque el servidor mande HTML", async () => {
    await widget({ suggestions: [{ kind: "topic", text: "<img src=x onerror=alert(1)>" }, { kind: "topic", text: "<b>negrita</b>" }] });
    $(".o_buddy_launcher").click();
    await frames();
    assert.equal($(".o_buddy_chips img"), null);
    assert.equal($(".o_buddy_chips b"), null);
    assert.ok(chipTexts().includes("<b>negrita</b>"));
});

test("si pedir las sugerencias falla, quedan los chips fijos y el chat anda igual", async () => {
    const { comp } = await widget({ overrides: { "/buddy/suggestions": () => ({ __status: 500, ok: false }) } });
    $(".o_buddy_launcher").click();
    await frames();
    assert.equal(chipTexts().length, 3);
    assert.equal(chipTexts()[0], "Soy nuevo, ¿por dónde empiezo?");
    assert.equal(comp.state.contextual.length, 0);
    comp.state.input = "hola";
    comp.send();
    await frames();
    assert.equal(calls("/buddy/chat").length, 1);
});

test("si no hay nada a medida, los chips son los fijos que escribió el administrador, como siempre", async () => {
    await widget({ cfg: { suggestions: ["¿Cómo pido vacaciones?", "¿Dónde está el wifi?"] } });
    $(".o_buddy_launcher").click();
    await frames();
    assert.deepEqual(chipTexts(), ["¿Cómo pido vacaciones?", "¿Dónde está el wifi?"]);
});

test("las sugerencias a medida tienen que completarse con los fijos del administrador sin pasar de cuatro", async () => {
    await widget({ cfg: { suggestions: ["Fija 1", "Fija 2", "Fija 3", "Fija 4"] }, suggestions: [{ kind: "topic", text: "Tema A" }] });
    $(".o_buddy_launcher").click();
    await frames();
    assert.deepEqual(chipTexts(), ["Tema A", "Fija 1", "Fija 2", "Fija 3"]);
});

test("un chip que es la pregunta de un paso «preguntarle al buddy» tilda el paso cuando llega la respuesta", async () => {
    const { comp } = await widget({ suggestions: [{ kind: "step", text: "¿Cómo pido un día libre?", ask_id: 41 }] });
    $(".o_buddy_launcher").click();
    await frames();
    $$(".o_buddy_chip")[0].click();
    for (let i = 0; i < 10; i++) {
        await frames();
    }
    assert.equal(calls("/buddy/chat").length, 1);
    const ticks = calls("/buddy/journey/step");
    assert.equal(ticks.length, 1);
    assert.deepEqual(ticks[0].params, { id: 41, done: true });
    assert.ok(comp.state.tasksStatus.includes("¿Cómo pido un día libre?"), "avisa que quedó hecho");
});

test("si la respuesta falla, el paso NO se tilda", async () => {
    await widget({ suggestions: [{ kind: "step", text: "¿Cómo pido un día libre?", ask_id: 41 }], overrides: { "/buddy/chat": () => ({ error: "provider_error", answer: "No pude" }) } });
    $(".o_buddy_launcher").click();
    await frames();
    $$(".o_buddy_chip")[0].click();
    for (let i = 0; i < 10; i++) {
        await frames();
    }
    assert.equal(calls("/buddy/journey/step").length, 0);
});

test("un chip de tema no tilda nada", async () => {
    await widget({ suggestions: [{ kind: "topic", text: "Tema A" }] });
    $(".o_buddy_launcher").click();
    await frames();
    $$(".o_buddy_chip")[0].click();
    for (let i = 0; i < 6; i++) {
        await frames();
    }
    assert.equal(calls("/buddy/journey/step").length, 0);
});

test("lo que la persona ya preguntó no se vuelve a ofrecer, y con la conversación empezada los chips no se muestran", async () => {
    const { comp } = await widget({ suggestions: POOL_FROM_SERVER });
    const asked = "¿Qué dice «PR 38 Backups»?";
    $(".o_buddy_launcher").click();
    await frames();
    comp.state.messages.push({ id: 90, role: "user", text: asked.toLowerCase() });
    assert.ok(!comp.suggestions.some((chip) => chip.text === asked), "el filtro mira la conversación");
    comp.state.messages.length = 0;
    comp.state.messages.push({ id: 91, role: "user", text: "otra cosa" });
    await frames();
    assert.equal($(".o_buddy_chips"), null);
});

test("pide las sugerencias una vez al cargar y, al abrir el panel, solo si pasaron más de 30 minutos", async () => {
    const { comp } = await widget({ suggestions: POOL_FROM_SERVER });
    assert.equal(calls("/buddy/suggestions").length, 1);
    $(".o_buddy_launcher").click();
    await frames();
    comp.toggle();
    comp.toggle();
    await frames();
    assert.equal(calls("/buddy/suggestions").length, 1, "dentro de los 30 minutos no vuelve a pedirlas");
    comp.toggle();
    await tick(31 * 60 * S);
    comp.toggle();
    await frames();
    assert.equal(calls("/buddy/suggestions").length, 2);
});
