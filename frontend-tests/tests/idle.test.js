// Gestos en reposo: el programador (con relojes falsos), cuándo NO dispara, y la integración con el widget.
import { test, before, beforeEach, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountComponent, destroyApps } from "./helpers.js";

const ADDON = "../../odoo-addons/buddy_ia/static/src/js/";

before(() => setupDom());
beforeEach(() => {
    document.body.innerHTML = "";
    sessionStorage.clear();
    localStorage.clear();
    mock.timers.enable({ apis: ["setTimeout", "Date"] });
});
afterEach(() => {
    destroyApps();
    mock.timers.reset();
    delete window.matchMedia;
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
});

const $ = (sel) => document.querySelector(sel);
const S = 1000;
// OWL re-renders on animation frames, which belong to jsdom (real time), not to the mocked setTimeout.
const frames = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
const tick = async (ms) => {
    mock.timers.tick(ms);
    await Promise.resolve();
};

async function scheduler(overrides = {}) {
    const { IdleScheduler } = await import(ADDON + "buddy_idle.js");
    const log = [];
    const options = {
        config: () => ({ frequency: "normal" }),
        canPlay: () => true,
        quietSeconds: () => 30,
        pick: () => "blink",
        onGesture: (id) => log.push(["start", id, Date.now()]),
        onEnd: () => log.push(["end", Date.now()]),
        random: () => 0,
        ...overrides,
    };
    const idle = new IdleScheduler(options);
    return { idle, log };
}

// ------------------------------------------------------------------ el programador
test("las frecuencias están documentadas: Poco 60 s y 2 a 4 min, Normal 30 s y 45 a 90 s, Mucho 12 s y 15 a 40 s", async () => {
    const { IDLE_FREQUENCIES, OPEN_QUIET_SECONDS } = await import(ADDON + "buddy_registry.js");
    assert.deepEqual(IDLE_FREQUENCIES, {
        low: { idle: 60, gap: [120, 240] },
        normal: { idle: 30, gap: [45, 90] },
        high: { idle: 12, gap: [15, 40] },
    });
    assert.equal(OPEN_QUIET_SECONDS, 60);
});

test("espera los segundos de quietud, hace un gesto, lo termina y espera el intervalo siguiente", async () => {
    const { idle, log } = await scheduler();
    const { GESTURES } = await import(ADDON + "buddy_registry.js");
    idle.start();
    await tick(29 * S + 999);
    assert.deepEqual(log, [], "antes de los 30 s no hay gesto");
    await tick(1);
    assert.deepEqual(log.map((e) => e[0]), ["start"]);
    assert.equal(idle.current, "blink");
    await tick(GESTURES.blink.ms - 1);
    assert.equal(log.length, 1, "el gesto sigue en curso");
    await tick(1);
    assert.deepEqual(log.map((e) => e[0]), ["start", "end"]);
    assert.equal(idle.current, null);
    // Azar 0: el intervalo es el mínimo (45 s).
    await tick(45 * S - 1);
    assert.equal(log.length, 2);
    await tick(1);
    assert.equal(log.length, 3, "el próximo gesto llega a los 45 s");
});

test("el intervalo tiene azar: siempre entre el mínimo y el máximo de la frecuencia elegida", async () => {
    const { IDLE_FREQUENCIES, GESTURES } = await import(ADDON + "buddy_registry.js");
    for (const [frequency, { idle: wait, gap }] of Object.entries(IDLE_FREQUENCIES)) {
        for (const roll of [0, 0.5, 0.999999]) {
            const { idle, log } = await scheduler({ config: () => ({ frequency }), quietSeconds: () => wait, random: () => roll });
            idle.start();
            await tick(wait * S * 1.25 + 1);
            assert.equal(log.filter((e) => e[0] === "start").length, 1, `${frequency}: el primero llega a lo sumo a +25%`);
            await tick(GESTURES.blink.ms);
            const endedAt = log.find((e) => e[0] === "end")[2 - 1];
            let next = null;
            for (let ms = 0; ms <= gap[1] * S + 1 && next === null; ms += 500) {
                if (log.filter((e) => e[0] === "start").length === 2) {
                    next = ms;
                } else {
                    await tick(500);
                }
            }
            assert.notEqual(next, null, `${frequency}/${roll}: hubo un segundo gesto`);
            const waited = (log.filter((e) => e[0] === "start")[1][2] - endedAt) / S;
            assert.ok(waited >= gap[0] - 0.5 && waited <= gap[1] + 0.5, `${frequency}/${roll}: esperó ${waited} s, rango ${gap}`);
            idle.stop();
        }
    }
});

