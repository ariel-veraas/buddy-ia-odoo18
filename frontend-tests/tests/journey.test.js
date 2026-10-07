import { test, before, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountBuddy, flush, calls, destroyApps } from "./helpers.js";
import { cleanJourneys, cleanTask, safeHttpsUrl, loadJourneys, setStepDone, summaryText, lateText, untilText } from "../../odoo-addons/buddy_ia/static/src/js/buddy_journey.js";

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

const task = (extra = {}) => ({
    id: 11, title: "Conocé a tu equipo", detail: "Presentate con tu área.", kind: "task", state: "today", due: "2026-10-06", days_late: 0,
    required: true, done: false, link: null, question: "", contact: null, ...extra,
});
const journey = (extra = {}) => ({
    name: "Primeros 30 días", description: "", start: "2026-10-06", day: 1, percent: 33, done: 1, total: 3, pending: 2, state: "active",
    tasks: [task(), task({ id: 12, title: "Revisá tus accesos", state: "overdue", days_late: 2 }), task({ id: 13, title: "Hecha hoy", done: true, state: "done" })],
    upcoming: [task({ id: 14, title: "Charla de la semana", state: "upcoming" })],
    ...extra,
});
const PAYLOAD = { ok: true, journeys: [journey()], pending: 2 };

async function mount(handlers = {}, payload = PAYLOAD) {
    fakeBackend({ "/buddy/journey": () => payload, ...handlers });
    const mounted = await mountBuddy();
    await flush();
    return mounted;
}
const openPanel = async () => {
    $(".o_buddy_launcher").click();
    await flush();
};

// ------------------------------------------------------------------------------------------ filters
test("safeHttpsUrl: solo https con host de verdad, sin credenciales ni caracteres raros", () => {
    for (const good of ["https://intranet.empresa.com/guia", "https://empresa.com/a?b=1#c", "https://sub.empresa.com:8443/x"]) {
        assert.equal(safeHttpsUrl(good), good, good);
    }
    for (const bad of [
        "http://empresa.com/x", "javascript:alert(1)", "data:text/html,x", "//empresa.com/x", "https://user@empresa.com/x", "https://user:pw@empresa.com/x",
        "https://empresa.com\\@evil.example/", "https://empresa.com/a b", "https://empresa.com/\n", "https://localhost/x", "https://intranet/x",
        "https://empresa.com./x", "https://empresa.com:99999/x", "https://", "", null, undefined, 7, { toString: () => "https://empresa.com/" },
        "https://192.168.1.1/", "https://10.0.0.5/admin", "https://0x7f.0.0.1/", "https://2130706433/", "https://[::1]/x", "https://empresa.123/x", "https://empresa.c/x",
        "https://empresa.com/" + "a".repeat(500),
    ]) {
        assert.equal(safeHttpsUrl(bad), "", String(bad));
    }
});

test("cleanTask: campos conocidos, tipos y topes; lo inservible se descarta", () => {
    assert.equal(cleanTask(null), null);
    assert.equal(cleanTask({ id: "1", title: "x" }), null);
    assert.equal(cleanTask({ id: 0, title: "x" }), null);
    assert.equal(cleanTask({ id: 1, title: "" }), null);
    assert.equal(cleanTask({ id: 1, title: 5 }), null);
    const clean = cleanTask({ id: 5, title: "T".repeat(300), detail: "D".repeat(900), kind: "hack", state: "weird", days_late: -4, required: "no", done: "yes", extra: "x" });
    assert.equal(clean.title.length, 120);
    assert.equal(clean.detail.length, 600);
    assert.deepEqual([clean.kind, clean.state, clean.daysLate, clean.required, clean.done, clean.busy], ["task", "today", 0, true, false, false]);
    assert.equal("extra" in clean, false);
});

