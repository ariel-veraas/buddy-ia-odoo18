// Buddys (Kitsune, Mochi), accesorios, lista blanca de valores del servidor, avatar y widgets de Ajustes.
import { test, before, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountBuddy, mountComponent, flush, destroyApps } from "./helpers.js";

const ADDON = "../../odoo-addons/buddy_ia/static/src/js/";

before(() => setupDom());
afterEach(() => destroyApps());
beforeEach(() => {
    document.body.innerHTML = "";
    sessionStorage.clear();
    localStorage.clear();
});

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// OWL re-renders on animation frames (jsdom's own timers, not the mocked ones): two frames are enough.
const frames = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));

async function avatar(props) {
    const { BuddyAvatar } = await import(ADDON + "buddy_avatar.js");
    const { target } = await mountComponent(BuddyAvatar, props);
    return target;
}

// ------------------------------------------------------------------ lista blanca (registro de JS)
test("cleanLook: sin datos o con datos raros da Mochi, sin accesorio, gestos activados, frecuencia normal y todos los grupos", async () => {
    const { cleanLook, GESTURE_GROUPS } = await import(ADDON + "buddy_registry.js");
    const defaults = { buddy: "mochi", accessory: "none", idle: { enabled: true, frequency: "normal", groups: [...GESTURE_GROUPS] } };
    for (const cfg of [undefined, null, {}, "x", 5, [], { idle: null }, { idle: "x" }, { character: 5, accessory: [], idle: 5 }]) {
        assert.deepEqual(cleanLook(cfg), defaults, JSON.stringify(cfg));
    }
});

test("cleanLook solo acepta ids conocidos: HTML, estilos, prototipos y mayúsculas vuelven al valor por defecto", async () => {
    const { cleanLook } = await import(ADDON + "buddy_registry.js");
    const evil = ['<img src=x onerror=alert(1)>', "pi; background:url(x)", "__proto__", "constructor", "toString", "Pi", " pi", ""];
    for (const value of evil) {
        const look = cleanLook({ character: value, accessory: value, idle: { frequency: value, groups: [value, "sleep", {}, 3] } });
        assert.equal(look.buddy, "mochi", value);
        assert.equal(look.accessory, "none", value);
        assert.equal(look.idle.frequency, "normal", value);
        assert.deepEqual(look.idle.groups, ["sleep"], value);
    }
    const ok = cleanLook({ character: "mochi", accessory: "headphones", idle: { enabled: false, frequency: "high", groups: ["accessories", "accessories"] } });
    assert.deepEqual(ok, { buddy: "mochi", accessory: "headphones", idle: { enabled: false, frequency: "high", groups: ["accessories"] } });
});