test("si alguien usa el buddy (touch) el gesto espera: nunca aparece sin quietud", async () => {
    const { idle, log } = await scheduler();
    idle.start();
    await tick(25 * S);
    idle.touch();
    await tick(10 * S);
    assert.deepEqual(log, [], "a los 35 s no pasaron 30 s de quietud desde el touch");
    await tick(20 * S + 500);
    assert.deepEqual(log.map((e) => e[0]), ["start"], "a los 30 s de quietud sí");
});

test("si no está permitido actuar (chat ocupado, etc.) se salta el turno sin gesto y se reintenta en el próximo intervalo", async () => {
    let allowed = false;
    const { idle, log } = await scheduler({ canPlay: () => allowed });
    idle.start();
    await tick(30 * S);
    assert.deepEqual(log, []);
    await tick(44 * S);
    assert.deepEqual(log, [], "dentro del intervalo no vuelve a intentar");
    allowed = true;
    await tick(1 * S + 1);
    assert.deepEqual(log.map((e) => e[0]), ["start"]);
});

test("sin gestos disponibles (pick devuelve null o algo inventado) no hace nada y sigue esperando", async () => {
    for (const pick of [() => null, () => "inventado", () => "__proto__", () => ({})]) {
        const { idle, log } = await scheduler({ pick });
        idle.start();
        await tick(10 * 60 * S);
        assert.deepEqual(log, []);
        idle.stop();
    }
});

test("pause apaga todo (también un gesto en curso) y resume vuelve a esperar desde cero", async () => {
    const { idle, log } = await scheduler();
    idle.start();
    await tick(30 * S);
    assert.equal(idle.current, "blink");
    idle.pause();
    assert.deepEqual(log.map((e) => e[0]), ["start", "end"], "el gesto en curso se corta");
    await tick(60 * 60 * S);
    assert.equal(log.length, 2, "en pausa no pasa nada, ni en una hora");
    idle.resume();
    await tick(29 * S);
    assert.equal(log.length, 2);
    await tick(1 * S);
    assert.equal(log.length, 3);
});

test("interrupt corta el gesto al instante y vuelve a esperar la quietud completa", async () => {
    const { idle, log } = await scheduler();
    idle.start();
    await tick(30 * S);
    assert.equal(idle.current, "blink");
    idle.interrupt();
    assert.deepEqual(log.map((e) => e[0]), ["start", "end"]);
    assert.equal(idle.current, null);
    await tick(29 * S);
    assert.equal(log.length, 2);
    await tick(1 * S + 1);
    assert.equal(log.length, 3);
    // Sin gesto en curso, interrupt no inventa un final.
    const quiet = await scheduler();
    quiet.idle.start();
    quiet.idle.interrupt();
    assert.deepEqual(quiet.log, []);
});

test("stop limpia los temporizadores: después no ocurre nada, ni siquiera un gesto en curso", async () => {
    const { idle, log } = await scheduler();
    idle.start();
    await tick(31 * S);
    idle.stop();
    const before = log.length;
    mock.timers.runAll();
    await tick(24 * 60 * 60 * S);
    assert.equal(log.length, before);
    assert.equal(idle._wait, null);
    assert.equal(idle._end, null);
    idle.interrupt();
    idle.touch();
    await tick(60 * S);
    assert.equal(log.length, before, "parado no se despierta con nada");
});

test("hay UN solo temporizador de espera a la vez (start repetido no los acumula)", async () => {
    const { idle, log } = await scheduler();
    idle.start();
    idle.start();
    idle.resume();
    idle.reset();
    await tick(30 * S);
    assert.equal(log.filter((e) => e[0] === "start").length, 1);
});

test("con el chat abierto se exige más quietud que con el chat cerrado", async () => {
    const { quietSecondsFor } = await import(ADDON + "buddy_idle.js");
    assert.equal(quietSecondsFor({ open: false, frequency: "normal" }), 30);
    assert.equal(quietSecondsFor({ open: true, frequency: "normal" }), 60);
    assert.equal(quietSecondsFor({ open: true, frequency: "high" }), 60);
    assert.equal(quietSecondsFor({ open: true, frequency: "low" }), 60);
    assert.equal(quietSecondsFor({ open: false, frequency: "<b>" }), 30, "frecuencia rara = normal");
});

