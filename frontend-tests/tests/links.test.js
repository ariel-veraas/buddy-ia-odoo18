import { test, before, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { setupDom, fakeBackend, mountBuddy, flush, destroyApps } from "./helpers.js";

before(() => setupDom());
afterEach(() => destroyApps());
beforeEach(() => {
    document.body.innerHTML = "";
    sessionStorage.clear();
    localStorage.clear();
});

const $$ = (sel) => [...document.querySelectorAll(sel)];

const GOOD = [
    "https://docs.google.com/document/d/abc/edit",
    "https://drive.google.com/file/d/abc/view?usp=sharing#x",
    "https://docs.google.com/",
];
const BAD = [
    "https://docs.google.com\\@evil.example/",
    "https://docs.google.com/a\\b",
    "https://docs.google.com\\evil.example/",
    "https://docs.google.com/x y",
    "https://docs.google.com/x\n",
    "https://docs.google.com/\u0000",
    "https://docs.google.com:443/x",
    "https://user@docs.google.com/x",
    "https://docs.google.com",
    "https://docs.google.com.evil.example/x",
    "https://docs.google.com@evil.example/x",
    "http://docs.google.com/x",
    "javascript:alert(1)",
    "//docs.google.com/x",
    "data:text/html,<script>alert(1)</script>",
    "",
    null,
    undefined,
    7,
    { toString: () => "https://docs.google.com/x" },
    ["https://docs.google.com/x"],
];

test("safeDriveLink deja pasar solo https de docs/drive.google.com sin barra invertida ni espacios", async () => {
    const { safeDriveLink } = await import("../../odoo-addons/buddy_ia/static/src/js/buddy.js");
    for (const link of GOOD) {
        assert.equal(safeDriveLink(link), link, link);
    }
    for (const link of BAD) {
        assert.equal(safeDriveLink(link), "", String(link));
    }
});

test("una fuente con un enlace peligroso se muestra como texto, sin href", async () => {
    fakeBackend({
        "/buddy/history": () => ({
            session_generation: 2,
            messages: [
                { role: "user", text: "¿Dónde está la guía?", date: "2026-01-01 10:00:00" },
                {
                    role: "assistant", text: "Está en la carpeta.", date: "2026-01-01 10:00:01", expression: "happy", ring: "ok",
                    sources: [
                        { title: "Buena", owner: "RRHH", updated: "2026-01-01", link: GOOD[0] },
                        { title: "Con barra", owner: "RRHH", updated: "", link: "https://docs.google.com\\@evil.example/" },
                        { title: "Otro host", owner: "RRHH", updated: "", link: "https://evil.example/docs.google.com" },
                        { title: "Script", owner: "RRHH", updated: "", link: "javascript:alert(1)" },
                    ],
                },
            ],
        }),
    });
    await mountBuddy();
    document.querySelector(".o_buddy_launcher").click();
    await flush();
    const anchors = $$(".o_buddy_source a");
    assert.equal(anchors.length, 1, "solo la fuente con enlace válido es un enlace");
    assert.equal(anchors[0].getAttribute("href"), GOOD[0]);
    assert.equal($$(".o_buddy_source").length, 4, "las otras tres se ven igual, como texto");
    assert.doesNotMatch(document.body.innerHTML, /evil\.example\/"|javascript:/);
});
