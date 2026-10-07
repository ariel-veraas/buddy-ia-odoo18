/** @odoo-module **/

import { Component } from "@odoo/owl";
import {
    ACCESSORIES,
    BUDDIES,
    GESTURES,
    cleanAccessory,
    cleanBuddy,
    cleanExpression,
    cleanFace,
    cleanRing,
} from "./buddy_registry";

let uidCounter = 0;

/**
 * Avatar del buddy. El dibujo vive en los templates `buddy_ia.Avatar.<buddy>` (xml/buddy_avatar_*.xml, generados por
 * tools/buddy_art.py); este componente solo elige qué mostrar. Todo valor se filtra contra buddy_registry.js.
 * - buddy: kitsune | mochi, o un id sumado por otro módulo (ver BUDDIES)
 * - accessory: none | mustache | wizard | shades | headphones | party (accesorio fijo)
 * - expression: neutral | happy | thinking | surprised | doubt | wink (el contrato con la IA)
 * - ring: idle | listening | thinking | ok | doubt  (color/estado del aro luminoso)
 * - gesture: gesto de reposo en curso (ver GESTURES). Solo se ve con el aro en reposo: apenas el chat piensa o
 *   escucha, el gesto desaparece aunque quien lo puso todavía no lo haya cortado.
 * - head: true = solo la cabeza (lanzador y cabecera del panel), sin cuerpo.
 */
export class BuddyAvatar extends Component {
    static template = "buddy_ia.Avatar";
    static props = {
        expression: { type: String, optional: true },
        ring: { type: String, optional: true },
        waving: { type: Boolean, optional: true },
        size: { type: Number, optional: true },
        buddy: { type: String, optional: true },
        accessory: { type: String, optional: true },
        gesture: { type: [String, { value: null }], optional: true },
        head: { type: Boolean, optional: true },
    };
    static defaultProps = {
        expression: "neutral", ring: "idle", waving: false, size: 96,
        buddy: "mochi", accessory: "none", gesture: null, head: false,
    };

    setup() {
        // Ids únicos por instancia: el avatar aparece más de una vez (lanzador y cabecera) y los
        // gradientes/filtros del SVG se referencian por id.
        this.uid = ++uidCounter;
    }

    get buddyId() {
        return cleanBuddy(this.props.buddy);
    }

    /** [x, y, ancho, alto] del lienzo: la cabeza sola (lanzador y cabecera) o el cuerpo entero. */
    get view() {
        return BUDDIES[this.buddyId].view[this.props.head ? "head" : "full"];
    }

    get viewBox() {
        return this.view.join(" ");
    }

    // `size` es lo que mediria el lienzo estandar de 200 de ancho: todos los buddys se dibujan a la misma escala.
    get svgWidth() {
        return (this.props.size * this.view[2]) / 200;
    }

    get svgHeight() {
        return (this.props.size * this.view[3]) / 200;
    }

    get buddyTemplate() {
        return BUDDIES[this.buddyId].template;
    }

    /** El gesto en curso, o null: no se muestra si el aro no está en reposo ni si no es de este buddy. */
    get gestureDef() {
        const id = this.props.gesture;
        if (cleanRing(this.props.ring) !== "idle" || typeof id !== "string" || !Object.hasOwn(GESTURES, id)) {
            return null;
        }
        const gesture = GESTURES[id];
        return gesture.buddies && !gesture.buddies.includes(this.buddyId) ? null : gesture;
    }

    get gestureId() {
        return this.gestureDef ? this.props.gesture : null;
    }

    /** La cara que se dibuja: la del gesto si la tiene; si no, la expresión. Nunca un valor desconocido. */
    get face() {
        return cleanFace(this.gestureDef?.face || cleanExpression(this.props.expression));
    }

    get fxs() {
        return this.gestureDef?.fx || [];
    }

    get rootClass() {
        const classes = [`ring-${cleanRing(this.props.ring)}`, `buddy-${this.buddyId}`];
        if (!BUDDIES[this.buddyId].disc) {
            classes.push("is-loose");
        }
        if (this.props.head) {
            classes.push("is-head");
        }
        if (this.gestureId) {
            classes.push(`g-${this.gestureId}`);
        }
        return classes.join(" ");
    }

    /** Accesorio fijo + accesorio temporal del gesto (si no se pisan en el cuerpo), con su posición ya calculada. */
    get accessoryList() {
        const fixed = cleanAccessory(this.props.accessory);
        const list = [];
        if (fixed !== "none") {
            list.push(this._accessory(fixed, false));
        }
        const temp = this.gestureDef?.accessory;
        if (temp && ACCESSORIES[temp].slot !== ACCESSORIES[fixed].slot) {
            list.push(this._accessory(temp, true));
        }
        return list;
    }

    _accessory(id, temp) {
        const spot = BUDDIES[this.buddyId].acc[ACCESSORIES[id].place];
        const [x, y, a, b] = spot;
        const transform = id === "headphones"
            ? `translate(${x} ${y}) scale(${a} ${b})`
            : `translate(${x} ${y}) rotate(${b}) scale(${a})`;
        return { id, transform, className: temp ? "bd-acc is-temp" : "bd-acc" };
    }

    /** Posición de un efecto (signo de pregunta, gota, Zzz...) como valor del atributo transform. */
    anchor(name) {
        const [x, y] = BUDDIES[this.buddyId].anchors[name] || [100, 40];
        return `translate(${x} ${y})`;
    }
}
