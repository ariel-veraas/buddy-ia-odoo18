/** @odoo-module **/

import { GESTURES, IDLE_FREQUENCIES, OPEN_QUIET_SECONDS, cleanFrequency, pickGesture } from "./buddy_registry";

/**
 * Programador de gestos en reposo. Sin DOM ni OWL: recibe todo lo que necesita por parámetro, así se prueba con
 * relojes falsos.
 *
 * Cómo funciona (los números están en IDLE_FREQUENCIES y en docs/ADMIN-GUIDE.md):
 *  1. Hay UN solo temporizador de espera. Al arrancar (o al reanudar) espera `idle` segundos con azar de hasta +25%.
 *  2. Al vencer, si hubo actividad hace menos de lo necesario (`quietSeconds()`), vuelve a esperar lo que falta.
 *     No gasta el turno: la gente que está usando el buddy no ve gestos.
 *  3. Si no está permitido actuar (`canPlay()`: chat ocupado, pestaña oculta...), salta este turno y espera el
 *     intervalo siguiente.
 *  4. Si puede, elige un gesto con `pick()`, lo anuncia con `onGesture(id)`, y a los `ms` del gesto llama a
 *     `onEnd()` y espera un intervalo al azar entre el mínimo y el máximo (el "jitter").
 *  4b. El aviso de tareas (E4b) usa este mismo turno: si hay `nudge` y devuelve algo, `onNudge(offer)` muestra el globito y el turno se
 *      gasta en el gesto suave que traiga (o sin gesto, si el administrador apagó ese grupo). Pasa por los mismos filtros que cualquier
 *      gesto: quietud, `canPlay()`, pausa (pestaña oculta, ocultar el buddy, reducir movimiento, gestos apagados). Cuándo hay algo que decir
 *      y cuántas veces por día lo decide quien lo da, no el programador.
 *  5. `interrupt()` corta un gesto en curso al instante (el chat empezó a pensar) y vuelve a esperar `idle`.
 *  `pause()` apaga todo (pestaña oculta, ocultar el buddy, reducir movimiento); `resume()` reanuda.
 * Los temporizadores se limpian con `stop()`: llamarlo al desmontar el componente.
 */
export class IdleScheduler {
    /**
     * @param {object} options
     * @param {() => {frequency: string}} options.config      frecuencia vigente
     * @param {() => boolean} options.canPlay                 ¿está permitido actuar ahora?
     * @param {() => number} options.quietSeconds             segundos de quietud que se exigen ahora
     * @param {() => string|null} options.pick                devuelve el id del gesto (o null)
     * @param {(id: string) => void} options.onGesture
     * @param {() => void} options.onEnd
     * @param {() => ({gesture: string|null}|null)} [options.nudge]   ¿hay un aviso para dar en este turno? (null = no)
     * @param {(offer: object) => void} [options.onNudge]               se llama cuando el aviso sale
     * @param {() => number} [options.now]
     * @param {() => number} [options.random]
     */
    constructor({ config, canPlay, quietSeconds, pick, onGesture, onEnd, nudge = null, onNudge = () => {}, now = () => Date.now(), random = Math.random }) {
        Object.assign(this, { config, canPlay, quietSeconds, pick, onGesture, onEnd, nudge, onNudge, now, random });
        this.current = null;
        this.paused = true;
        this.lastActivity = this.now();
        this._wait = null;
        this._end = null;
    }

    get frequency() {
        return IDLE_FREQUENCIES[cleanFrequency(this.config()?.frequency)];
    }

    /** Anota que alguien está usando el buddy (barato: solo guarda la hora). */
    touch() {
        this.lastActivity = this.now();
    }

    /** Arranca (o reanuda). No hace nada si ya está corriendo. */
    start() {
        if (!this.paused) {
            return;
        }
        this.paused = false;
        this.lastActivity = this.now();
        this._arm(this._firstDelay());
    }

    resume() {
        this.start();
    }

    /** Apaga todo y deja el buddy quieto; un gesto en curso se corta. */
    pause() {
        this.paused = true;
        this._clear();
        this._finish(false);
    }

    stop() {
        this.pause();
    }

    /** Corta un gesto en curso al instante (el chat pasó a pensar o responder) y espera `idle` de nuevo. */
    interrupt() {
        if (this.paused) {
            return;
        }
        this.touch();
        this._clear();
        this._finish(false);
        this._arm(this._firstDelay());
    }

    /** La configuración cambió (frecuencia): empezar de cero con los valores nuevos. */
    reset() {
        if (this.paused) {
            return;
        }
        this._clear();
        this._finish(false);
        this._arm(this._firstDelay());
    }

    _firstDelay() {
        return this.frequency.idle * 1000 * (1 + this.random() * 0.25);
    }

    _gap() {
        const [min, max] = this.frequency.gap;
        return (min + this.random() * (max - min)) * 1000;
    }

    _arm(ms) {
        clearTimeout(this._wait);
        this._wait = setTimeout(() => this._fire(), Math.max(1000, ms));
    }

    _clear() {
        clearTimeout(this._wait);
        clearTimeout(this._end);
        this._wait = null;
        this._end = null;
    }

    _fire() {
        this._wait = null;
        if (this.paused || this.current) {
            return;
        }
        const need = this.quietSeconds() * 1000;
        const quiet = this.now() - this.lastActivity;
        if (quiet < need) {
            this._arm(need - quiet + 250);
            return;
        }
        const allowed = this.canPlay();
        const offer = allowed && this.nudge ? this.nudge() : null;
        if (offer) {
            this.onNudge(offer);
            const soft = offer.gesture;
            if (soft && Object.hasOwn(GESTURES, soft)) {
                this._play(soft);
            } else {
                this._arm(this._gap());
            }
            return;
        }
        const id = allowed ? this.pick() : null;
        if (!id || !Object.hasOwn(GESTURES, id)) {
            this._arm(this._gap());
            return;
        }
        this._play(id);
    }

    _play(id) {
        this.current = id;
        this.onGesture(id);
        this._end = setTimeout(() => {
            this._end = null;
            this._finish(true);
        }, GESTURES[id].ms);
    }

    _finish(rearm) {
        if (this.current === null) {
            return;
        }
        this.current = null;
        this.onEnd();
        if (rearm && !this.paused) {
            this._arm(this._gap());
        }
    }
}

/** El tiempo de quietud que se exige: con el chat abierto, bastante más. */
export function quietSecondsFor({ open, frequency }) {
    return open ? Math.max(OPEN_QUIET_SECONDS, IDLE_FREQUENCIES[cleanFrequency(frequency)].idle) : IDLE_FREQUENCIES[cleanFrequency(frequency)].idle;
}

export { pickGesture };