test("cleanTask: los enlaces de un documento solo son de Drive y los demás https; nada de javascript:", () => {
    const link = (kind, url) => cleanTask({ id: 1, title: "x", link: { kind, label: "L", url } }).link;
    assert.deepEqual(link("doc", "https://docs.google.com/document/d/1/edit"), { kind: "doc", label: "L", url: "https://docs.google.com/document/d/1/edit" });
    assert.equal(link("doc", "https://evil.example/x"), null);
    assert.equal(link("doc", "https://docs.google.com\\@evil.example/"), null);
    assert.deepEqual(link("url", "https://intranet.empresa.com/x"), { kind: "url", label: "L", url: "https://intranet.empresa.com/x" });
    for (const url of ["javascript:alert(1)", "http://empresa.com/", "https://u:p@empresa.com/", "", null, 3]) {
        assert.equal(link("url", url), null, String(url));
    }
    assert.equal(cleanTask({ id: 1, title: "x", link: "https://empresa.com/" }).link, null);
});

test("cleanJourneys: topes de recorridos y de pasos, números acotados y nada que no sea de la lista", () => {
    const many = { journeys: Array.from({ length: 9 }, (_, i) => journey({ name: "R" + i })) };
    assert.equal(cleanJourneys(many).journeys.length, 5);
    assert.equal(cleanJourneys({ journeys: [journey({ tasks: Array.from({ length: 200 }, (_, i) => task({ id: i + 1 })) })] }).journeys[0].tasks.length, 60);
    const odd = cleanJourneys({ journeys: [journey({ percent: 900, done: -1, total: "3", day: 0, pending: 2 }), { name: "" }, null, "x", { name: "Sin pasos" }] });
    assert.equal(odd.journeys.length, 2);
    assert.deepEqual([odd.journeys[0].percent, odd.journeys[0].done, odd.journeys[0].total, odd.journeys[0].day], [100, 0, 0, 1]);
    assert.deepEqual(odd.journeys[1].tasks, []);
    assert.equal(odd.pending, 2);
    assert.deepEqual(cleanJourneys(null), { journeys: [], pending: 0 });
    assert.deepEqual(cleanJourneys({ journeys: "x" }), { journeys: [], pending: 0 });
});

test("textos del resumen y del atraso, en singular y plural", () => {
    assert.equal(summaryText(0), "Todo al día");
    assert.equal(summaryText(1), "1 pendiente");
    assert.equal(summaryText(4), "4 pendientes");
    assert.equal(lateText(1), "Atrasada 1 día");
    assert.equal(lateText(5), "Atrasada 5 días");
    assert.equal(untilText(0), "mañana");
    assert.equal(untilText(1), "mañana");
    assert.equal(untilText(3), "en 3 días");
});

// ------------------------------------------------------------------------------------------ the requests
test("loadJourneys y setStepDone: POST JSON con el token CSRF en una cabecera y el cuerpo exacto", async () => {
    fakeBackend({ "/buddy/journey": () => PAYLOAD, "/buddy/journey/step": () => PAYLOAD });
    globalThis.odoo = { csrf_token: "tok-9" };
    const list = await loadJourneys();
    assert.equal(list.ok, true);
    assert.equal(list.pending, 2);
    await setStepDone(11, true);
    const [first, second] = ["/buddy/journey", "/buddy/journey/step"].map((route) => calls(route).at(-1));
    assert.deepEqual(first.params, {});
    assert.deepEqual(second.params, { id: 11, done: true });
    for (const call of [first, second]) {
        assert.equal(call.headers["X-CSRF-Token"], "tok-9");
        assert.equal(call.headers["Content-Type"], "application/json");
    }
});

test("una respuesta de error se vuelve {ok:false} con el estado y el mensaje del servidor (cortado)", async () => {
    fakeBackend({ "/buddy/journey/step": () => ({ __status: 429, ok: false, message: "M".repeat(900) }) });
    const result = await setStepDone(1, true);
    assert.equal(result.ok, false);
    assert.equal(result.status, 429);
    assert.equal(result.message.length, 300);
    fakeBackend({ "/buddy/journey": () => ({ __status: 200, ok: false }) });
    assert.equal((await loadJourneys()).ok, false);
});

