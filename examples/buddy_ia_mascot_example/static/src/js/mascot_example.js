/** @odoo-module **/

// 3. Register the drawing in the widget registry (buddy_ia's assets load first because this module depends on it).
import { BUDDIES } from "@buddy_ia/js/buddy_registry";

BUDDIES.mascot_example = {
    template: "buddy_ia.Avatar.mascot_example", // the t-name inside the XML file
    disc: false, // true only if the head sits inside a dark disc with a ring, like a round logo
    view: { head: [24, 0, 152, 140], full: [24, 0, 152, 148] }, // viewBox of the head alone and of the whole body
    anchors: { top: [80, 8], temple: [150, 62], forehead: [66, 58], zzz: [152, 22], nose: [110, 104] }, // where effects go
    acc: { hat: [100, 40, 0.78, 0], eyes: [100, 90, 0.9, 0], mouth: [100, 106, 0.6, 0], phones: [100, 88, 1.22, 1.0] }, // accessories
};

// Optional: a gesture only your mascot does. (Its CSS goes in a .g-<id> rule; see docs/CUSTOMIZE.md.)
// GESTURES.my_gesture = { group: "expressions", ms: 2400, weight: 2, buddies: ["mascot_example"] };
