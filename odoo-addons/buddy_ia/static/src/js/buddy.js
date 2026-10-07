/** @odoo-module **/

import { Component, useEffect, useRef, useState, onMounted, onPatched, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { BuddyAvatar } from "./buddy_avatar";
import { BUDDIES, cleanLook, nudgeGesture, pickGesture } from "./buddy_registry";
import { IdleScheduler, quietSecondsFor } from "./buddy_idle";
import { hasLateTask, lateText, loadJourneys, nudgeText, setStepDone, summaryText, untilText } from "./buddy_journey";
import { loadSuggestions, pickChips } from "./buddy_suggestions";

// The floating greeting appears at most once a day (a date, kept in this browser) and never again once the person has
// opened the chat at least once: it must not come back with every new tab.
const GREETED_KEY = "buddy_ia.greeted";
const OPENED_KEY = "buddy_ia.opened";
const todayKey = () => new Date().toISOString().slice(0, 10);
const HIDDEN_KEY = "buddy_ia.hidden";
// The day (a date) the person closed «Tus tareas de hoy»: it does not open by itself again that day.
const TASKS_CLOSED_KEY = "buddy_ia.tasks_closed";
// The small bubble that tells about the tasks of the day (E4b): at most once a day (a date in the person's own time zone, kept in this
// browser) and it goes away by itself after a few seconds. The rotation of the suggestion chips lives in the tab (sessionStorage).
const NUDGED_KEY = "buddy_ia.nudged";
const CHIP_ROTATION_KEY = "buddy_ia.chip_rotation";
const NUDGE_SECONDS = 15;
// The bubble waits while the person points at it or has the keyboard on it, and goes away this long after they leave it.
const NUDGE_RELEASE_SECONDS = 6;
// It does not come while the person is busy anywhere in the page (typing in a form, the chatter, a search box): not only in the buddy.
const PAGE_QUIET_MS = 20 * 1000;
const SUGGESTIONS_MAX_AGE = 30 * 60 * 1000;
function localDayKey() {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}
const HEX_COLOR = /^#[0-9a-f]{6}$/i;
// Errors worth offering a "Retry" button for (the user's question is kept and re-sent). Not "daily_limit" or
// "system_limit": the daily ceilings do not go away in a few seconds, so a button would only invite useless clicks.
const RETRYABLE = new Set(["provider_error", "provider_timeout", "busy", "request_pending", "invalid_response", "rate_limited"]);
// Feedback (👍/👎) and confidence of an answer: whatever the server sends is filtered here before it reaches the screen.
const FEEDBACK_TOKEN = /^[A-Za-z0-9_-]{16,64}$/;
const CONFIDENCE_LEVELS = new Set(["high", "medium", "low"]);

/** The opaque code that lets the person rate an answer, or null (greetings, errors and the welcome have none). */
export function cleanFeedbackToken(value) {
    return typeof value === "string" && FEEDBACK_TOKEN.test(value) ? value : null;
}

export function cleanVote(value) {
    return value === "up" || value === "down" ? value : null;
}

export function cleanConfidence(value) {
    return CONFIDENCE_LEVELS.has(value) ? value : null;
}

const FEEDBACK_REASON_IDS = ["not_asked", "wrong", "incomplete", "other"];
// What a message carries about its rating and its confidence note (reset every time a message is built or replaced).
const FEEDBACK_DEFAULTS = {
    feedbackToken: null,
    feedback: null,
    confidence: null,
    conflict: false,
    fbOpen: false,
    fbReason: "",
    fbInclude: false,
    fbBusy: false,
    fbStatus: "",
    fbError: "",
};

/**
 * POST a vote to /buddy/feedback (a plain HTTP route with a JSON body, protected like /buddy/chat_stream: JSON content type
 * and Odoo's CSRF token in a header). Resolves to `{ ok: true, vote }` or `{ ok: false, message }`; throws only when the
 * network fails.
 */
export async function sendFeedback(body) {
    const response = await fetch("/buddy/feedback", {
        method: "POST",
        credentials: "same-origin",
        headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRF-Token": globalThis.odoo?.csrf_token || "",
        },
        body: JSON.stringify(body),
    });
    let data = null;
    try {
        data = await response.json();
    } catch {
        data = null;
    }
    if (response.ok && data && data.ok === true && (data.vote === null || cleanVote(data.vote))) {
        return { ok: true, vote: data.vote };
    }
    return { ok: false, message: typeof data?.message === "string" ? data.message.slice(0, 300) : "" };
}