// ------------------------------------------------------------------------------------------ the widget
test("sin recorridos no hay bloque ni punto, y el chat funciona igual", async () => {
    await mount({}, { ok: true, journeys: [], pending: 0 });
    assert.equal($(".o_buddy_dot"), null);
    await openPanel();
    assert.equal($(".o_buddy_tasks"), null);
    assert.ok($(".o_buddy_input"));
});

test("con tareas para hoy el lanzador muestra un punto discreto, accesible, que se va al abrir el chat", async () => {
    await mount();
    const dot = $(".o_buddy_launcher .o_buddy_dot");
    assert.ok(dot);
    assert.equal(dot.getAttribute("aria-hidden"), "true");
    assert.match($(".o_buddy_launcher").getAttribute("aria-label"), /tareas de onboarding para hoy/);
    await openPanel();
    assert.equal($(".o_buddy_dot"), null, "con el chat abierto no hace falta el punto");
});

test("sin tareas obligatorias pendientes no hay punto, pero el bloque existe", async () => {
    await mount({}, { ok: true, journeys: [journey({ pending: 0, tasks: [] , upcoming: [] })], pending: 0 });
    assert.equal($(".o_buddy_dot"), null);
    assert.doesNotMatch($(".o_buddy_launcher").getAttribute("aria-label"), /onboarding/);
    await openPanel();
    assert.ok($(".o_buddy_tasks"));
    assert.match($(".o_buddy_tasks_head").textContent, /Todo al día/);
});

test("el punto no aparece con el buddy oculto, y vuelve al mostrarlo", async () => {
    localStorage.setItem("buddy_ia.hidden", "1");
    await mount();
    assert.equal($(".o_buddy_dot"), null);
    assert.ok($(".o_buddy_restore"));
    $(".o_buddy_restore").click();
    await flush();
    assert.ok($(".o_buddy_dot"));
});

test("si el administrador apagó el buddy no se pide nada de onboarding", async () => {
    fakeBackend({ "/buddy/config": () => ({ first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: false }) });
    await mountBuddy();
    await flush();
    assert.equal(calls("/buddy/journey").length, 0);
});

test("la primera vez que se abre el chat la lista de tareas se abre sola y muestra el progreso", async () => {
    await mount();
    await openPanel();
    const head = $(".o_buddy_tasks_head");
    assert.equal(head.getAttribute("aria-expanded"), "true");
    assert.match(head.textContent, /Tus tareas de hoy/);
    assert.match(head.textContent, /2 pendientes/);
    assert.match($(".o_buddy_journey_title").textContent, /Primeros 30 días/);
    assert.match($(".o_buddy_journey_title").textContent, /Día 1/);
    const bar = $(".o_buddy_progress");
    assert.equal(bar.getAttribute("role"), "progressbar");
    assert.equal(bar.getAttribute("aria-valuenow"), "33");
    assert.match(bar.getAttribute("aria-label"), /^Primeros 30 días: 1 de 3 tareas obligatorias hechas$/);
    assert.equal(bar.firstElementChild.getAttribute("style"), "width: 33%");
    assert.match($(".o_buddy_journey_pct").textContent, /33%/);
    assert.equal($$(".o_buddy_task").length, 4);
    assert.ok($(".o_buddy_task.is-overdue"));
    assert.match($(".o_buddy_task_tag.is-late").textContent, /Atrasada 2 días/);
    assert.ok($(".o_buddy_task.is-done"));
});

test("el encabezado abre y cierra la lista con aria-expanded, y las próximas van en un desplegable", async () => {
    await mount();
    await openPanel();
    $(".o_buddy_tasks_head").click();
    await flush();
    assert.equal($(".o_buddy_tasks_head").getAttribute("aria-expanded"), "false");
    assert.equal($(".o_buddy_tasks_body"), null);
    $(".o_buddy_tasks_head").click();
    await flush();
    assert.equal($(".o_buddy_tasks_head").getAttribute("aria-expanded"), "true");
    assert.equal($(".o_buddy_upcoming summary").textContent.trim(), "Próximas (1)");
    assert.match($(".o_buddy_upcoming").textContent, /Charla de la semana/);
});

