// El test inyecta la implementación en globalThis.__rpc y puede inspeccionar globalThis.__rpcCalls.
export async function rpc(route, params = {}) {
    (globalThis.__rpcCalls ??= []).push({ route, params });
    return globalThis.__rpc(route, params);
}
