// Stand-in for Odoo's standardFieldProps (the props every field widget receives).
export const standardFieldProps = {
    name: String,
    record: Object,
    readonly: { type: Boolean, optional: true },
};