test("tildar un paso manda {id, done} y la lista vuelve actualizada; el estado se anuncia", async () => {
    const updated = { ok: true, journeys: [journey({ percent: 67, done: 2, pending: 1, tasks: [task({ done: true, state: "done" }), task({ id: 12, title: "Revisá tus accesos", state: "overdue", days_late: 2 })] })], pending: 1 };
    await mount({ "/buddy/journey/step": () => updated });
    await openPanel();
    const box = $$(".o_buddy_task_check input")[0];
    box.checked = true;
    box.dispatchEvent(new window.Event("change", { bubbles: true }));
    await flush();
    assert.deepEqual(calls("/buddy/journey/step")[0].params, { id: 11, done: true });
    assert.equal($(".o_buddy_progress").getAttribute("aria-valuenow"), "67");
    assert.match($(".o_buddy_tasks_head").textContent, /1 pendiente/);
    assert.match($("[role=status].o_buddy_sr").textContent, /Marcaste «Conocé a tu equipo» como hecha/);
});

test("destildar manda done=false", async () => {
    await mount();
    await openPanel();
    const box = $$(".o_buddy_task_check input").find((input) => input.checked);
    assert.ok(box);
    box.checked = false;
    box.dispatchEvent(new window.Event("change", { bubbles: true }));
    await flush();
    assert.deepEqual(calls("/buddy/journey/step")[0].params, { id: 13, done: false });
});

test("si el servidor rechaza el cambio, el tilde vuelve atrás y se explica con un aviso", async () => {
    await mount({ "/buddy/journey/step": () => ({ __status: 429, ok: false, message: "Estás tocando muy seguido." }) });
    await openPanel();
    const box = $$(".o_buddy_task_check input")[0];
    box.checked = true;
    box.dispatchEvent(new window.Event("change", { bubbles: true }));
    await flush();
    assert.equal($$(".o_buddy_task_check input")[0].checked, false);
    assert.match($(".o_buddy_tasks [role=alert]").textContent, /muy seguido/);
});

test("una falla de red al tildar no rompe nada: tilde revertido y mensaje", async () => {
    await mount({ "/buddy/journey/step": () => { throw new Error("offline"); } });
    await openPanel();
    const box = $$(".o_buddy_task_check input")[0];
    box.checked = true;
    box.dispatchEvent(new window.Event("change", { bubbles: true }));
    await flush();
    assert.equal($$(".o_buddy_task_check input")[0].checked, false);
    assert.match($(".o_buddy_tasks [role=alert]").textContent, /No pude guardar/);
    assert.ok($(".o_buddy_input"));
});

test("«Preguntarle al buddy» manda la pregunta sugerida al chat y cierra la lista", async () => {
    const ask = task({ id: 21, title: "Preguntale al buddy", kind: "ask", question: "¿Cómo pido vacaciones?" });
    const payload = { ok: true, journeys: [journey({ tasks: [ask], pending: 1 })], pending: 1 };
    await mount({ "/buddy/journey/step": () => ({ ...payload, journeys: [journey({ tasks: [{ ...ask, done: true, state: "done" }], pending: 0 })], pending: 0 }),
        "/buddy/chat": () => ({ answer: "Pedilas en Odoo.", expression: "happy", ring: "ok", sources: [], ticket: null, contact: null, provider: "llm" }) }, payload);
    await openPanel();
    $(".o_buddy_task_ask").click();
    await flush(150);
    assert.equal(calls("/buddy/chat")[0].params.message, "¿Cómo pido vacaciones?");
    assert.equal($(".o_buddy_tasks_head").getAttribute("aria-expanded"), "false");
    assert.match($(".is-user").textContent, /¿Cómo pido vacaciones\?/);
    assert.match($$(".is-buddy").at(-1).textContent, /Pedilas en Odoo/);
});