function newRequestKey() {
    if (globalThis.crypto?.randomUUID) {
        return globalThis.crypto.randomUUID();
    }
    // getRandomValues also works on HTTP intranets where randomUUID is unavailable.
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * Incremental Server-Sent Events parser. `feed(bytes)` (a Uint8Array) or `feedText(string)` returns the events that
 * the new data completed as `{ event, data }`. Events and even UTF-8 characters may be split between reads: the
 * TextDecoder keeps the unfinished bytes, and unfinished lines wait for the next read. Comments (":") are ignored.
 */
export class SSEParser {
    constructor() {
        this.buffer = "";
        this.event = "";
        this.data = [];
        this.decoder = new TextDecoder("utf-8");
        this.started = false;
    }

    feed(bytes) {
        return this.feedText(this.decoder.decode(bytes, { stream: true }));
    }

    feedText(text) {
        if (!this.started && text) {
            this.started = true;
            text = text.replace(/^﻿/, "");
        }
        const buffer = this.buffer + text;
        const events = [];
        const eol = /\r\n|\n|\r/g;
        let position = 0;
        let match;
        while ((match = eol.exec(buffer))) {
            if (match[0] === "\r" && eol.lastIndex === buffer.length) {
                break; // a CR at the very end may be the first half of a CRLF
            }
            this._line(buffer.slice(position, match.index), events);
            position = eol.lastIndex;
        }
        this.buffer = buffer.slice(position);
        return events;
    }

    _line(line, events) {
        if (!line) {
            if (this.data.length) {
                events.push({ event: this.event || "message", data: this.data.join("\n") });
            }
            this.event = "";
            this.data = [];
        } else if (!line.startsWith(":")) {
            const index = line.indexOf(":");
            const field = index < 0 ? line : line.slice(0, index);
            const value = index < 0 ? "" : line.slice(index + 1).replace(/^ /, "");
            if (field === "event") {
                this.event = value;
            } else if (field === "data") {
                this.data.push(value);
            }
        }
    }
}

const requestFrame = (callback) =>
    typeof globalThis.requestAnimationFrame === "function" ? globalThis.requestAnimationFrame(callback) : setTimeout(callback, 16);
const cancelFrame = (handle) =>
    typeof globalThis.cancelAnimationFrame === "function" ? globalThis.cancelAnimationFrame(handle) : clearTimeout(handle);
// The user is "at the bottom" when less than this many pixels are left below the visible area.
const STICK_PX = 48;

function storage(kind) {
    // Storage may be blocked (private mode, browser policies): it must never break the widget.
    return (action, key, value) => {
        try {
            const store = kind === "local" ? localStorage : sessionStorage;
            return action === "get" ? store.getItem(key) : store.setItem(key, value);
        } catch {
            return null;
        }
    };
}
const safeSession = storage("session");
const safeLocal = storage("local");

// ---------------------------------------------------------------- Colors
function channels(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function luminance([r, g, b]) {
    const lin = (c) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

const toHex = (rgb) => "#" + rgb.map((c) => Math.round(c).toString(16).padStart(2, "0")).join("");
const INK = "#04122a";
const PANEL_LUMINANCE = luminance(channels("#0b0f2b"));

function toHsl([r, g, b]) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const l = (max + min) / 2;
    const d = max - min;
    if (!d) {
        return [0, 0, l];
    }
    const s = d / (1 - Math.abs(2 * l - 1));
    const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [(h * 60 + 360) % 360, s, l];
}

function fromHsl(h, s, l) {
    const k = (n) => (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return toHex([f(0) * 255, f(8) * 255, f(4) * 255]);
}

/**
 * Colors of the Kitsune and Mochi buddies, all derived from the primary color: the fur (light, mid and dark tones) and
 * the tinted white of the mochi. The warm accent (inner ears, iris, whisker marks) stays warm: when the primary color
 * is itself orange or yellow it turns rose so it still stands out.
 */
export function buddyPalette(rgb) {
    const [h, s] = toHsl(rgb);
    const gray = s < 0.08;
    const sat = (value) => (gray ? 0 : Math.min(0.95, Math.max(value, s * 0.9)));
    const warm = h >= 8 && h <= 70 && !gray ? ["#ff7aa0", "#cf3f6a"] : ["#ffb457", "#d9692a"];
    return [
        `--bd-fur: ${fromHsl(h, sat(0.55), 0.74)}`,
        `--bd-fur-lt: ${fromHsl(h, sat(0.45), 0.91)}`,
        `--bd-fur-dk: ${fromHsl(h, sat(0.55), 0.42)}`,
        `--bd-warm: ${warm[0]}`,
        `--bd-warm-dk: ${warm[1]}`,
        `--bd-mochi: ${fromHsl(h, gray ? 0 : 0.6, 0.985)}`,
        `--bd-mochi-dk: ${fromHsl(h, gray ? 0 : 0.45, 0.85)}`,
    ].join("; ") + ";";
}

/**
 * Derive the CSS variables of the widget from the admin's primary color:
 * the fill color, a readable text color on top of it, an "accent" light enough to read on the dark panel,
 * and the palette of the Kitsune and Mochi buddies.
 */
export function themeVars(color) {
    if (!HEX_COLOR.test(color || "")) {
        return "";
    }
    const rgb = channels(color);
    const lum = luminance(rgb);
    const onPrimary = (lum + 0.05) / (luminance(channels(INK)) + 0.05) >= 1.05 / (lum + 0.05) ? INK : "#ffffff";
    let accent = rgb;
    for (let i = 0; i < 10 && (luminance(accent) + 0.05) / (PANEL_LUMINANCE + 0.05) < 4.5; i++) {
        accent = accent.map((c) => c + (255 - c) * 0.15);
    }
    return `--bd-primary: ${color.toLowerCase()}; --bd-on-primary: ${onPrimary}; --bd-accent: ${toHex(accent)}; ${buddyPalette(rgb)}`;
}

// -------------------------------------------------------------- Safe markdown
// A tiny subset (bold, italic, code, lists, line breaks) parsed into plain data. The templates render it
// as OWL nodes with t-esc: the model's text is never injected as HTML.
const INLINE_RE = new RegExp(
    [
        String.raw`\*\*([^*\n]+?)\*\*`,
        String.raw`\x60([^\x60\n]+)\x60`,
        String.raw`(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])`,
        String.raw`(?<!\w)_(?![\s_])([^_\n]+?)(?<![\s_])_(?!\w)`,
    ].join("|"),
    "g"
);

function parseInline(line) {
    const tokens = [];
    let last = 0;
    for (const match of line.matchAll(INLINE_RE)) {
        if (match.index > last) {
            tokens.push({ t: "text", v: line.slice(last, match.index) });
        }
        if (match[1] !== undefined) {
            tokens.push({ t: "strong", v: match[1] });
        } else if (match[2] !== undefined) {
            tokens.push({ t: "code", v: match[2] });
        } else {
            tokens.push({ t: "em", v: match[3] ?? match[4] });
        }
        last = match.index + match[0].length;
    }
    if (last < line.length) {
        tokens.push({ t: "text", v: line.slice(last) });
    }
    return tokens;
}

export function parseMarkdown(text) {
    const blocks = [];
    let paragraph = null;
    let list = null;
    for (const raw of String(text ?? "").replace(/\r\n?/g, "\n").split("\n")) {
        const line = raw.trimEnd();
        if (!line.trim()) {
            paragraph = null; // a blank line ends the paragraph, not (yet) the list
            continue;
        }
        const heading = /^\s{0,3}#{1,6}\s+(.+?)\s*#*$/.exec(line);
        if (heading) {
            // Headings are shown as a bold line: the widget is too narrow for real heading sizes.
            blocks.push({ type: "p", inline: [{ t: "strong", v: heading[1] }] });
            paragraph = null;
            list = null;
            continue;
        }
        const bullet = /^\s*[-*•]\s+(.+)$/.exec(line);
        const ordered = /^\s*(\d{1,3})[.)]\s+(.+)$/.exec(line);
        if (bullet || ordered) {
            const type = bullet ? "ul" : "ol";
            if (!list || list.type !== type) {
                list = { type, items: [], start: ordered ? Number(ordered[1]) : 1 };
                blocks.push(list);
            }
            list.items.push(parseInline((bullet ? bullet[1] : ordered[2]).trim()));
            paragraph = null;
            continue;
        }
        list = null;
        if (!paragraph) {
            paragraph = { type: "p", inline: [] };
            blocks.push(paragraph);
        } else {
            paragraph.inline.push({ t: "br" });
        }
        paragraph.inline.push(...parseInline(line.trim()));
    }
    return blocks;
}

// The server already filters source links; this is the second wall. Exactly Google's https hosts, no port or user info,
// and a path without whitespace, control characters or a backslash (browsers read a backslash as a slash, which would
// let an address like https://docs.google.com\@evil.example/ land on another host).
const DRIVE_LINK = /^https:\/\/(?:docs|drive)\.google\.com\/[^\s\\\x00-\x1f\x7f]*$/;

/** The link as it may go in an href, or "" when it is not a Google Docs/Drive https address. */
export function safeDriveLink(link) {
    return typeof link === "string" && DRIVE_LINK.test(link) ? link : "";
}

/** Fill {nombre}/{name} in an admin-written greeting; without a name the placeholder (and its comma) disappear. */
export function renderGreeting(template, firstName) {
    const name = (firstName || "").trim();
    return template
        .replace(/[,\s]*\{(?:nombre|name)\}/gi, (found) => (name ? found.replace(/\{(?:nombre|name)\}/i, name) : ""))
        .replace(/\s+([!?.,;:])/g, "$1")
        .trim();
}

/**
 * Buddy: lanzador flotante + globito de saludo + panel de chat.
 * Toda la inteligencia vive en el servicio de IA; acá solo hay interfaz y llamadas a /buddy/*.
 */
export class Buddy extends Component {
    static template = "buddy_ia.Buddy";
    static components = { BuddyAvatar };
    static props = {};

    setup() {
        this._seq = 0;
        this._epoch = 0;
        this._destroyed = false;
        this._configPromise = null;
        this.sessionGeneration = null;
        this.action = useService("action");
        this.state = useState({
            ready: false,
            disabled: false,
            hidden: safeLocal("get", HIDDEN_KEY) === "1",
            open: false,
            loading: false,
            streaming: false,
            forgetting: false,
            confirmForget: false,
            forgetError: "",
            input: "",
            messages: [],
            expression: "neutral",
            ring: "idle",
            bubble: null,
            firstName: "",
            buddyName: "Buddy",
            greeting: "",
            tagline: "",
            suggestions: [],
            primaryColor: "",
            restored: false,
            // false only when the server says so: an older server (or one that failed to answer) keeps the old behavior.
            configured: true,
            isAdmin: false,
            company: "",
            keepQuestions: false,
            // Look chosen by the administrator (always filtered by cleanLook) and the idle gesture being played.
            character: "mochi",
            accessory: "none",
            idle: { enabled: true, frequency: "normal", groups: [] },
            gesture: null,
            // Gentle proactivity (E4b): what the administrator allows, the suggestions of the server that fit the person (always filtered
            // by cleanSuggestions), the turn of the rotation of the chips and what the open bubble is for («tasks» = the reminder, none = the greeting).
            proactive: { suggestions: true, nudge: true },
            contextual: [],
            chipRotation: 0,
            bubbleAction: null,
            // Onboarding («Tus tareas de hoy»): the person's journeys (always filtered by cleanJourneys), how many required steps
            // are due today or late (the dot of the launcher) and whether the block is open.
            journeys: [],
            pending: 0,
            tasksOpen: false,
            tasksError: "",
            tasksStatus: "",
        });
        this._tasksLoadedAt = 0;
        this._suggestionsLoadedAt = 0;
        this._tasksAutoOpened = false;
        this._greetedThisPage = false;
        this._historyPending = false;
        this._historyPromise = null;
        this.messagesRef = useRef("messages");
        this.inputRef = useRef("input");
        this.launcherRef = useRef("launcher");
        this.confirmNoRef = useRef("confirmNo");
        this.nudgeRef = useRef("nudge");
        this._pageActivity = 0;

        useEffect(
            () => {
                const el = this.messagesRef.el;
                if (el) {
                    el.scrollTop = el.scrollHeight;
                }
            },
            () => [this.state.messages.length, this.state.loading, this.state.open]
        );
        this.idleScheduler = new IdleScheduler({
            config: () => this.state.idle,
            canPlay: () => this.canPlayGesture(),
            quietSeconds: () => quietSecondsFor({ open: this.state.open, frequency: this.state.idle.frequency }),
            pick: () => this.pickIdleGesture(),
            onGesture: (id) => {
                this._lastGesture = id;
                this.state.gesture = id;
            },
            onEnd: () => {
                this.state.gesture = null;
            },
            nudge: () => this.nudgeOffer(),
            onNudge: (offer) => this.showNudge(offer),
        });
        this._onVisibility = () => {
            this.syncIdle();
            if (!document.hidden) {
                // A tab left open since yesterday: the dot and the list are brought up to date (at most every 30 minutes).
                this.loadTasks({ maxAge: 30 * 60 * 1000 });
            }
        };
        this._onMotion = () => this.syncIdle();
        // Somebody is working in the page (keys, clicks, the wheel): the reminder waits (see nudgeOffer).
        this._onPageActivity = () => {
            this._pageActivity = Date.now();
        };
        onMounted(() => {
            document.addEventListener("visibilitychange", this._onVisibility);
            for (const type of ["keydown", "pointerdown", "wheel"]) {
                document.addEventListener(type, this._onPageActivity, { capture: true, passive: true });
            }
            this._motionQuery = window.matchMedia?.("(prefers-reduced-motion: reduce)") || null;
            this._motionQuery?.addEventListener?.("change", this._onMotion);
            this.init();
        });
        onPatched(() => {
            if (this._wantLauncherFocus) {
                this._wantLauncherFocus = false;
                this.launcherRef.el?.focus();
            }
            if (this._wantInputFocus && !this.state.loading) {
                this._wantInputFocus = false;
                this.focusInputIfLost();
            }
            if (this._feedbackFocus) {
                // The reason form just opened: the keyboard goes straight into it.
                const id = this._feedbackFocus;
                this._feedbackFocus = null;
                this.messagesRef.el?.querySelector(`[data-fb-form="${Number(id)}"] input`)?.focus();
            }
            if (this._feedbackRefocus) {
                // The form just closed (and its focused button with it): the focus goes back to the 👎 that opened it.
                const id = this._feedbackRefocus;
                this._feedbackRefocus = null;
                this.messagesRef.el?.querySelector(`[data-fb-down="${Number(id)}"]`)?.focus();
            }
            if (this._stickScroll) {
                // The text grew while the person was reading the bottom: keep following it.
                this._stickScroll = false;
                const el = this.messagesRef.el;
                if (el) {
                    el.scrollTop = el.scrollHeight;
                }
            }
        });
        onWillUnmount(() => {
            this._destroyed = true;
            this._epoch++;
            this.abortStream();
            clearTimeout(this._showTimer);
            clearTimeout(this._hideTimer);
            clearTimeout(this._focusTimer);
            this.idleScheduler.stop();
            document.removeEventListener("visibilitychange", this._onVisibility);
            for (const type of ["keydown", "pointerdown", "wheel"]) {
                document.removeEventListener(type, this._onPageActivity, { capture: true });
            }
            this._motionQuery?.removeEventListener?.("change", this._onMotion);
        });
    }

    /** Loose characters (Kitsune, Mochi) have no dark disc behind them; a registered character with `disc: true` does. */
    get looseBuddy() {
        return !BUDDIES[this.state.character].disc;
    }

    // ------------------------------------------------------------------ idle gestures
    /** Reduced motion is a promise to the person: no gestures at all (the chat itself keeps working). */
    get reducedMotion() {
        return Boolean(this._motionQuery?.matches);
    }

    /** Should the scheduler be running right now? Not while hidden, disabled, in a hidden tab or with reduced motion. */
    syncIdle() {
        const tabVisible = document.visibilityState !== "hidden";
        const active = this.state.ready && !this.state.disabled && !this.state.hidden && this.state.idle.enabled &&
            tabVisible && !this.reducedMotion && !this._destroyed;
        if (active) {
            this.idleScheduler.start();
        } else {
            this.idleScheduler.pause();
        }
    }

    /** A gesture must never get in the way of the chat: nothing while it is busy, greeting or asking something. */
    canPlayGesture() {
        const s = this.state;
        return !this._destroyed && !s.hidden && !s.disabled && !s.loading && !s.streaming && !s.forgetting &&
            !s.confirmForget && !s.bubble && s.ring === "idle" && !this.reducedMotion &&
            document.visibilityState !== "hidden";
    }

    // ------------------------------------------------------------------ the small bubble about the tasks of the day (E4b)
    /**
     * Is there something to tell, right now, with a bubble? Asked by the idle scheduler on the turn of a gesture (so it never comes
     * while the person is busy, the tab is hidden, gestures are off or the person asked for reduced motion). Never with the chat open,
     * never twice in a day, never after the person already knows (they closed the list today or the greeting was on screen), and only
     * for required tasks that are due today or late. Returns `{ text, gesture }` or null.
     */
    nudgeOffer() {
        const s = this.state;
        if (!s.proactive.nudge || s.open || s.hidden || s.disabled || s.pending < 1 || this._greetedThisPage) {
            return null;
        }
        if (safeLocal("get", NUDGED_KEY) === localDayKey() || safeLocal("get", TASKS_CLOSED_KEY) === todayKey()) {
            return null;
        }
        if (Date.now() - this._pageActivity < PAGE_QUIET_MS || this.typingElsewhere()) {
            return null; // the person is busy in the page: the turn passes and the next one asks again
        }
        const text = nudgeText({ pending: s.pending, late: hasLateTask(s.journeys) });
        return text ? { text, gesture: nudgeGesture({ buddy: s.character, accessory: s.accessory, groups: s.idle.groups }) } : null;
    }

    /** Is the cursor in a field of the page (outside the buddy)? Then a bubble would pop up over what they are writing. */
    typingElsewhere() {
        const active = document.activeElement;
        return Boolean(active && !active.closest?.(".o_buddy") && active.matches?.("input, textarea, select, [contenteditable]:not([contenteditable='false'])"));
    }

    showNudge(offer) {
        this.state.bubble = offer.text;
        this.state.bubbleAction = "tasks";
        // Remembered when it is SHOWN: a closed tab or a reload must not bring it back the same day.
        safeLocal("set", NUDGED_KEY, localDayKey());
        clearTimeout(this._hideTimer);
        this._hideTimer = setTimeout(() => this.dismissNudge(), NUDGE_SECONDS * 1000);
    }

    dismissNudge() {
        clearTimeout(this._hideTimer);
        // The keyboard was on the bubble: it must not fall to the page when the bubble goes away.
        if (this.nudgeRef.el?.contains(document.activeElement)) {
            this.focusLauncherSoon();
        }
        this.state.bubble = null;
        this.state.bubbleAction = null;
    }

    /** Pointing at the bubble or having the keyboard on it stops the clock; leaving it gives a few seconds more. */
    holdNudge() {
        clearTimeout(this._hideTimer);
    }

    releaseNudge() {
        if (this.state.bubbleAction) {
            clearTimeout(this._hideTimer);
            this._hideTimer = setTimeout(() => this.dismissNudge(), NUDGE_RELEASE_SECONDS * 1000);
        }
    }

    onNudgeKeydown(ev) {
        if (ev.key === "Escape" && !ev.isComposing) {
            ev.preventDefault();
            ev.stopPropagation();
            this.dismissNudge();
        }
    }

    /** The bubble was tapped: the panel opens with «Tus tareas de hoy» already unfolded. */
    openTasksFromBubble() {
        this.dismissNudge();
        this.state.tasksOpen = true;
        this.state.tasksError = "";
        if (!this.state.open) {
            this.toggle();
        }
    }

    pickIdleGesture() {
        return pickGesture({
            buddy: this.state.character,
            accessory: this.state.accessory,
            groups: this.state.idle.groups,
            last: this._lastGesture,
            random: this.idleScheduler.random,
        });
    }

    /** Somebody is using the buddy (mouse over it, typing, clicking): the scheduler waits. */
    onActivity() {
        this.idleScheduler.touch();
    }

    /** The chat takes over: a gesture in progress is cut cleanly and the buddy waits quietly. */
    interruptGesture() {
        this.state.gesture = null;
        this.idleScheduler.interrupt();
    }

    // ------------------------------------------------------------------ texts (lazy: need the user's language)
    get panelId() {
        return "o_buddy_panel";
    }

    get panelLabel() {
        return _t("Chat con %(name)s", { name: this.state.buddyName });
    }

    get launcherLabel() {
        if (this.state.open) {
            return _t("Cerrar el chat con %(name)s", { name: this.state.buddyName });
        }
        return this.state.pending
            ? _t("Abrir el chat con %(name)s. Tenés tareas de onboarding para hoy.", { name: this.state.buddyName })
            : _t("Abrir el chat con %(name)s", { name: this.state.buddyName });
    }

    // ------------------------------------------------------------------ onboarding («Tus tareas de hoy»)
    get tasksSummary() {
        return summaryText(this.state.pending);
    }

    dayLabel(journey) {
        return _t("Día %(day)s", { day: journey.day });
    }

    progressLabel(journey) {
        return _t("%(done)s de %(total)s tareas obligatorias hechas", { done: journey.done, total: journey.total });
    }

    /** The name a screen reader gives the bar: which journey, and how far. */
    progressAria(journey) {
        return `${journey.name}: ${this.progressLabel(journey)}`;
    }

    untilLabel(task) {
        return untilText(task.daysUntil);
    }

    moreUpcoming(journey) {
        return _t("…y %(count)s más", { count: journey.upcomingTotal - journey.upcoming.length });
    }

    /** Why «Preguntarle al buddy» is off right now (it is shown as the tooltip of the button). */
    askHint() {
        if (!this.state.configured) {
            return _t("Buddy todavía no está configurado");
        }
        return this.state.loading || this.state.forgetting ? _t("Esperá a que termine la respuesta") : "";
    }

    lateLabel(task) {
        return lateText(task.daysLate);
    }

    upcomingLabel(journey) {
        return _t("Próximas (%(count)s)", { count: journey.upcomingTotal });
    }

    /** The links of a step only come out of cleanJourneys (Drive hosts for documents, plain https for the rest). */
    taskLinkLabel(task) {
        return task.link.kind === "doc" ? _t("Abrir el documento: %(name)s", { name: task.link.label }) : _t("Abrir el enlace: %(name)s", { name: task.link.label });
    }

    /** Fetch the person's tasks. `maxAge` skips the request when the last one is that recent (milliseconds). */
    async loadTasks({ maxAge = 0 } = {}) {
        if (this._destroyed || this.state.disabled || this._tasksBusy || (maxAge && Date.now() - this._tasksLoadedAt < maxAge)) {
            return;
        }
        this._tasksBusy = true;
        try {
            const result = await loadJourneys();
            if (!this._destroyed && result.ok) {
                this._tasksLoadedAt = Date.now();
                this.applyTasks(result);
            }
        } catch {
            // Without the block (and the dot) the chat works exactly as before.
        } finally {
            this._tasksBusy = false;
        }
    }

    applyTasks(result) {
        this.state.journeys = result.journeys;
        this.state.pending = result.pending;
        if (!result.journeys.length) {
            this.state.tasksOpen = false;
        }
    }

    /** The first time the panel opens in this page, with tasks due and nothing said yet, the list opens by itself. */
    openTasksOnce() {
        const closedToday = safeLocal("get", TASKS_CLOSED_KEY) === todayKey();
        if (!this._tasksAutoOpened && !closedToday && this.state.pending > 0 && !this.hasUserMessages) {
            this._tasksAutoOpened = true;
            this.state.tasksOpen = true;
        }
    }

    toggleTasks() {
        this.state.tasksOpen = !this.state.tasksOpen;
        this.state.tasksError = "";
        if (!this.state.tasksOpen) {
            // Closed on purpose: it does not open by itself again today.
            safeLocal("set", TASKS_CLOSED_KEY, todayKey());
        }
    }

    async toggleTask(task, ev) {
        const done = ev.target.checked;
        if (task.busy || this._destroyed) {
            ev.target.checked = !done;
            return;
        }
        task.busy = true;
        this.state.tasksError = "";
        try {
            const result = await setStepDone(task.id, done);
            if (this._destroyed) {
                return;
            }
            if (result.ok) {
                this._tasksLoadedAt = Date.now();
                this.applyTasks(result);
                this.state.tasksStatus = done
                    ? _t("Marcaste «%(title)s» como hecha.", { title: task.title })
                    : _t("Volviste a poner «%(title)s» como pendiente.", { title: task.title });
            } else {
                ev.target.checked = !done;
                this.state.tasksError = result.message || _t("No pude guardar el cambio. Probá de nuevo.");
                if (result.status === 404) {
                    this.loadTasks();
                }
            }
        } catch {
            ev.target.checked = !done;
            this.state.tasksError = _t("No pude guardar el cambio. Probá de nuevo.");
        } finally {
            task.busy = false;
        }
    }

    /** «Preguntarle al buddy»: the suggested question goes to the chat as if the person had typed it. */
    async askTask(task) {
        if (!task.question || this.state.loading || this.state.forgetting || !this.state.configured) {
            return;
        }
        this.state.tasksOpen = false;
        await this.send(task.question);
        const reply = this.lastMessage;
        // Asking the buddy IS the step: once the answer arrived (an error has no request key) it is ticked, and the
        // person can untick it from the list.
        if (task.kind === "ask" && !task.done && reply && reply.role !== "user" && reply.requestKey && !this._destroyed) {
            await this.markDone(task);
        }
    }

    /** Tick a step without a click on its box (the answer of the server replaces the list, as with a click). */
    async markDone(task) {
        try {
            const result = await setStepDone(task.id, true);
            if (!this._destroyed && result.ok) {
                this._tasksLoadedAt = Date.now();
                this.applyTasks(result);
                this.state.tasksStatus = _t("Marcaste «%(title)s» como hecha.", { title: task.title });
            }
        } catch {
            // Not ticked: the person can tick it by hand.
        }
    }

    /** Who is speaking, for screen readers (the visible bubbles only have the text). */
    get replyLabel() {
        return _t("%(name)s responde", { name: this.state.buddyName });
    }

    get hideLabel() {
        return _t("Ocultar a %(name)s", { name: this.state.buddyName });
    }

    get showLabel() {
        return _t("Mostrar a %(name)s", { name: this.state.buddyName });
    }

    get tagline() {
        return this.state.tagline || _t("Tu guía en Odoo");
    }

    /** Why the buddy cannot answer yet. One text, shown in one place (the notice above the box), never as a chat message. */
    get notConfiguredText() {
        return this.state.isAdmin
            ? _t("Todavía no estoy configurado: falta terminar la configuración.")
            : _t("Todavía no estoy listo. Avisale a quien administra Odoo.");
    }

    /** The fixed suggestions: the ones the administrator wrote, or the three of the house. */
    get genericSuggestions() {
        if (this.state.suggestions.length) {
            return this.state.suggestions;
        }
        return [
            _t("Soy nuevo, ¿por dónde empiezo?"),
            _t("¿A quién le pregunto por un tema puntual?"),
            _t("Necesito ayuda de IT"),
        ];
    }

    /**
     * The chips: up to three that fit the person (the questions of their onboarding steps, the topics the company asks about most) that
     * rotate every time the panel opens and never repeat what they already asked in this conversation, completed with the fixed ones up
     * to four. Without anything from the server it is the fixed list, as it always was.
     */
    get suggestions() {
        const generic = this.genericSuggestions;
        if (!this.state.proactive.suggestions || !this.state.contextual.length) {
            return generic.map((text) => ({ kind: "generic", text, label: text }));
        }
        return pickChips({
            pool: this.state.contextual,
            generic,
            asked: this.state.messages.filter((message) => message.role === "user").map((message) => message.text),
            rotation: this.state.chipRotation,
        });
    }

    /** A chip that is the question of an onboarding step of the kind «preguntarle al buddy»: asking it IS the step, so it is ticked
     *  once the answer arrived (the same as with the button of the task). */
    async sendChip(chip) {
        await this.send(chip.text);
        const reply = this.lastMessage;
        if (chip.askId && reply && reply.role !== "user" && reply.requestKey && !this._destroyed) {
            await this.markDone({ id: chip.askId, title: chip.label });
            this.loadSuggestions();
        }
    }

    /** Ask the server for the suggestions that fit the person (`maxAge` skips the request when the last one is that recent). */
    async loadSuggestions({ maxAge = 0 } = {}) {
        if (this._destroyed || this.state.disabled || !this.state.proactive.suggestions || this._suggestionsBusy ||
            (maxAge && Date.now() - this._suggestionsLoadedAt < maxAge)) {
            return;
        }
        this._suggestionsBusy = true;
        try {
            const result = await loadSuggestions();
            if (!this._destroyed && result.ok) {
                this._suggestionsLoadedAt = Date.now();
                this.state.contextual = result.suggestions;
            }
        } catch {
            // Without them the chips are the fixed ones: nothing else changes.
        } finally {
            this._suggestionsBusy = false;
        }
    }

    /** The next turn of the rotation (kept in the tab, so every page and every opening of the panel shows a different mix). */
    nextChipRotation() {
        const turn = (Number.parseInt(safeSession("get", CHIP_ROTATION_KEY), 10) || 0) + 1;
        safeSession("set", CHIP_ROTATION_KEY, String(turn));
        this.state.chipRotation = turn;
    }

    get greetingText() {
        if (this.state.greeting) {
            return renderGreeting(this.state.greeting, this.state.firstName);
        }
        return this.state.firstName
            ? _t("¡Hola, %(name)s! ¿Te ayudo con algo?", { name: this.state.firstName })
            : _t("¡Hola! ¿Te ayudo con algo?");
    }

    get welcomeText() {
        const fallbackCompany = _t("la empresa");
        const hello = this.state.firstName ? _t("¡Hola, %(name)s!", { name: this.state.firstName }) : _t("¡Hola!");
        // One literal string (no concatenation): the translation exporter only extracts literals.
        return _t("%(hello)s Soy %(buddy)s. Respondo con la documentación de %(company)s: puedo explicarte cómo hacer algo en Odoo, contarte cómo trabajamos o ayudarte a encontrar a la persona indicada. ¿Por dónde empezamos?", {
            hello,
            buddy: this.state.buddyName,
            company: this.state.company || fallbackCompany,
        });
    }

    /** The welcome, when the panel opens on an empty conversation. Not configured: nothing (the notice says why). */
    pushWelcome() {
        if (this.state.configured) {
            this.pushBuddy({ text: this.welcomeText });
        }
    }

    /** The administrator's way out of the «not configured» notice: the settings screen of Buddy. */
    finishSetup() {
        if (window.matchMedia?.("(max-width: 480px)").matches) {
            this.state.open = false;
        }
        return this.action.doAction("buddy_ia.buddy_settings_action");
    }

    get rootStyle() {
        return themeVars(this.state.primaryColor);
    }

    safeLink(link) {
        return safeDriveLink(link);
    }

    sourceTitle(src) {
        return src.updated
            ? _t("Origen: %(owner)s · Actualizado: %(updated)s", { owner: src.owner, updated: src.updated })
            : _t("Origen: %(owner)s", { owner: src.owner });
    }

    get hasUserMessages() {
        return this.state.messages.some((m) => m.role === "user");
    }

    get lastMessage() {
        return this.state.messages.at(-1);
    }

    get canSend() {
        return this.state.configured && !this.state.loading && !this.state.forgetting && this.state.input.trim().length > 0;
    }

    // ------------------------------------------------------------------ config
    applyConfig(cfg) {
        if (!cfg || typeof cfg !== "object") {
            return;
        }
        const text = (value) => (typeof value === "string" ? value : "");
        this.state.firstName = text(cfg.first_name);
        this.state.buddyName = text(cfg.buddy_name) || "Buddy";
        this.state.disabled = cfg.enabled === false;
        this.state.greeting = text(cfg.greeting);
        this.state.tagline = text(cfg.tagline);
        this.state.suggestions = Array.isArray(cfg.suggestions)
            ? cfg.suggestions.filter((s) => typeof s === "string" && s.trim()).slice(0, 4)
            : [];
        this.state.primaryColor = HEX_COLOR.test(text(cfg.primary_color)) ? cfg.primary_color : "";
        this.state.configured = cfg.configured !== false;
        this.state.isAdmin = cfg.is_admin === true;
        this.state.company = text(cfg.company);
        // The administrator keeps every question: the «include my question» box would promise something untrue.
        this.state.keepQuestions = cfg.keep_questions === true;
        const look = cleanLook(cfg);
        this.state.character = look.buddy;
        this.state.accessory = look.accessory;
        this.state.idle = look.idle;
        // Older servers do not say it: everything on, as it was decided by default.
        this.state.proactive = {
            suggestions: cfg.proactive?.suggestions !== false,
            nudge: cfg.proactive?.nudge !== false,
        };
        if (this.state.ready) {
            this.syncIdle();
        }
    }

    ensureConfig() {
        if (!this._configPromise) {
            this._configPromise = rpc("/buddy/config").then((cfg) => {
                if (!this._destroyed) {
                    this.applyConfig(cfg);
                }
                if (!Number.isInteger(cfg?.session_generation) || cfg.session_generation < 0) {
                    // The server explains why in cfg.answer (not configured, busy...): keep it for the user.
                    const error = new Error("Missing conversation generation");
                    error.userMessage = typeof cfg?.answer === "string" ? cfg.answer : "";
                    throw error;
                }
                if (!this._destroyed) {
                    this.sessionGeneration = cfg.session_generation;
                }
            }).catch((error) => {
                this._configPromise = null;
                throw error;
            });
        }
        return this._configPromise;
    }

    async init() {
        try {
            await this.ensureConfig();
        } catch {
            // Sin config seguimos con los valores por defecto.
        }
        if (this._destroyed) {
            return;
        }
        this.state.ready = true;
        this.syncIdle();
        if (this.state.disabled) {
            return;
        }
        // The tasks of the day travel in parallel with the history: a failure only means no block and no dot.
        this.loadTasks();
        // The suggestions that fit the person too (a failure only means the fixed chips).
        this.nextChipRotation();
        this.loadSuggestions();
        // The conversation lives on the server: pick it up where it was left instead of starting from scratch.
        await this.loadHistory();
        if (this._destroyed || this.state.hidden) {
            return;
        }
        if (this.shouldGreet() && !this.state.restored) {
            this._showTimer = setTimeout(() => {
                if (!this.state.open && !this.state.hidden) {
                    this.state.bubble = this.greetingText;
                    this.state.expression = "happy";
                    // One bubble per visit: the reminder of the tasks never follows the greeting in the same page.
                    this._greetedThisPage = true;
                }
            }, 2000);
            this._hideTimer = setTimeout(() => this.dismissBubble(), 14000);
        }
    }

    // ------------------------------------------------------------------ panel
    shouldGreet() {
        return safeLocal("get", OPENED_KEY) !== "1" && safeLocal("get", GREETED_KEY) !== todayKey();
    }

    dismissBubble() {
        if (this.state.bubbleAction) {
            this.dismissNudge();
            return;
        }
        this.state.bubble = null;
        if (!this.state.open) {
            this.state.expression = "neutral";
        }
        safeLocal("set", GREETED_KEY, todayKey());
    }

    /** Put the cursor back in the box, unless the person went somewhere else on purpose (a link, another field). */
    focusInputIfLost() {
        const input = this.inputRef.el;
        if (!input || input.disabled || !this.state.open) {
            return;
        }
        const active = document.activeElement;
        const panel = input.closest(".o_buddy_panel");
        if (!active || active === document.body || panel?.contains(active)) {
            input.focus();
        }
    }

    focusLauncherSoon() {
        // Focus it after the next render (see onPatched): on phones the launcher is hidden while the panel
        // is open, and after "show" it does not exist yet.
        this._wantLauncherFocus = true;
    }

    toggle() {
        this.interruptGesture();
        this.state.open = !this.state.open;
        this.state.bubble = null;
        this.state.bubbleAction = null;
        clearTimeout(this._hideTimer);
        this.state.confirmForget = false;
        safeLocal("set", GREETED_KEY, todayKey());
        if (this.state.open) {
            safeLocal("set", OPENED_KEY, "1");
            this.state.expression = "happy";
            this.openTasksOnce();
            this.loadTasks({ maxAge: 30000 });
            if (!this.hasUserMessages) {
                this.nextChipRotation();
            }
            this.loadSuggestions({ maxAge: SUGGESTIONS_MAX_AGE });
            // While the history is still on its way the greeting waits: it would flash and then be replaced.
            if (!this.state.messages.length && !this._historyPending) {
                this.pushWelcome();
            }
            clearTimeout(this._focusTimer);
            // On a phone the keyboard must not jump up over a list that just opened by itself.
            const touch = window.matchMedia?.("(pointer: coarse)")?.matches;
            if (!(touch && this.state.tasksOpen)) {
                this._focusTimer = setTimeout(() => this.state.open && this.inputRef.el?.focus(), 50);
            }
        } else {
            this.state.expression = "neutral";
            this.state.ring = "idle";
            clearTimeout(this._focusTimer);
            this.focusLauncherSoon();
        }
    }

    /** The user asked not to see the buddy: remembered in this browser (localStorage). */
    hide() {
        this.state.open = false;
        this.state.bubble = null;
        this.state.bubbleAction = null;
        this.state.confirmForget = false;
        this.state.hidden = true;
        this.state.gesture = null;
        this.syncIdle();
        clearTimeout(this._showTimer);
        clearTimeout(this._hideTimer);
        safeLocal("set", HIDDEN_KEY, "1");
    }

    show() {
        this.state.hidden = false;
        this.loadTasks({ maxAge: 30000 });
        this.syncIdle();
        safeLocal("set", HIDDEN_KEY, "0");
        this.focusLauncherSoon();
    }

    pushBuddy(extra) {
        this.state.messages.push({
            id: ++this._seq,
            role: "buddy",
            sources: [],
            ticket: null,
            contact: null,
            ticketResult: null,
            creating: false,
            retryText: "",
            ...FEEDBACK_DEFAULTS,
            ...extra,
            requestKey: null,
            ...extra,
            blocks: parseMarkdown(extra.text),
            epoch: this._epoch,
            sessionGeneration: this.sessionGeneration,
        });
    }

    // ------------------------------------------------------------------ restored conversation
    loadHistory() {
        if (!this._historyPromise) {
            this._historyPending = true;
            this._historyPromise = this._fetchHistory().finally(() => {
                this._historyPending = false;
                // The panel was opened while waiting and there was nothing to restore: greet now.
                if (!this._destroyed && this.state.open && !this.state.messages.length) {
                    this.pushWelcome();
                }
            });
        }
        return this._historyPromise;
    }

    async _fetchHistory() {
        const epoch = this._epoch;
        try {
            const res = await rpc("/buddy/history");
            // Anything that happened meanwhile (the user already asked, deleted or the session changed) wins.
            if (this._destroyed || epoch !== this._epoch || this.hasUserMessages) {
                return;
            }
            if (!res || res.error || !Array.isArray(res.messages) ||
                    !Number.isInteger(res.session_generation) || res.session_generation < 0) {
                return;
            }
            const restored = res.messages.map((item) => this.restoreMessage(item, res.session_generation)).filter(Boolean);
            if (!restored.some((msg) => msg.role === "user")) {
                return;
            }
            const last = res.messages.at(-1);
            const when = last && typeof last.date === "string" ? new Date(last.date.replace(" ", "T") + "Z") : null;
            if (when && !Number.isNaN(when.getTime()) && when.toDateString() !== new Date().toDateString()) {
                restored.push({
                    id: ++this._seq,
                    role: "divider",
                    text: _t("Conversación anterior · %(date)s", {
                        date: when.toLocaleDateString(undefined, { day: "numeric", month: "short" }),
                    }),
                });
            }
            this.sessionGeneration = res.session_generation;
            this.state.messages = restored;
            this.state.restored = true;
            this.state.expression = this.state.open ? "happy" : "neutral";
            this.state.ring = "idle";
            this.state.bubble = null;
        } catch {
            // Without history the widget behaves as a new conversation: nothing breaks.
        }
    }

    /** One stored message from the server as a widget message (null if it is not usable). Text only, never HTML. */
    restoreMessage(item, generation) {
        if (!item || typeof item.text !== "string" || !item.text) {
            return null;
        }
        if (item.role === "user") {
            return { id: ++this._seq, role: "user", text: item.text };
        }
        if (item.role !== "assistant") {
            return null;
        }
        const list = (value) => (Array.isArray(value) ? value : []);
        const ticket = item.ticket && typeof item.ticket.title === "string" && item.ticket.title ? item.ticket : null;
        return {
            id: ++this._seq,
            role: "buddy",
            text: item.text,
            blocks: parseMarkdown(item.text),
            sources: list(item.sources).filter((src) => src && typeof src.title === "string"),
            ticket,
            contact: item.contact && typeof item.contact === "object" ? item.contact : null,
            // The server says whether this answer's ticket was already created: the card keeps showing it after a reload.
            ticketResult: ticket && this.createdTicket(item.ticket_created),
            creating: false,
            retryText: "",
            epoch: this._epoch,
            sessionGeneration: generation,
            requestKey: ticket && typeof item.ticket_key === "string" ? item.ticket_key : null,
            ...FEEDBACK_DEFAULTS,
            // The vote already given (so a reload shows it) and the confidence note come from the server, filtered.
            feedbackToken: cleanFeedbackToken(item.feedback_token),
            feedback: cleanFeedbackToken(item.feedback_token) ? cleanVote(item.feedback) : null,
            confidence: cleanConfidence(item.confidence),
            conflict: item.conflict === true,
        };
    }

    /** The `{id, name}` of an already created ticket as sent by the history route, or null if it is not usable. */
    createdTicket(created) {
        return created && Number.isInteger(created.id) && created.id > 0 && typeof created.name === "string"
            ? { id: created.id, name: created.name }
            : null;
    }

    onInput() {
        this.onActivity();
        if (!this.state.loading && !this.state.forgetting) {
            this.state.ring = this.state.input.trim() ? "listening" : "idle";
        }
    }

    onKeydown(ev) {
        if (ev.isComposing || ev.keyCode === 229) {
            return;
        }
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.send();
        }
    }

    onPanelKeydown(ev) {
        if (ev.key === "Escape" && !ev.isComposing && this.state.open) {
            ev.preventDefault();
            ev.stopPropagation();
            this.toggle();
        }
    }

    // ------------------------------------------------------------------ chat
    async send(text) {
        const message = (text ?? this.state.input).trim();
        if (!message || !this.state.configured || this.state.loading || this.state.forgetting || this._destroyed) {
            return;
        }
        // The chat takes over from the very first instant: a gesture in progress is cut before anything is awaited.
        this.interruptGesture();
        if (this._historyPending) {
            // The restored conversation must land before the new question, never after it.
            await this._historyPromise;
            if (this._destroyed || this.state.loading || this.state.forgetting) {
                return;
            }
        }
        this._stopped = false;
        this.state.input = "";
        if (this.inputRef.el) {
            // Si el texto se escribió y se envió dentro del mismo cuadro de render, OWL no ve cambio de
            // estado y deja el texto viejo en el DOM: se limpia el elemento directamente.
            this.inputRef.el.value = "";
            // Sending with the button or a suggestion moved the focus there (and the button turns off): back to the box.
            this.inputRef.el.focus();
        }
        this.state.messages.push({ id: ++this._seq, role: "user", text: message });
        this.state.loading = true;
        this.state.ring = "thinking";
        this.state.expression = "thinking";
        const epoch = this._epoch;
        let configured = false;
        try {
            await this.ensureConfig();
            if (epoch !== this._epoch || this._destroyed) {
                return;
            }
            configured = true;
            const requestId = newRequestKey();
            const res = await this.ask(message, requestId, epoch);
            if (epoch !== this._epoch || this._destroyed) {
                return;
            }
            if (res.error === "session_changed") {
                this.handleSessionChanged(res);
                return;
            }
            this.finishBuddy({
                text: res.answer,
                sources: res.sources || [],
                ticket: res.ticket || null,
                contact: res.contact || null,
                retryText: RETRYABLE.has(res.error) ? message : "",
                // The chat request id is also the ticket's idempotency key: a reload restores the same key.
                requestKey: res.error ? null : requestId,
                // Only a real answer can be rated and carries a confidence note.
                feedbackToken: res.error ? null : cleanFeedbackToken(res.feedback_token),
                confidence: res.error ? null : cleanConfidence(res.confidence),
                conflict: !res.error && res.conflict === true,
            });
            this.state.expression = res.expression || "neutral";
            this.state.ring = res.ring || "idle";
        } catch (error) {
            if (epoch !== this._epoch || this._destroyed) {
                return;
            }
            if (this._stopped) {
                // The person pressed «Detener»: what was written so far stays, and the question can be asked again.
                this._stopped = false;
                this.finishBuddy({ text: this._liveText || _t("Detuve la respuesta."), retryText: message });
                this.state.expression = "neutral";
                this.state.ring = "idle";
                return;
            }
            // A config failure carries the server's own friendly explanation (cfg.answer).
            this.finishBuddy({
                text: configured
                    ? _t("Se me cortó la conexión. Probá de nuevo en unos segundos.")
                    : error?.userMessage || _t("No pude iniciar la conversación. Probá de nuevo en unos segundos."),
                retryText: message,
            });
            this.state.expression = "doubt";
            this.state.ring = "doubt";
        } finally {
            if (epoch === this._epoch && !this._destroyed) {
                this.state.loading = false;
                this.state.streaming = false;
                this._wantInputFocus = true; // after the render that shows the answer (see onPatched)
            }
        }
    }

    /** «Detener»: cancel the answer being written (the server releases the question; nothing more is spent). */
    stop() {
        if (!this.state.loading || this._destroyed) {
            return;
        }
        this._stopped = true;
        this.abortStream();
    }

    // ------------------------------------------------------------------ streaming
    /**
     * Ask the buddy. Streams the answer through /buddy/chat_stream (text shows up as it is written) and resolves to
     * the same object /buddy/chat returns. Falls back, ONCE and with the same request id (the server's
     * idempotency prevents a double charge), to the plain JSON route when the browser cannot stream or when the
     * stream fails before a single byte arrived (a proxy that refuses it, a 404...).
     */
    async ask(message, requestId, epoch) {
        const body = { message, session_generation: this.sessionGeneration, request_id: requestId };
        const streamed = await this.streamChat(body, epoch);
        if (streamed) {
            return streamed;
        }
        if (epoch !== this._epoch || this._destroyed) {
            return {};
        }
        return rpc("/buddy/chat", body);
    }

    /** The final response of a stream, or null when the caller should fall back to the JSON route. */
    async streamChat(body, epoch) {
        if (typeof fetch !== "function" || typeof AbortController !== "function") {
            return null;
        }
        this.abortStream();
        const controller = new AbortController();
        this._abort = controller;
        let response;
        try {
            response = await fetch("/buddy/chat_stream", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    Accept: "text/event-stream",
                    "X-CSRF-Token": globalThis.odoo?.csrf_token || "",
                },
                body: JSON.stringify(body),
                signal: controller.signal,
            });
        } catch (error) {
            if (controller.signal.aborted) {
                throw error;
            }
            return null;
        }
        const type = response.headers?.get("content-type") || "";
        if (!response.ok || !response.body || !type.includes("text/event-stream")) {
            try {
                await response.body?.cancel();
            } catch {
                // Nothing to release.
            }
            return null;
        }
        const reader = response.body.getReader();
        const parser = new SSEParser();
        let received = false;
        let final = null;
        try {
            while (!final) {
                let chunk;
                try {
                    chunk = await reader.read();
                } catch (error) {
                    if (controller.signal.aborted || received) {
                        throw error;
                    }
                    return null;
                }
                if (chunk.done) {
                    break;
                }
                if (epoch !== this._epoch || this._destroyed) {
                    controller.abort();
                    return {};
                }
                received = true;
                for (const event of parser.feed(chunk.value)) {
                    final = this.onStreamEvent(event, epoch) || final;
                }
            }
        } finally {
            if (this._abort === controller) {
                this._abort = null;
            }
            reader.cancel().catch(() => {});
        }
        if (final) {
            return final;
        }
        if (!received) {
            return null;
        }
        throw new Error("The stream ended without an answer");
    }

    /** One SSE event: text is shown right away; a `done` or `error` event is the final response. */
    onStreamEvent(event, epoch) {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch {
            return null;
        }
        if (!data || typeof data !== "object") {
            return null;
        }
        if (event.event === "delta") {
            if (typeof data.text === "string" && data.text) {
                this.appendLive(data.text, epoch);
            }
            return null;
        }
        if (event.event === "done") {
            if (typeof data.answer !== "string") {
                throw new Error("Malformed final event");
            }
            return data;
        }
        if (event.event === "error") {
            return { ...data, error: data.error || "provider_error" };
        }
        return null;
    }

    /** New text of the answer: the first piece replaces the "typing" dots with a growing message. */
    appendLive(text, epoch) {
        if (epoch !== this._epoch || this._destroyed) {
            return;
        }
        this._liveText = (this._liveText || "") + text;
        if (!this._live) {
            this.state.messages.push({
                id: ++this._seq,
                role: "buddy",
                text: "",
                blocks: [],
                sources: [],
                ticket: null,
                contact: null,
                ticketResult: null,
                creating: false,
                retryText: "",
                requestKey: null,
                ...FEEDBACK_DEFAULTS,
                streaming: true,
                epoch: this._epoch,
                sessionGeneration: this.sessionGeneration,
            });
            this._live = this.state.messages.at(-1);
            this.state.streaming = true;
        }
        if (this._frame === undefined || this._frame === null) {
            // At most one re-render per animation frame, however many fragments arrive.
            this._frame = requestFrame(() => {
                this._frame = null;
                this.renderLive();
            });
        }
    }

    renderLive() {
        const live = this._live;
        if (!live || this._destroyed) {
            return;
        }
        const el = this.messagesRef.el;
        if (!el || el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_PX) {
            this._stickScroll = true;
        }
        live.text = this._liveText;
        live.blocks = parseMarkdown(this._liveText);
    }

    /** Forget the message being written (it is replaced, dropped or the stream was aborted). */
    resetLive() {
        if (this._frame !== undefined && this._frame !== null) {
            cancelFrame(this._frame);
        }
        this._frame = null;
        this._live = null;
        this._liveText = "";
        this.state.streaming = false;
    }

    abortStream() {
        if (this._abort) {
            this._abort.abort();
            this._abort = null;
        }
    }

    /** Show the final (or error) message: it takes the place of the text that was being written, if any. */
    finishBuddy(extra) {
        const live = this._live;
        const present = live && this.state.messages.includes(live);
        const box = this.messagesRef.el;
        if (box && box.scrollHeight - box.scrollTop - box.clientHeight <= STICK_PX) {
            this._stickScroll = true; // the vote row and the notes under the answer must not end up out of sight
        }
        this.resetLive();
        if (!present) {
            this.pushBuddy(extra);
            return;
        }
        Object.assign(live, {
            sources: [],
            ticket: null,
            contact: null,
            ticketResult: null,
            creating: false,
            retryText: "",
            requestKey: null,
            ...FEEDBACK_DEFAULTS,
            ...extra,
            blocks: parseMarkdown(extra.text),
            streaming: false,
            epoch: this._epoch,
            sessionGeneration: this.sessionGeneration,
        });
    }

    /** Re-send the question that failed, without duplicating it in the conversation. */
    async retry(msg) {
        if (!msg.retryText || this.state.loading || this.state.forgetting || this._destroyed ||
                msg.epoch !== this._epoch || msg !== this.lastMessage) {
            return;
        }
        const index = this.state.messages.indexOf(msg);
        const previous = this.state.messages[index - 1];
        const dropQuestion = previous?.role === "user" && previous.text === msg.retryText;
        this.state.messages.splice(dropQuestion ? index - 1 : index, dropQuestion ? 2 : 1);
        await this.send(msg.retryText);
    }

    handleSessionChanged(res) {
        this._epoch++;
        this.abortStream();
        this.resetLive();
        this.sessionGeneration = Number.isInteger(res.session_generation) ? res.session_generation : null;
        if (this.sessionGeneration === null) {
            this._configPromise = null;
        }
        this.state.messages = [];
        this.state.restored = false;
        this.state.loading = false;
        this.state.expression = "doubt";
        this.state.ring = "doubt";
        this.pushBuddy({ text: res.answer || _t("La conversación cambió. Volvé a enviar tu consulta.") });
    }

    async createTicket(msg) {
        if (this.state.forgetting || this._destroyed || msg.epoch !== this._epoch ||
                !this.state.messages.includes(msg) || !msg.ticket || msg.creating ||
                (msg.ticketResult && !msg.ticketResult.error)) {
            return;
        }
        msg.creating = true;
        const epoch = this._epoch;
        try {
            // Keep this key after an ambiguous network failure: retries recover the same ticket.
            msg.requestKey ||= newRequestKey();
            const res = await rpc("/buddy/ticket", {
                title: msg.ticket.title,
                description: msg.ticket.description,
                request_key: msg.requestKey,
                session_generation: msg.sessionGeneration,
            });
            if (epoch !== this._epoch || this._destroyed) {
                return;
            }
            if (res?.error === "session_changed") {
                this.handleSessionChanged(res);
                return;
            }
            // A refusal that explains itself (the daily cap of tickets) shows its reason instead of a generic failure.
            msg.ticketResult = res && res.id ? res : { error: true, message: typeof res?.message === "string" ? res.message : "" };
        } catch {
            if (epoch === this._epoch && !this._destroyed) {
                msg.ticketResult = { error: true };
            }
        } finally {
            msg.creating = false;
        }
    }

    ticketUrl(result) {
        return `/odoo/action-buddy_ia.buddy_ticket_action/${Number(result.id)}`;
    }

    /** A click on the ticket link: with ctrl, cmd, shift or the middle button the browser opens it in a new tab as usual. */
    onTicketLink(ev, result) {
        if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey || ev.button > 0) {
            return;
        }
        ev.preventDefault();
        return this.openTicket(result);
    }

    /**
     * Open the ticket inside Odoo (no page reload). On a computer it opens in a dialog over the page, so the chat stays
     * where it was; on phones the panel would cover it, so it closes and the ticket takes the whole screen.
     */
    openTicket(result) {
        const phone = Boolean(window.matchMedia?.("(max-width: 480px)").matches);
        if (phone) {
            this.state.open = false;
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "buddy.ticket",
            res_id: Number(result.id),
            views: [[false, "form"]],
            target: phone ? "current" : "new",
        });
    }

    // ------------------------------------------------------------------ feedback (👍 / 👎) and confidence
    get feedbackReasons() {
        return [
            { id: "not_asked", label: _t("No era lo que pregunté") },
            { id: "wrong", label: _t("Dato incorrecto o desactualizado") },
            { id: "incomplete", label: _t("Incompleta") },
            { id: "other", label: _t("Otro") },
        ];
    }

    /** A short, calm note under the answer when the documents disagree or back it weakly (empty otherwise). */
    confidenceNote(msg) {
        // The contact card only exists when there is somebody to name: without it the note does not point at a person.
        if (msg.conflict) {
            return msg.contact
                ? _t("Los documentos no coinciden en esto. Confirmalo con la persona de contacto antes de usarlo.")
                : _t("Los documentos no coinciden en esto. Confirmalo antes de usarlo.");
        }
        if (msg.confidence === "low") {
            return msg.contact
                ? _t("Hay poco respaldo en los documentos para esto. Confirmalo con la persona de contacto.")
                : _t("Hay poco respaldo en los documentos para esto. Confirmalo antes de usarlo.");
        }
        return "";
    }

    canVote(msg) {
        return Boolean(msg.feedbackToken) && !msg.streaming && !this._destroyed && !this.state.forgetting &&
            msg.epoch === this._epoch && this.state.messages.includes(msg);
    }

    /**
     * 👍 / 👎. The same thumb twice takes the vote back; the other thumb changes it. A 👎 is recorded at once and then
     * offers (optionally) a reason and the box «include my question»: skipping the form still leaves the vote.
     */
    async vote(msg, value) {
        if (!this.canVote(msg) || msg.fbBusy) {
            return;
        }
        const next = msg.feedback === value ? "none" : value;
        const res = await this.postFeedback(msg, { vote: next });
        if (!res?.ok) {
            return;
        }
        msg.feedback = res.vote;
        msg.fbOpen = res.vote === "down";
        if (msg.fbOpen) {
            msg.fbStatus = _t("Anotamos tu voto. Si querés, contanos qué pasó.");
            this._feedbackFocus = msg.id;
        } else {
            Object.assign(msg, {
                fbReason: "",
                fbInclude: false,
                fbStatus: res.vote === "up" ? _t("¡Gracias por tu opinión!") : _t("Voto retirado."),
            });
        }
    }

    /** «Enviar» in the reason form: the same vote again, now with the reason and the optional question. */
    async submitFeedback(msg) {
        if (!this.canVote(msg) || msg.fbBusy || msg.feedback !== "down") {
            return;
        }
        const body = { vote: "down" };
        if (FEEDBACK_REASON_IDS.includes(msg.fbReason)) {
            body.reason = msg.fbReason;
        }
        if (!this.state.keepQuestions && msg.fbInclude) {
            body.include_question = true;
        }
        const res = await this.postFeedback(msg, body);
        if (res?.ok) {
            msg.fbOpen = false;
            msg.fbStatus = _t("¡Gracias! Lo vamos a tener en cuenta.");
            this._feedbackRefocus = msg.id;
        }
    }

    closeFeedback(msg) {
        msg.fbOpen = false;
        msg.fbStatus = msg.feedback === "down" ? _t("Anotamos tu voto.") : "";
        this._feedbackRefocus = msg.id;
    }

    onFeedbackKeydown(ev, msg) {
        if (ev.key === "Escape" && !ev.isComposing) {
            // Closes the little form, not the whole chat.
            ev.preventDefault();
            ev.stopPropagation();
            this.closeFeedback(msg);
        }
    }

    setFeedbackReason(msg, id) {
        msg.fbReason = FEEDBACK_REASON_IDS.includes(id) ? id : "";
    }

    setFeedbackInclude(msg, ev) {
        msg.fbInclude = Boolean(ev.target.checked);
    }

    /** One call to the voting route. Errors become a short message under the buttons; the old vote stays on screen. */
    async postFeedback(msg, fields) {
        const epoch = this._epoch;
        msg.fbBusy = true;
        msg.fbError = "";
        try {
            const res = await sendFeedback({ token: msg.feedbackToken, ...fields });
            if (epoch !== this._epoch || this._destroyed) {
                return null;
            }
            if (!res.ok) {
                msg.fbError = res.message || _t("No pude guardar tu opinión. Probá de nuevo.");
            }
            return res;
        } catch {
            if (epoch === this._epoch && !this._destroyed) {
                msg.fbError = _t("No pude guardar tu opinión. Probá de nuevo.");
            }
            return null;
        } finally {
            msg.fbBusy = false;
        }
    }

    /** Copy the contact's e-mail (the card also has a link, but not everybody has a mail program). */
    async copyEmail(msg) {
        try {
            await navigator.clipboard.writeText(msg.contact.contact);
            msg.copied = true;
            setTimeout(() => {
                msg.copied = false;
            }, 2000);
        } catch {
            // No clipboard permission: the address is visible on the card anyway.
        }
    }

    // ------------------------------------------------------------------ forget history
    askForget() {
        if (this.state.forgetting || this._destroyed) {
            return;
        }
        this.interruptGesture();
        this.state.confirmForget = true;
        this.state.forgetError = "";
        clearTimeout(this._focusTimer);
        // Focus the safe option (Cancel) once the confirmation is rendered.
        this._focusTimer = setTimeout(() => this.confirmNoRef.el?.focus(), 0);
    }

    cancelForget() {
        this.state.confirmForget = false;
        clearTimeout(this._focusTimer);
        this._focusTimer = setTimeout(() => this.inputRef.el?.focus(), 0);
    }

    async confirmForgetNow() {
        this.state.confirmForget = false;
        await this.forget();
    }

    async forget() {
        if (this.state.forgetting || this._destroyed) {
            return;
        }
        this._epoch++;
        // "New conversation" cancels the answer being written (closing the panel does not).
        this.abortStream();
        this.resetLive();
        this.state.forgetting = true;
        this.state.forgetError = "";
        this.state.loading = false;
        try {
            await this.ensureConfig();
            if (this._destroyed) {
                return;
            }
            const res = await rpc("/buddy/forget");
            if (this._destroyed) {
                return;
            }
            if (res?.ok !== true || !Number.isInteger(res.session_generation) || res.session_generation < 0) {
                throw new Error("Deletion was not confirmed");
            }
            this.sessionGeneration = res.session_generation;
            this.state.messages = [];
            this.state.restored = false;
            this.state.input = "";
            this.state.expression = "happy";
            this.state.ring = "idle";
            this.pushBuddy({ text: _t("Listo, borré nuestra conversación. ¿En qué te ayudo?") });
        } catch {
            if (!this._destroyed) {
                this.state.forgetError = _t("No pude confirmar el borrado del historial. Probá de nuevo; puede seguir guardado.");
                this.state.expression = "doubt";
                this.state.ring = "doubt";
                for (const msg of this.state.messages) {
                    msg.epoch = this._epoch;
                }
            }
        } finally {
            if (!this._destroyed) {
                this.state.forgetting = false;
            }
        }
    }
}

registry.category("main_components").add("buddy_ia.Buddy", { Component: Buddy });
