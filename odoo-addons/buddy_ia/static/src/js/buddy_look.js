/** @odoo-module **/

import { Component, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";
import { BuddyAvatar } from "./buddy_avatar";
import {
    BUDDIES,
    EXPRESSIONS,
    GESTURES,
    GESTURE_GROUPS,
    availableGestures,
    cleanAccessory,
    cleanBuddy,
} from "./buddy_registry";
import { themeVars } from "./buddy";

/**
 * Widgets of the settings screen (Ajustes > Buddy IA > Apariencia): they read and write the fields of the form that is
 * still being edited, so everything changes on screen BEFORE saving.
 *  - buddy_character_picker: the characters as cards (field `buddy_character`).
 *  - buddy_chips: any selection field as a row of buttons (accessory, frequency).
 *  - buddy_preview: a big live preview that also lets the admin try every expression and gesture.
 */

/** The look of the form being edited: always filtered, never trusted. */
export function formLook(record) {
    const data = record.data || {};
    return {
        buddy: cleanBuddy(data.buddy_character),
        accessory: cleanAccessory(data.buddy_accessory),
        style: themeVars(typeof data.buddy_primary_color === "string" ? data.buddy_primary_color.trim() : ""),
    };
}

function selectionOf(props) {
    return (props.record.fields?.[props.name]?.selection || []).map(([value, label]) => ({ value, label }));
}

export class BuddyCharacterPicker extends Component {
    static template = "buddy_ia.CharacterPicker";
    static components = { BuddyAvatar };
    static props = { ...standardFieldProps };

    get look() {
        return formLook(this.props.record);
    }

    get options() {
        // Only characters the browser knows how to draw (the server may offer more after a partial upgrade).
        return selectionOf(this.props).filter((option) => Object.hasOwn(BUDDIES, option.value));
    }

    get selected() {
        return cleanBuddy(this.props.record.data[this.props.name]);
    }

    get groupLabel() {
        return _t("Personaje del buddy");
    }

    choose(value) {
        if (!this.props.readonly) {
            this.props.record.update({ [this.props.name]: value });
        }
    }
}

export class BuddyChips extends Component {
    static template = "buddy_ia.Chips";
    static props = { ...standardFieldProps };

    get options() {
        return selectionOf(this.props);
    }

    get selected() {
        return this.props.record.data[this.props.name];
    }

    choose(value) {
        if (!this.props.readonly) {
            this.props.record.update({ [this.props.name]: value });
        }
    }
}

/** Texts for the preview. One literal per string so the translation exporter finds them. */
export function expressionLabels() {
    return {
        neutral: _t("Normal"),
        happy: _t("Contento"),
        thinking: _t("Pensando"),
        surprised: _t("Sorprendido"),
        doubt: _t("Duda"),
        wink: _t("Guiño"),
    };
}

export function gestureLabels() {
    return {
        blink: _t("Parpadeo"),
        look: _t("Mira alrededor"),
        sparkle: _t("Ojos brillantes"),
        heart: _t("Ojos de corazón"),
        sweat: _t("Gota de sudor"),
        vein: _t("Venita de enojo"),
        tears: _t("Lagrimitas"),
        dizzy: _t("Mareo"),
        question: _t("Signo de pregunta"),
        exclaim: _t("Signo de exclamación"),
        tail: _t("Mueve la cola"),
        ears: _t("Mueve las orejas"),
        bounce: _t("Rebota"),
        jiggle: _t("Se bambolea"),
        spin: _t("Aro giratorio"),
        mustache: _t("Se pone un bigote"),
        wizard: _t("Se pone el sombrero de mago"),
        shades: _t("Se pone lentes de sol"),
        yawn: _t("Bosteza"),
        stretch: _t("Se estira"),
        sleep: _t("Se duerme"),
    };
}

export function groupLabels() {
    return {
        expressions: _t("Expresiones"),
        accessories: _t("Accesorios"),
        sleep: _t("Sueño"),
    };
}

export class BuddyPreview extends Component {
    static template = "buddy_ia.Preview";
    static components = { BuddyAvatar };
    static props = { ...standardFieldProps };

    setup() {
        this.state = useState({ expression: "neutral", gesture: null, waving: false });
        onWillUnmount(() => clearTimeout(this._timer));
    }

    get look() {
        return formLook(this.props.record);
    }

    /** The character is drawn about 200 px wide whatever its canvas (Mochi is narrower than Kitsune). */
    get previewSize() {
        return (200 * 200) / BUDDIES[this.look.buddy].view.full[2];
    }

    get expressions() {
        const labels = expressionLabels();
        return EXPRESSIONS.map((id) => ({ id, label: labels[id] }));
    }

    /** The gestures this character can do with the accessory chosen right now, grouped as the admin sees them. */
    get gestureGroups() {
        const { buddy, accessory } = this.look;
        const available = availableGestures({ buddy, accessory, groups: GESTURE_GROUPS });
        const labels = gestureLabels();
        const groups = groupLabels();
        return GESTURE_GROUPS.map((group) => ({
            id: group,
            label: groups[group],
            gestures: available.filter((id) => GESTURES[id].group === group).map((id) => ({ id, label: labels[id] })),
        })).filter((group) => group.gestures.length);
    }

    setExpression(id) {
        clearTimeout(this._timer);
        this.state.gesture = null;
        this.state.expression = id;
    }

    play(id) {
        clearTimeout(this._timer);
        this.state.gesture = null;
        // One tick later so a gesture that is played twice in a row restarts its animation.
        this._timer = setTimeout(() => {
            this.state.gesture = id;
            this._timer = setTimeout(() => {
                this.state.gesture = null;
            }, GESTURES[id].ms);
        }, 30);
    }

    toggleWave() {
        this.state.waving = !this.state.waving;
    }
}

registry.category("fields").add("buddy_character_picker", {
    component: BuddyCharacterPicker,
    displayName: _t("Elegir el personaje del buddy"),
    supportedTypes: ["selection"],
});
registry.category("fields").add("buddy_chips", {
    component: BuddyChips,
    displayName: _t("Opciones como botones"),
    supportedTypes: ["selection"],
});
registry.category("fields").add("buddy_preview", {
    component: BuddyPreview,
    displayName: _t("Vista previa del buddy"),
    supportedTypes: ["selection"],
});