test("«Preguntarle al buddy» no se ofrece sin pregunta y está apagado si el buddy no está configurado", async () => {
    const ask = task({ id: 21, title: "Preguntar", kind: "ask", question: "¿Qué hago hoy?" });
    await mount({ "/buddy/config": () => ({ first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: true, configured: false }) },
        { ok: true, journeys: [journey({ tasks: [ask, task({ id: 22, title: "Sin pregunta" })], pending: 2 })], pending: 2 });
    await openPanel();
    const buttons = $$(".o_buddy_task_ask");
    assert.equal(buttons.length, 1);
    assert.equal(buttons[0].disabled, true);
    buttons[0].click();
    await flush();
    assert.equal(calls("/buddy/chat").length, 0);
});

test("los enlaces: documento y https se abren en otra pestaña sin opener; un enlace peligroso nunca se dibuja", async () => {
    const tasks = [
        task({ id: 31, title: "Leer guía", kind: "read", link: { kind: "doc", label: "Guía de bienvenida", url: "https://docs.google.com/document/d/1/edit" } }),
        task({ id: 32, title: "Intranet", kind: "read", link: { kind: "url", label: "intranet.empresa.com", url: "https://intranet.empresa.com/guia" } }),
        task({ id: 33, title: "Peligroso", kind: "read", link: { kind: "url", label: "x", url: "javascript:alert(1)" } }),
        task({ id: 34, title: "Documento ajeno", kind: "read", link: { kind: "doc", label: "x", url: "https://evil.example/doc" } }),
    ];
    await mount({}, { ok: true, journeys: [journey({ tasks, pending: 4 })], pending: 4 });
    await openPanel();
    const links = $$(".o_buddy_task_link");
    assert.deepEqual(links.map((a) => a.getAttribute("href")), ["https://docs.google.com/document/d/1/edit", "https://intranet.empresa.com/guia"]);
    for (const a of links) {
        assert.equal(a.getAttribute("target"), "_blank");
        assert.match(a.getAttribute("rel"), /noopener/);
        assert.match(a.getAttribute("rel"), /noreferrer/);
    }
    assert.match(links[0].textContent, /Abrir el documento: Guía de bienvenida/);
    assert.match(links[1].textContent, /Abrir el enlace: intranet\.empresa\.com/);
    assert.equal($$("a[href^=javascript]").length, 0);
    assert.equal($$("a[href*=evil]").length, 0);
});

test("lo que llega del servidor se dibuja siempre como texto, nunca como HTML", async () => {
    const evil = task({ id: 41, title: "<img src=x onerror=alert(1)>", detail: "<b>negrita</b>", contact: { label: "<i>RR.HH.</i>", name: "<script>x</script>", contact: "" } });
    await mount({}, { ok: true, journeys: [journey({ name: "<u>Mi recorrido</u>", tasks: [evil], pending: 1 })], pending: 1 });
    await openPanel();
    assert.equal($(".o_buddy_tasks img"), null);
    assert.equal($(".o_buddy_tasks b"), null);
    assert.equal($(".o_buddy_tasks script"), null);
    assert.equal($(".o_buddy_tasks u"), null);
    assert.match($(".o_buddy_tasks").textContent, /<img src=x onerror=alert\(1\)>/);
});

test("cleanTask: un correo de contacto que no tiene forma de correo no llega a un mailto:", () => {
    const contact = (email) => cleanTask({ id: 1, title: "x", contact: { label: "RR.HH.", name: "Equipo", contact: email } }).contact.contact;
    assert.equal(contact("rrhh@empresa.com"), "rrhh@empresa.com");
    for (const bad of ["x y@a.com", "a@b@c.com", "a<b@c.com", "javascript:alert(1)", 'a"b@c.com', "sin-arroba", "x@y.com?cc=otro@z.com", "x@y.com&bcc=z", "x@y.com%0Abcc", "a,b@c.com", "", null, 5]) {
        assert.equal(contact(bad), "", String(bad));
    }
});

test("el contacto del paso se muestra con su correo", async () => {
    const withContact = task({ id: 51, title: "Hablar", contact: { label: "RR.HH.", name: "Equipo de RR.HH.", contact: "rrhh@empresa.com" } });
    await mount({}, { ok: true, journeys: [journey({ tasks: [withContact], pending: 1 })], pending: 1 });
    await openPanel();
    assert.match($(".o_buddy_task_contact").textContent, /Con quién hablar: RR\.HH\. · Equipo de RR\.HH\./);
    assert.equal($(".o_buddy_task_contact a").getAttribute("href"), "mailto:rrhh@empresa.com");
});