// ------------------------------------------------------------------ integración con el widget
const configWith = (extra = {}) => () => ({
    first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: true,
    idle: { enabled: true, frequency: "high", groups: ["expressions", "accessories", "sleep"] }, ...extra,
});

async function widget(config = configWith(), prepare = () => {}, overrides = {}) {
    fakeBackend({ "/buddy/config": config, ...overrides });
    // Already opened once: no greeting bubble in the way (a visible greeting blocks gestures on purpose, tested below).
    localStorage.setItem("buddy_ia.opened", "1");
    prepare();
    const { Buddy } = await import(ADDON + "buddy.js");
    const mounted = await mountComponent(Buddy);
    for (let i = 0; i < 200 && !mounted.comp.state.ready; i++) {
        await frames();
    }
    await frames();
    return mounted;
}

const gestureClass = () => [...$(".o_buddy_launcher svg").classList].find((c) => c.startsWith("g-")) || null;

test("el widget hace un gesto tras la quietud (Mucho: 12 a 15 s) y lo termina solo", async () => {
    const { comp } = await widget();
    await tick(11 * S);
    assert.equal(comp.state.gesture, null);
    await tick(4 * S);
    await frames();
    assert.ok(comp.state.gesture, "ya hay un gesto");
    assert.equal(gestureClass(), "g-" + comp.state.gesture);
    const { GESTURES } = await import(ADDON + "buddy_registry.js");
    await tick(GESTURES[comp.state.gesture].ms);
    await frames();
    assert.equal(comp.state.gesture, null);
    assert.equal(gestureClass(), null);
});

test("el estado «pensando/respondiendo» del chat corta el gesto en curso y manda sobre él", async () => {
    // The answer never arrives: the buddy stays "thinking".
    const { comp } = await widget(configWith(), () => {}, { "/buddy/chat": () => new Promise(() => {}) });
    await tick(15 * S);
    await frames();
    assert.ok(comp.state.gesture);
    comp.state.input = "hola";
    comp.send();
    assert.equal(comp.state.gesture, null, "se cortó en el mismo instante");
    await frames();
    assert.equal(gestureClass(), null);
    assert.ok($(".o_buddy_launcher svg").classList.contains("ring-thinking"));
    // Aunque algo intentara poner un gesto mientras piensa, el avatar no lo muestra (aro ≠ reposo).
    comp.state.gesture = "sparkle";
    await frames();
    assert.equal(gestureClass(), null);
    assert.equal($(".o_buddy_launcher .bd-fx"), null);
});

test("no hace gestos mientras el chat está ocupado, borrando, confirmando o con el saludo en pantalla", async () => {
    const { comp } = await widget();
    for (const busy of [{ loading: true }, { streaming: true }, { forgetting: true }, { confirmForget: true }, { bubble: "¡Hola!" }, { ring: "thinking" }, { ring: "listening" }]) {
        Object.assign(comp.state, busy);
        assert.equal(comp.canPlayGesture(), false, JSON.stringify(busy));
        Object.assign(comp.state, { loading: false, streaming: false, forgetting: false, confirmForget: false, bubble: null, ring: "idle" });
    }
    assert.equal(comp.canPlayGesture(), true);
    comp.state.loading = true;
    await tick(10 * 60 * S);
    assert.equal(comp.state.gesture, null, "ocupado, ni en 10 minutos");
});

test("con el chat abierto y en uso no dispara; abierto y sin uso por más de un minuto, sí", async () => {
    const { comp } = await widget();
    comp.toggle();
    await frames();
    await tick(59 * S);
    assert.equal(comp.state.gesture, null, "recién abierto: 59 s sin gestos");
    comp.onActivity();
    await tick(59 * S);
    assert.equal(comp.state.gesture, null, "usarlo (mouse, teclado) reinicia la espera");
    await tick(2 * S);
    assert.ok(comp.state.gesture, "más de un minuto sin uso: puede hacer un gesto");
});

test("el mouse sobre el buddy lo mantiene quieto (pointermove en el widget cuenta como uso)", async () => {
    const { comp } = await widget();
    for (let i = 0; i < 12; i++) {
        $(".o_buddy").dispatchEvent(new window.Event("pointermove", { bubbles: true }));
        await tick(5 * S);
    }
    assert.equal(comp.state.gesture, null, "un minuto con el mouse encima: ningún gesto");
    await tick(13 * S);
    assert.ok(comp.state.gesture);
});