test("la lista de buddys, accesorios y grupos de JS coincide con la de Python (un solo contrato)", async () => {
    const { readFileSync } = await import("node:fs");
    const { BUDDIES, ACCESSORIES, GESTURE_GROUPS, IDLE_FREQUENCIES, GESTURES } = await import(ADDON + "buddy_registry.js");
    const python = readFileSync(new URL("../../odoo-addons/buddy_ia/models/buddy_look.py", import.meta.url), "utf-8");
    const idsOf = (name) => {
        const block = python.match(new RegExp(`^${name} = [\\(\\[]([\\s\\S]*?)^[\\)\\]]`, "m"))[1];
        return [...block.matchAll(/\("([a-z_]+)",/g)].map((m) => m[1]);
    };
    assert.deepEqual(Object.keys(BUDDIES), idsOf("CHARACTERS"));
    assert.deepEqual(Object.keys(ACCESSORIES), idsOf("ACCESSORIES"));
    assert.deepEqual(GESTURE_GROUPS, idsOf("GESTURE_GROUPS"));
    assert.deepEqual(Object.keys(IDLE_FREQUENCIES), idsOf("FREQUENCIES"));
    for (const [id, gesture] of Object.entries(GESTURES)) {
        assert.ok(GESTURE_GROUPS.includes(gesture.group), id + " tiene un grupo que existe");
        assert.ok(gesture.ms >= 1000 && gesture.weight > 0, id);
    }
});

test("availableGestures: respeta grupos, variantes de cada buddy y no repite el lugar del accesorio fijo", async () => {
    const { availableGestures, GESTURE_GROUPS } = await import(ADDON + "buddy_registry.js");
    const all = (buddy, accessory = "none", groups = GESTURE_GROUPS) => availableGestures({ buddy, accessory, groups });
    assert.ok(all("kitsune").includes("tail") && all("kitsune").includes("ears"));
    assert.ok(!all("mochi").includes("tail") && !all("kitsune").includes("jiggle"));
    assert.ok(all("mochi").includes("bounce") && !all("kitsune").includes("bounce"));
    assert.deepEqual(all("mochi", "none", []), [], "sin grupos permitidos no hay gestos");
    assert.deepEqual(all("mochi", "none", ["sleep"]).sort(), ["sleep", "stretch", "yawn"]);
    assert.deepEqual(all("mochi", "none", ["accessories"]).sort(), ["mustache", "shades", "wizard"]);
    // Con el sombrero fijo no se pone otro sombrero; con lentes fijos no se pone lentes.
    assert.deepEqual(all("mochi", "wizard", ["accessories"]).sort(), ["mustache", "shades"]);
    assert.deepEqual(all("mochi", "party", ["accessories"]).sort(), ["mustache", "shades"]);
    assert.deepEqual(all("mochi", "headphones", ["accessories"]).sort(), ["mustache", "shades"]);
    assert.deepEqual(all("mochi", "shades", ["accessories"]).sort(), ["mustache", "wizard"]);
    assert.deepEqual(all("mochi", "mustache", ["accessories"]).sort(), ["shades", "wizard"]);
});

test("pickGesture: solo devuelve gestos del registro, con pesos, sin repetir el último y null si no hay", async () => {
    const { pickGesture, availableGestures, GESTURES, GESTURE_GROUPS } = await import(ADDON + "buddy_registry.js");
    const groups = [...GESTURE_GROUPS];
    for (const buddy of ["kitsune", "mochi", "evil", undefined]) {
        for (let i = 0; i < 60; i++) {
            const id = pickGesture({ buddy, accessory: "none", groups, random: Math.random });
            assert.ok(Object.hasOwn(GESTURES, id));
            assert.ok(availableGestures({ buddy: ["kitsune", "mochi"].includes(buddy) ? buddy : "mochi", accessory: "none", groups }).includes(id));
        }
    }
    const ids = availableGestures({ buddy: "mochi", accessory: "none", groups });
    assert.equal(pickGesture({ buddy: "mochi", accessory: "none", groups, random: () => 0 }), ids[0], "azar 0 = el primero");
    assert.equal(pickGesture({ buddy: "mochi", accessory: "none", groups, random: () => 0.999999 }), ids.at(-1), "azar ~1 = el último");
    assert.equal(pickGesture({ buddy: "mochi", accessory: "none", groups, last: ids[0], random: () => 0 }), ids[1], "no repite el anterior");
    assert.equal(pickGesture({ buddy: "mochi", accessory: "none", groups: [] }), null);
    // Con un solo gesto posible, repetirlo es mejor que no hacer nada.
    const only = pickGesture({ buddy: "mochi", accessory: "wizard", groups: ["accessories"], last: "mustache", random: () => 0 });
    assert.ok(["mustache", "shades"].includes(only));
});

// ------------------------------------------------------------------ el avatar
test("cada buddy dibuja las 14 caras (las 6 del contrato con la IA y las de los gestos) con ojos y sin errores", async () => {
    const { BUDDIES, EXPRESSIONS, GESTURES } = await import(ADDON + "buddy_registry.js");
    for (const buddy of Object.keys(BUDDIES)) {
        const seen = new Map();
        for (const expression of EXPRESSIONS) {
            const target = await avatar({ buddy, expression, size: 100 });
            const svg = $("svg.o_buddy_avatar", target);
            assert.ok(svg && svg.classList.contains(`buddy-${buddy}`), `${buddy}/${expression}`);
            assert.ok($(".bd-eyes", svg), `${buddy}/${expression} tiene ojos`);
            seen.set(expression, $(".bd-look", svg).innerHTML + $(".bd-mouth", svg).innerHTML);
            target.remove();
        }
        assert.equal(new Set(seen.values()).size, 6, `${buddy}: las 6 expresiones se ven distintas`);
        const faces = [...new Set(Object.values(GESTURES).map((g) => g.face).filter(Boolean))];
        for (const face of faces) {
            const gesture = Object.keys(GESTURES).find((id) => GESTURES[id].face === face && (!GESTURES[id].buddies || GESTURES[id].buddies.includes(buddy)));
            if (!gesture) {
                continue;
            }
            const target = await avatar({ buddy, gesture, size: 100 });
            assert.ok($(".bd-eyes", target), `${buddy}/${gesture} dibuja la cara ${face}`);
            target.remove();
        }
    }
});

test("la IA no puede pedir una cara de gesto: una expresión fuera del contrato se dibuja como neutral", async () => {
    const neutral = await avatar({ buddy: "kitsune", expression: "neutral" });
    for (const expression of ["heart", "sleep", "dizzy", "<b>", "__proto__", ""]) {
        const target = await avatar({ buddy: "kitsune", expression });
        assert.equal($(".bd-look", target).innerHTML.replace(/bd-[a-z]+-\d+/g, "id"), $(".bd-look", neutral).innerHTML.replace(/bd-[a-z]+-\d+/g, "id"), expression);
        target.remove();
    }
});

test("Kitsune y Mochi no tienen redondel ni aro: un indicador de estado propio y el lienzo entero", async () => {
    const full = await avatar({ buddy: "mochi", size: 100 });
    assert.ok($(".bd-arm", full), "cuerpo entero con su brazo");
    const head = await avatar({ buddy: "mochi", head: true });
    assert.ok(!$(".bd-arm", head), "solo la cabeza: sin cuerpo");
    assert.equal($("svg", full).getAttribute("width"), "76", "el ancho sale del lienzo propio de cada buddy");
    for (const buddy of ["kitsune", "mochi"]) {
        const target = await avatar({ buddy, size: 100 });
        const svg = $("svg", target);
        assert.ok(!$(".bd-ring", svg), `${buddy}: sin aro circular`);
        assert.equal($$("circle", svg).filter((c) => c.getAttribute("r") >= 55 && c.getAttribute("fill") === "none").length, 0, `${buddy}: ningún círculo grande de fondo`);
        assert.ok($(".bd-status .bd-dot", svg), `${buddy}: indicador de estado`);
        assert.ok(svg.classList.contains("is-loose"), `${buddy}: personaje suelto`);
        target.remove();
    }
});

test("el aro de estado llega a las clases ring-*: idle, listening, thinking, ok y doubt; un valor raro es idle", async () => {
    for (const buddy of ["kitsune", "mochi"]) {
        for (const ring of ["idle", "listening", "thinking", "ok", "doubt"]) {
            const target = await avatar({ buddy, ring });
            assert.ok($("svg", target).classList.contains(`ring-${ring}`), `${buddy}/${ring}`);
            target.remove();
        }
        const odd = await avatar({ buddy, ring: "<b>" });
        assert.ok($("svg", odd).classList.contains("ring-idle"));
        assert.ok(!/<b>/.test(odd.innerHTML));
    }
});

test("un buddy desconocido o hostil se dibuja como Mochi; el servidor nunca elige una plantilla arbitraria", async () => {
    for (const buddy of ["dragon", "<script>", "__proto__", "pi/../x", ""]) {
        const target = await avatar({ buddy, accessory: buddy });
        const svg = $("svg", target);
        assert.ok(svg.classList.contains("buddy-mochi"), buddy);
        assert.equal($$(".bd-acc", svg).length, 0, "accesorio desconocido = ninguno");
        target.remove();
    }
});

test("accesorios: cada uno se dibuja en los tres buddys, con una forma distinta y sin tocar el resto", async () => {
    const { ACCESSORIES, BUDDIES } = await import(ADDON + "buddy_registry.js");
    for (const buddy of Object.keys(BUDDIES)) {
        const none = await avatar({ buddy, accessory: "none" });
        assert.equal($$(".bd-acc", none).length, 0);
        const shapes = new Set();
        for (const accessory of Object.keys(ACCESSORIES).filter((id) => id !== "none")) {
            const target = await avatar({ buddy, accessory });
            const acc = $$(".bd-acc", target);
            assert.equal(acc.length, 1, `${buddy}/${accessory}`);
            assert.ok(!acc[0].classList.contains("is-temp"), "el fijo no es temporal");
            assert.ok(acc[0].querySelector("path, rect"), `${buddy}/${accessory} tiene dibujo`);
            shapes.add(acc[0].innerHTML);
            assert.match(acc[0].parentElement.getAttribute("transform"), /^translate\([-\d. ]+\) (rotate\([-\d.]+\) )?scale\([-\d. ]+\)$/, "la posición sale del registro, nunca del servidor");
            target.remove();
        }
        assert.equal(shapes.size, 5, `${buddy}: 5 accesorios distintos`);
    }
});

test("accesorio temporal de un gesto: se suma al fijo si no ocupan el mismo lugar, y es temporal", async () => {
    const both = await avatar({ buddy: "kitsune", accessory: "headphones", gesture: "mustache" });
    const acc = $$(".bd-acc", both);
    assert.equal(acc.length, 2);
    assert.deepEqual(acc.map((el) => el.classList.contains("is-temp")), [false, true]);
    assert.ok($("svg", both).classList.contains("g-mustache"));
    const clash = await avatar({ buddy: "kitsune", accessory: "wizard", gesture: "wizard" });
    assert.equal($$(".bd-acc", clash).length, 1, "con el sombrero fijo no se pone otro encima");
    assert.ok(!$(".bd-acc.is-temp", clash));
});

test("un gesto se ve solo con el aro en reposo, solo si existe y solo si es de ese buddy", async () => {
    const idle = await avatar({ buddy: "mochi", gesture: "sparkle" });
    assert.ok($("svg", idle).classList.contains("g-sparkle") && $(".bd-fx-sparkles", idle));
    for (const ring of ["listening", "thinking", "ok", "doubt"]) {
        const target = await avatar({ buddy: "mochi", gesture: "sparkle", ring });
        assert.ok(![...$("svg", target).classList].some((c) => c.startsWith("g-")), ring);
        assert.equal($(".bd-fx", target), null, ring);
        target.remove();
    }
    for (const gesture of ["tail", "ears", "nope", "__proto__", "constructor", "<b>", ""]) {
        const target = await avatar({ buddy: "mochi", gesture });
        assert.ok(![...$("svg", target).classList].some((c) => c.startsWith("g-")), gesture);
        target.remove();
    }
    const tail = await avatar({ buddy: "kitsune", gesture: "tail" });
    assert.ok($("svg", tail).classList.contains("g-tail") && $(".bd-tail", tail));
});

test("cada gesto con efecto lo dibuja, sin texto y sin contenido de usuario", async () => {
    const { GESTURES } = await import(ADDON + "buddy_registry.js");
    const names = { sparkles: "sparkles", hearts: "hearts", sweat: "sweat", vein: "vein", question: "question", exclaim: "exclaim", zzz: "zzz", bubble: "bubble", stars: "stars" };
    for (const [id, gesture] of Object.entries(GESTURES)) {
        const buddy = gesture.buddies?.[0] || "mochi";
        const target = await avatar({ buddy, gesture: id, head: true });
        for (const fx of gesture.fx || []) {
            assert.ok($(`.bd-fx-${names[fx]}`, target), `${id} dibuja ${fx}`);
        }
        assert.equal($$("text", target).length, 0, `${id}: ningún <text> (los signos son dibujo, no texto)`);
        target.remove();
    }
});

test("el avatar usa ids únicos por instancia (cabecera y lanzador no se pisan)", async () => {
    const { BuddyAvatar } = await import(ADDON + "buddy_avatar.js");
    const a = await mountComponent(BuddyAvatar, { buddy: "kitsune" });
    const b = await mountComponent(BuddyAvatar, { buddy: "kitsune" });
    const idsOf = (target) => $$("[id]", target).map((el) => el.id);
    const first = idsOf(a.target);
    assert.ok(first.length >= 3);
    assert.equal(first.filter((id) => idsOf(b.target).includes(id)).length, 0);
});

// ------------------------------------------------------------------ el color principal tiñe a los buddys
test("themeVars deriva la paleta de Kitsune y Mochi del color principal, con acento cálido", async () => {
    const { themeVars, buddyPalette } = await import(ADDON + "buddy.js");
    const css = themeVars("#2f6df6");
    for (const name of ["--bd-primary", "--bd-fur", "--bd-fur-lt", "--bd-fur-dk", "--bd-warm", "--bd-warm-dk", "--bd-mochi", "--bd-mochi-dk"]) {
        assert.match(css, new RegExp(`${name}: #[0-9a-f]{6}`), name);
    }
    assert.match(css, /--bd-warm: #ffb457/, "azul: acento naranja");
    assert.match(buddyPalette([255, 140, 0]), /--bd-warm: #ff7aa0/, "naranja: el acento pasa a rosa para que se distinga");
    assert.match(buddyPalette([128, 128, 128]), /--bd-fur: #([0-9a-f]{2})\1\1;/, "gris: pelaje gris, sin tinte");
    assert.notEqual(themeVars("#2f6df6"), themeVars("#e03e3e"));
    assert.equal(themeVars("rojo"), "");
    assert.equal(themeVars("red; background:url(x)"), "");
});

// ------------------------------------------------------------------ el widget con cada buddy
const configWith = (extra) => () => ({
    first_name: "Ana", buddy_name: "Buddy", session_generation: 0, enabled: true, ...extra,
});

test("sin los campos nuevos (servidor viejo) el widget usa Mochi sin accesorio", async () => {
    fakeBackend({ "/buddy/config": configWith({}) });
    const { comp } = await mountBuddy();
    assert.equal(comp.state.character, "mochi");
    assert.equal(comp.state.accessory, "none");
    assert.equal($(".o_buddy_launcher svg").classList.contains("buddy-mochi"), true);
    assert.equal($(".o_buddy_launcher").classList.contains("is-loose"), true);
});

test("con Kitsune o Mochi el lanzador y la cabecera no llevan disco (is-loose) y se ven con su dibujo", async () => {
    for (const character of ["kitsune", "mochi"]) {
        document.body.innerHTML = "";
        destroyApps();
        fakeBackend({ "/buddy/config": configWith({ character, accessory: "party" }) });
        const { comp } = await mountBuddy();
        const launcher = $(".o_buddy_launcher");
        assert.ok(launcher.classList.contains("is-loose"), character);
        assert.ok($("svg.o_buddy_avatar", launcher).classList.contains(`buddy-${character}`));
        assert.equal($$(".bd-acc", launcher).length, 1, "el accesorio fijo llega al lanzador");
        assert.ok(!$(".bd-ring", launcher), "ni aro ni redondel");
        launcher.click();
        await flush();
        assert.ok($(".o_buddy_head_avatar").classList.contains("is-loose"));
        assert.ok($(".o_buddy_head_avatar svg").classList.contains(`buddy-${character}`));
        assert.equal(comp.looseBuddy, true);
    }
});

test("el globito de saludo usa el cuerpo entero del buddy elegido, saludando", async () => {
    fakeBackend({ "/buddy/config": configWith({ character: "kitsune" }) });
    await mountBuddy();
    await flush(2300);
    const bubble = $(".o_buddy_bubble svg");
    assert.ok(bubble.classList.contains("buddy-kitsune"));
    assert.ok($(".bd-arm.is-waving", bubble));
});

test("valores raros en /buddy/config no rompen nada: se usa Mochi", async () => {
    fakeBackend({ "/buddy/config": configWith({ character: "<img src=x onerror=1>", accessory: { a: 1 }, idle: "no" }) });
    const { comp } = await mountBuddy();
    assert.equal(comp.state.character, "mochi");
    assert.equal(comp.state.accessory, "none");
    assert.equal($("img"), null);
});

// ------------------------------------------------------------------ widgets de Ajustes (vista previa en vivo)
const fakeRecord = (data) => ({
    data: { buddy_character: "mochi", buddy_accessory: "none", buddy_primary_color: "", ...data },
    fields: {
        buddy_character: { selection: [["kitsune", "Kitsune"], ["mochi", "Mochi"], ["otro", "Otro módulo"]] },
        buddy_accessory: { selection: [["none", "Ninguno"], ["wizard", "Sombrero de mago"]] },
    },
    updates: [],
    update(changes) {
        this.updates.push(changes);
        Object.assign(this.data, changes);
    },
});

test("el selector de personaje muestra solo los buddys que el navegador sabe dibujar y marca el elegido", async () => {
    const { BuddyCharacterPicker } = await import(ADDON + "buddy_look.js");
    const record = fakeRecord({ buddy_character: "mochi", buddy_accessory: "wizard", buddy_primary_color: "#112233" });
    const { target } = await mountComponent(BuddyCharacterPicker, { name: "buddy_character", record });
    const cards = $$(".o_buddy_picker_card", target);
    assert.equal(cards.length, 2, "«Otro módulo» no se muestra");
    assert.deepEqual(cards.map((c) => c.getAttribute("aria-checked")), ["false", "true"]);
    assert.deepEqual($$(".o_buddy_picker_card svg", target).map((svg) => [...svg.classList].find((c) => c.startsWith("buddy-"))),
        ["buddy-kitsune", "buddy-mochi"]);
    assert.equal($$(".bd-acc", target).length, 2, "las dos tarjetas ya muestran el accesorio elegido (sin guardar)");
    assert.match($(".o_buddy_picker", target).getAttribute("style"), /--bd-primary: #112233/, "y el color elegido");
    assert.equal($(".o_buddy_picker", target).getAttribute("role"), "radiogroup");
});

test("elegir una tarjeta escribe el id en el formulario (sin guardar); un id raro del formulario se ve como Mochi", async () => {
    const { BuddyCharacterPicker } = await import(ADDON + "buddy_look.js");
    const record = fakeRecord({});
    const { target } = await mountComponent(BuddyCharacterPicker, { name: "buddy_character", record });
    $$(".o_buddy_picker_card", target)[0].click();
    assert.deepEqual(record.updates, [{ buddy_character: "kitsune" }]);
    const hostile = fakeRecord({ buddy_character: "<b>x</b>" });
    const other = await mountComponent(BuddyCharacterPicker, { name: "buddy_character", record: hostile });
    assert.equal($$(".o_buddy_picker_card", other.target)[1].getAttribute("aria-checked"), "true");
    const readonly = fakeRecord({});
    const frozen = await mountComponent(BuddyCharacterPicker, { name: "buddy_character", record: readonly, readonly: true });
    $$(".o_buddy_picker_card", frozen.target)[0].click();
    assert.deepEqual(readonly.updates, [], "solo lectura no escribe");
});

test("los botones de opciones (accesorio, frecuencia) leen la lista del servidor y escriben el valor", async () => {
    const { BuddyChips } = await import(ADDON + "buddy_look.js");
    const record = fakeRecord({ buddy_accessory: "wizard" });
    const { target } = await mountComponent(BuddyChips, { name: "buddy_accessory", record });
    const buttons = $$("button", target);
    assert.deepEqual(buttons.map((b) => b.textContent.trim()), ["Ninguno", "Sombrero de mago"]);
    assert.deepEqual(buttons.map((b) => b.getAttribute("aria-checked")), ["false", "true"]);
    buttons[0].click();
    assert.deepEqual(record.updates, [{ buddy_accessory: "none" }]);
});

test("la vista previa usa el mismo avatar, con el personaje, accesorio y color del formulario sin guardar", async () => {
    const { BuddyPreview } = await import(ADDON + "buddy_look.js");
    const record = fakeRecord({ buddy_character: "kitsune", buddy_accessory: "shades", buddy_primary_color: "#e03e3e" });
    const { target } = await mountComponent(BuddyPreview, { name: "buddy_character", record, readonly: true });
    const svg = $(".o_buddy_preview_stage svg", target);
    assert.ok(svg.classList.contains("buddy-kitsune"));
    assert.equal($$(".bd-acc", svg).length, 1);
    assert.match($(".o_buddy_preview_box", target).getAttribute("style"), /--bd-primary: #e03e3e/);
    // Los gestos que ofrece son los de Kitsune, y sin el de lentes de sol (ya los lleva puestos).
    const labels = $$(".o_buddy_try", target).map((b) => b.textContent.trim());
    assert.ok(labels.includes("Mueve la cola") && labels.includes("Se pone el sombrero de mago"));
    assert.ok(!labels.includes("Rebota") && !labels.includes("Se pone lentes de sol"));
});

test("en la vista previa se prueban las expresiones y los gestos; el gesto se corta solo y se limpia al cerrar", async () => {
    const { mock } = await import("node:test");
    const { BuddyPreview } = await import(ADDON + "buddy_look.js");
    const { GESTURES } = await import(ADDON + "buddy_registry.js");
    const record = fakeRecord({ buddy_character: "kitsune" });
    const { target, app } = await mountComponent(BuddyPreview, { name: "buddy_character", record, readonly: true });
    const stage = () => $(".o_buddy_preview_stage svg", target);
    const expressions = $$(".o_buddy_preview_group", target)[0];
    assert.equal($$("button", expressions).length, 7, "6 expresiones del contrato + saludar");
    mock.timers.enable({ apis: ["setTimeout"] });
    try {
        $$("button", expressions)[1].click();
        await frames();
        assert.ok($(".bd-mouth", stage()), "la expresión «Contento» se dibuja");
        $$(".o_buddy_try", target).find((b) => b.textContent.trim() === "Mueve la cola").click();
        mock.timers.tick(40);
        await frames();
        assert.ok(stage().classList.contains("g-tail"));
        mock.timers.tick(GESTURES.tail.ms);
        await frames();
        assert.ok(![...stage().classList].some((c) => c.startsWith("g-")), "el gesto terminó solo");
        $$(".o_buddy_try", target)[0].click();
        app.destroy();
        mock.timers.tick(10000);
    } finally {
        mock.timers.reset();
    }
});

test("los widgets de Ajustes están registrados como campos de selección", async () => {
    await import(ADDON + "buddy_look.js");
    const { registry } = await import("@web/core/registry");
    const fields = registry.category("fields");
    for (const name of ["buddy_character_picker", "buddy_chips", "buddy_preview"]) {
        assert.ok(fields.get(name), name);
        assert.deepEqual(fields.get(name).supportedTypes, ["selection"]);
    }
});