test("un paso hecho no muestra enlaces ni preguntas; uno opcional lo dice", async () => {
    const done = task({ id: 61, title: "Ya hecho", done: true, state: "done", link: { kind: "url", label: "x.com", url: "https://x.example.com/" }, question: "¿Algo?" });
    const optional = task({ id: 62, title: "Extra", required: false });
    await mount({}, { ok: true, journeys: [journey({ tasks: [done, optional], pending: 0 })], pending: 0 });
    await openPanel();
    $(".o_buddy_tasks_head").click();
    await flush();
    assert.equal($(".o_buddy_task.is-done .o_buddy_task_actions"), null);
    assert.match($$(".o_buddy_journey > .o_buddy_task_list > .o_buddy_task").at(-1).textContent, /Opcional/);
});

test("un recorrido sin tareas para hoy dice que está todo al día", async () => {
    await mount({}, { ok: true, journeys: [journey({ tasks: [], pending: 0, upcoming: [] })], pending: 0 });
    await openPanel();
    $(".o_buddy_tasks_head").click();
    await flush();
    assert.match($(".o_buddy_tasks_empty").textContent, /Todo al día/);
});

test("si /buddy/journey falla, el chat sigue como siempre (sin bloque ni punto)", async () => {
    await mount({ "/buddy/journey": () => { throw new Error("500"); } });
    assert.equal($(".o_buddy_dot"), null);
    await openPanel();
    assert.equal($(".o_buddy_tasks"), null);
    assert.ok($(".o_buddy_input"));
});

test("al reabrir el chat se refresca la lista, a lo sumo cada 30 segundos", async () => {
    await mount();
    assert.equal(calls("/buddy/journey").length, 1);
    await openPanel();
    $(".o_buddy_close").click();
    await flush();
    await openPanel();
    assert.equal(calls("/buddy/journey").length, 1, "menos de 30 s: no se vuelve a pedir");
});

test("los textos del bloque pasan por la traducción", async () => {
    fakeBackend({ "/buddy/journey": () => PAYLOAD });
    globalThis.__translations = { "Todo al día": "Todo listo", "%(count)s pendientes": "%(count)s por hacer", "Día %(day)s": "Jornada %(day)s", "Atrasada %(days)s días": "Con %(days)s días de retraso" };
    await mountBuddy();
    await flush();
    await openPanel();
    assert.match($(".o_buddy_tasks_head").textContent, /2 por hacer/);
    assert.match($(".o_buddy_journey_title").textContent, /Jornada 1/);
    assert.match($(".o_buddy_task_tag.is-late").textContent, /Con 2 días de retraso/);
});

test("un paso opcional vencido no se marca como atrasado ni con borde de alerta", async () => {
    const optional = task({ id: 71, title: "Extra vencido", required: false, state: "overdue", days_late: 4 });
    await mount({}, { ok: true, journeys: [journey({ tasks: [optional], pending: 0 })], pending: 0 });
    await openPanel();
    $(".o_buddy_tasks_head").click();
    await flush();
    const li = $(".o_buddy_journey > .o_buddy_task_list > .o_buddy_task");
    assert.equal(li.classList.contains("is-overdue"), false);
    assert.equal($(".o_buddy_task_tag.is-late"), null);
    assert.match(li.textContent, /Opcional/);
});

test("el tilde no se desactiva mientras se guarda (el foco del teclado no se pierde) y describe su estado", async () => {
    await mount();
    await openPanel();
    const box = $$(".o_buddy_task_check input")[0];
    assert.equal(box.hasAttribute("disabled"), false);
    const meta = document.getElementById(box.getAttribute("aria-describedby"));
    assert.ok(meta, "el tilde apunta a su marca de atraso y a su detalle");
    assert.equal(meta.className, "o_buddy_task_meta");
});

