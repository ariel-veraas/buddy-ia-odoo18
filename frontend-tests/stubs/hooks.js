// Stand-in for Odoo's useService: tests can provide fakes in globalThis.__services.
export function useService(name) {
    const services = (globalThis.__services ??= {});
    return new Proxy({}, {
        get: (_target, method) => (...args) => services[name]?.[method]?.(...args),
    });
}