test("con la pestaña oculta no hay temporizadores ni gestos; al volver reanuda", async () => {
    const { comp } = await widget();
    await tick(8 * S);
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new window.Event("visibilitychange"));
    assert.equal(comp.idleScheduler.paused, true);
    assert.equal(comp.idleScheduler._wait, null, "ni un temporizador vivo");
    await tick(30 * 60 * S);
    assert.equal(comp.state.gesture, null);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new window.Event("visibilitychange"));
    await tick(11 * S);
    assert.equal(comp.state.gesture, null, "vuelve a esperar la quietud completa");
    await tick(4 * S);
    assert.ok(comp.state.gesture);
});

test("con «reducir movimiento» (prefers-reduced-motion) no hay gestos nunca; si cambia la preferencia, se adapta", async () => {
    const listeners = [];
    const query = { matches: true, addEventListener: (_, fn) => listeners.push(fn), removeEventListener: () => {} };
    const { comp } = await widget(configWith(), () => {
        window.matchMedia = () => query;
    });
    assert.equal(comp.idleScheduler.paused, true);
    await tick(60 * 60 * S);
    assert.equal(comp.state.gesture, null);
    assert.equal(comp.canPlayGesture(), false);
    query.matches = false;
    listeners.forEach((fn) => fn());
    await tick(16 * S);
    assert.ok(comp.state.gesture, "sin la preferencia, vuelve a actuar");
    query.matches = true;
    listeners.forEach((fn) => fn());
    assert.equal(comp.state.gesture, null, "y si la activan, se corta en el acto");
});

test("el administrador puede apagar los gestos o dejar sin grupos: no hay ningún gesto", async () => {
    for (const idle of [{ enabled: false, frequency: "high", groups: ["expressions"] }, { enabled: true, frequency: "high", groups: [] }]) {
        document.body.innerHTML = "";
        destroyApps();
        const { comp } = await widget(configWith({ idle }));
        await tick(60 * 60 * S);
        assert.equal(comp.state.gesture, null, JSON.stringify(idle));
    }
});

test("solo hace los gestos de los grupos permitidos y de su personaje", async () => {
    const { GESTURES } = await import(ADDON + "buddy_registry.js");
    const { comp } = await widget(configWith({ character: "kitsune", idle: { enabled: true, frequency: "high", groups: ["sleep"] } }));
    const seen = new Set();
    const original = comp.idleScheduler.onGesture;
    comp.idleScheduler.onGesture = (id) => {
        seen.add(id);
        original(id);
    };
    for (let i = 0; i < 3 * 60 * 60; i++) {
        await tick(1 * S);
    }
    assert.ok(seen.size >= 2, "salen varios: " + [...seen]);
    for (const id of seen) {
        assert.equal(GESTURES[id].group, "sleep", id);
    }
});

test("si la persona oculta al buddy los gestos se apagan, y vuelven al mostrarlo", async () => {
    const { comp } = await widget();
    await tick(15 * S);
    assert.ok(comp.state.gesture);
    comp.hide();
    assert.equal(comp.state.gesture, null);
    assert.equal(comp.idleScheduler.paused, true);
    await tick(60 * 60 * S);
    assert.equal(comp.state.gesture, null);
    comp.show();
    assert.equal(comp.idleScheduler.paused, false);
});

test("al desmontar el widget se limpian todos los temporizadores y no queda ningún gesto programado", async () => {
    const { comp, app } = await widget();
    await tick(20 * S);
    app.destroy();
    assert.equal(comp.idleScheduler.paused, true);
    assert.equal(comp.idleScheduler._wait, null);
    assert.equal(comp.idleScheduler._end, null);
    const before = comp.state.gesture;
    mock.timers.runAll();
    await tick(24 * 60 * 60 * S);
    assert.equal(comp.state.gesture, before === null ? null : null, "ya no escribe el estado de un widget destruido");
    assert.equal(comp.idleScheduler._wait, null);
});

test("los gestos no tienen texto ni contenido de usuario: el avatar solo dibuja formas", async () => {
    const { comp } = await widget(configWith({ buddy_name: "<b>Yo</b>", first_name: "<i>Ana</i>" }));
    comp.state.messages.push({ id: 99, role: "user", text: "MI-SECRETO" });
    for (const id of ["question", "exclaim", "sleep", "sweat", "heart"]) {
        comp.state.gesture = id;
        await frames();
        const svg = $(".o_buddy_launcher svg");
        assert.equal(svg.querySelectorAll("text").length, 0, id);
        assert.ok(!svg.textContent.includes("MI-SECRETO") && !svg.textContent.includes("Ana"), id);
    }
});
