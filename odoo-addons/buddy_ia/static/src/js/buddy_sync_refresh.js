/** @odoo-module **/

import { Component, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";

// How often the screen of a folder that is synchronizing asks the server how far it got.
export const REFRESH_MS = 5000;
const SYNCING = new Set(["queued", "syncing"]);

/** Reload only while the synchronization is running and the person has nothing unsaved (a reload would drop it). */
export function shouldRefresh(state, dirty) {
    return SYNCING.has(state) && !dirty;
}

/**
 * Widget for the «state» field of a Drive folder: while it is waiting or synchronizing, it reloads the record every
 * few seconds, so the administrator sees the folder finish without having to refresh the page. When the folder is
 * done the form hides it and the timer stops with it.
 */
export class BuddySyncRefresh extends Component {
    static template = "buddy_ia.SyncRefresh";
    static props = { ...standardFieldProps };

    setup() {
        this._busy = false;
        onMounted(() => {
            this._timer = setInterval(() => this.tick(), REFRESH_MS);
        });
        onWillUnmount(() => clearInterval(this._timer));
    }

    get label() {
        return _t("Esta pantalla se actualiza sola.");
    }

    async tick() {
        const record = this.props.record;
        if (this._busy || !shouldRefresh(record.data[this.props.name], await record.isDirty())) {
            return;
        }
        this._busy = true;
        try {
            await record.load();
        } catch {
            // The server may be busy with the very synchronization: the next tick tries again.
        } finally {
            this._busy = false;
        }
    }
}

registry.category("fields").add("buddy_sync_refresh", {
    component: BuddySyncRefresh,
    displayName: _t("Estado de la sincronización (se actualiza solo)"),
    supportedTypes: ["selection"],
});
