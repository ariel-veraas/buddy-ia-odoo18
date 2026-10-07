const categories = {};
export const registry = {
    category(name) {
        categories[name] ??= {
            entries: {},
            add(key, value) { this.entries[key] = value; return this; },
            get(key) { return this.entries[key]; },
        };
        return categories[name];
    },
};