test("las próximas dicen cuándo tocan y el rótulo cuenta todas, no solo las que se muestran", async () => {
    const upcoming = [task({ id: 81, title: "En tres días", state: "upcoming", days_until: 3 }), task({ id: 82, title: "Mañana", state: "upcoming", days_until: 1 })];
    await mount({}, { ok: true, journeys: [journey({ upcoming, upcoming_total: 8 })], pending: 2 });
    await openPanel();
    assert.equal($(".o_buddy_upcoming summary").textContent.trim(), "Próximas (8)");
    const text = $(".o_buddy_upcoming").textContent;
    assert.match(text, /En tres días\s*en 3 días/);
    assert.match(text, /Mañana\s*mañana/);
    assert.match(text, /…y 6 más/);
});

test("si la persona cierra la lista, no se abre sola otra vez ese día", async () => {
    await mount();
    await openPanel();
    assert.equal($(".o_buddy_tasks_head").getAttribute("aria-expanded"), "true");
    $(".o_buddy_tasks_head").click();
    await flush();
    assert.equal(localStorage.getItem("buddy_ia.tasks_closed"), new Date().toISOString().slice(0, 10));
    destroyApps();
    document.body.innerHTML = "";
    await mount();
    await openPanel();
    assert.equal($(".o_buddy_tasks_head").getAttribute("aria-expanded"), "false", "cerrada a propósito: hoy no se abre sola");
});

test("preguntarle al buddy en un paso de tipo «preguntar» lo tilda solo", async () => {
    const ask = task({ id: 91, title: "Preguntar", kind: "ask", question: "¿Cómo pido vacaciones?" });
    const updated = { ok: true, journeys: [journey({ tasks: [{ ...ask, done: true, state: "done" }], pending: 0 })], pending: 0 };
    await mount({ "/buddy/journey/step": () => updated, "/buddy/chat": () => ({ answer: "Pedilas en Odoo.", expression: "happy", ring: "ok", sources: [], ticket: null, contact: null, provider: "llm" }) },
        { ok: true, journeys: [journey({ tasks: [ask], pending: 1 })], pending: 1 });
    await openPanel();
    $(".o_buddy_task_ask").click();
    await flush(200);
    assert.deepEqual(calls("/buddy/journey/step")[0].params, { id: 91, done: true });
    assert.equal($(".o_buddy_dot"), null);
});

test("el aviso de error de la lista queda fuera del área que se desplaza y el botón explica por qué está apagado", async () => {
    await mount({ "/buddy/config": () => ({ first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: true, configured: false }),
        "/buddy/journey/step": () => ({ __status: 429, ok: false, message: "Estás tocando muy seguido." }) },
        { ok: true, journeys: [journey({ tasks: [task({ id: 95, question: "¿Algo?" }), task({ id: 96, title: "Otra" })], pending: 2 })], pending: 2 });
    await openPanel();
    const ask = $(".o_buddy_task_ask");
    assert.equal(ask.disabled, true);
    assert.equal(ask.getAttribute("title"), "Buddy todavía no está configurado");
    const box = $$(".o_buddy_task_check input")[0];
    box.checked = true;
    box.dispatchEvent(new window.Event("change", { bubbles: true }));
    await flush();
    const alert = $(".o_buddy_tasks [role=alert]");
    assert.match(alert.textContent, /muy seguido/);
    assert.equal(alert.closest(".o_buddy_tasks_body"), null, "el aviso no queda adentro del área con scroll");
});

test("si la pregunta del paso falla (error del buddy), el paso NO se tilda", async () => {
    const ask = task({ id: 97, title: "Preguntar", kind: "ask", question: "¿Algo?" });
    await mount({ "/buddy/chat": () => ({ error: "daily_limit", answer: "Llegué al máximo de consultas por hoy." }) },
        { ok: true, journeys: [journey({ tasks: [ask], pending: 1 })], pending: 1 });
    await openPanel();
    $(".o_buddy_task_ask").click();
    await flush(250);
    assert.equal(calls("/buddy/journey/step").length, 0, "sin respuesta no hay tilde");
    assert.match($$(".is-buddy").at(-1).textContent, /máximo de consultas/);
});
