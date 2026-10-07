// Stand-in for Odoo's _t. Tests can install a dictionary in globalThis.__translations to prove that a
// string goes through the translation function (the key is the Spanish source text).
export function _t(term, ...values) {
    const dictionary = globalThis.__translations || {};
    let text = Object.hasOwn(dictionary, term) ? dictionary[term] : term;
    const [first] = values;
    if (first && typeof first === "object") {
        return text.replace(/%\((\w+)\)s/g, (_, key) => String(first[key]));
    }
    let index = 0;
    return text.replace(/%s/g, () => String(values[index++]));
}
