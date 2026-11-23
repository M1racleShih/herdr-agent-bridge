// Use the same public Undici dependency and proxy behavior as Pi 1.5's CLI.
// The caller owns the returned dispatcher and should await its destruction.
import { EventEmitter } from 'node:events';
import { realpathSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import { isAbsolute } from 'node:path';

const originalFetch = globalThis.fetch;
let installedFetch;
let hasInstalledFetch = false;
const ignoreDispatcherError = () => {};

function settingsForNetwork(settings) {
    if (!settings || typeof settings !== 'object' || Array.isArray(settings)) {
        throw new Error('Invalid Pi network settings');
    }
    const configuredProxy = settings.httpProxy;
    if (configuredProxy != null && typeof configuredProxy !== 'string') {
        throw new Error('Invalid Pi network settings');
    }
    const proxy = configuredProxy?.trim();
    let timeout = settings.httpIdleTimeoutMs;
    if (timeout === undefined) {
        timeout = 300_000;
    } else if (typeof timeout === 'string') {
        const text = timeout.trim();
        if (text.toLowerCase() === 'disabled') {
            timeout = 0;
        } else if (text) {
            timeout = Number(text);
        } else {
            throw new Error('Invalid Pi network settings');
        }
    }
    if (typeof timeout !== 'number' || !Number.isFinite(timeout) || timeout < 0) {
        throw new Error('Invalid Pi network settings');
    }
    return { proxy, timeout: Math.floor(timeout) };
}

function withErrorListener(dispatcher) {
    if (dispatcher instanceof EventEmitter) {
        EventEmitter.prototype.on.call(dispatcher, 'error', ignoreDispatcherError);
    }
    return dispatcher;
}

export function configurePiNetwork(sdkEntry, settings = {}) {
    // Validate before loading Undici, mutating the environment, or constructing
    // any dispatcher. Error strings deliberately omit paths and proxy values.
    let proxy, timeout;
    try {
        ({ proxy, timeout } = settingsForNetwork(settings));
    } catch {
        throw new Error('Invalid Pi network settings');
    }
    let undici;
    try {
        if (typeof sdkEntry !== 'string' || !isAbsolute(sdkEntry)) throw new Error();
        const realEntry = realpathSync(sdkEntry);
        if (!statSync(realEntry).isFile()) throw new Error();
        undici = createRequire(realEntry)('undici');
        if (typeof undici.EnvHttpProxyAgent !== 'function' || typeof undici.Client !== 'function'
                || typeof undici.Pool !== 'function' || typeof undici.setGlobalDispatcher !== 'function'
                || (undici.install !== undefined && typeof undici.install !== 'function')) {
            throw new Error();
        }
    } catch {
        throw new Error('Pi network initialization failed');
    }

    const createClient = (origin, options) => withErrorListener(new undici.Client(origin, options));
    const createOriginDispatcher = (origin, options) => {
        if (options.connections === 1) return createClient(origin, options);
        return withErrorListener(new undici.Pool(origin, { ...options, factory: createClient }));
    };

    const changedEnv = [];
    let dispatcher;
    let previousDispatcher;
    let attemptedRegistration = false;
    const fetchBeforeInstall = globalThis.fetch;
    try {
        if (typeof undici.getGlobalDispatcher === 'function') {
            previousDispatcher = undici.getGlobalDispatcher();
        }
        if (proxy) {
            for (const key of ['HTTP_PROXY', 'HTTPS_PROXY']) {
                if (process.env[key] === undefined) {
                    process.env[key] = proxy;
                    changedEnv.push(key);
                }
            }
        }
        dispatcher = withErrorListener(new undici.EnvHttpProxyAgent({
            allowH2: false,
            proxyTunnel: true,
            bodyTimeout: timeout,
            headersTimeout: timeout,
            connect: { autoSelectFamilyAttemptTimeout: 2_000 },
            clientFactory: createClient,
            factory: createOriginDispatcher,
        }));
        attemptedRegistration = true;
        undici.setGlobalDispatcher(dispatcher);
        const mayInstall = globalThis.fetch === originalFetch
            || (hasInstalledFetch && globalThis.fetch === installedFetch);
        if (mayInstall && typeof undici.install === 'function') {
            undici.install();
            installedFetch = globalThis.fetch;
            hasInstalledFetch = true;
        }
        return dispatcher;
    } catch {
        if (attemptedRegistration && previousDispatcher !== undefined) {
            try { undici.setGlobalDispatcher(previousDispatcher); } catch { /* Best effort. */ }
        }
        if (globalThis.fetch !== fetchBeforeInstall) {
            try { globalThis.fetch = fetchBeforeInstall; } catch { /* Best effort. */ }
        }
        for (const key of changedEnv) delete process.env[key];
        if (dispatcher && typeof dispatcher.destroy === 'function') {
            try {
                const cleanup = dispatcher.destroy();
                if (cleanup && typeof cleanup.catch === 'function') cleanup.catch(() => {});
            } catch { /* Best effort. */ }
        }
        throw new Error('Pi network initialization failed');
    }
}
