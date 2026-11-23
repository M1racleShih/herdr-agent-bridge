// Pi 1.5's public SDK hosts an isolated interactive session while the native
// ModelRuntime alone owns OAuth refresh and locking of the original auth store.
import { randomUUID } from 'node:crypto';
import { readFile, realpath, rename, stat, unlink, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { configurePiNetwork } from './pi_network.mjs';
import { createDevelopmentTools } from './pi_development_tools.mjs';
import { thinkingLevels } from './bridge_protocol.mjs';

const packageNames = new Set([
    '@earendil-works/pi-coding-agent', '@mariozechner/pi-coding-agent',
]);
const selectionErrors = new Set(['model_unavailable', 'thinking_unsupported',
    'selection_mismatch', 'selection_validation_unavailable']);
let selectionFile;

class SelectionError extends Error {
    constructor(reason) {
        super(reason);
        this.reason = reason;
    }
}

async function recordSelection(selection) {
    const temporary = `${selectionFile}.${randomUUID()}.tmp`;
    try {
        await writeFile(temporary, `${JSON.stringify(selection)}\n`, { flag: 'wx', mode: 0o600 });
        await rename(temporary, selectionFile);
    } finally {
        await unlink(temporary).catch(() => {});
    }
}

function inside(directory, path) {
    const subpath = relative(directory, path);
    return subpath !== '' && subpath !== '..' && !subpath.startsWith(`..${sep}`)
        && !isAbsolute(subpath);
}

async function publicSdk(cliPath) {
    if (!isAbsolute(cliPath)) throw new Error('invalid CLI path');
    const executable = await realpath(cliPath);
    if (!(await stat(executable)).isFile()) throw new Error('invalid CLI');
    let directory = dirname(executable);
    while (true) {
        let manifest;
        try {
            manifest = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8'));
        } catch (error) {
            if (error?.code !== 'ENOENT') throw error;
        }
        if (manifest && packageNames.has(manifest.name)) {
            const root = manifest.exports?.['.'];
            const target = typeof root === 'string' ? root : root?.import ?? root?.default;
            if (typeof target !== 'string' || !target.startsWith('./')) {
                throw new Error('public SDK export unavailable');
            }
            const entry = await realpath(resolve(directory, target));
            if (!inside(directory, entry) || !(await stat(entry)).isFile()) {
                throw new Error('invalid public SDK export');
            }
            return { sdk: await import(pathToFileURL(entry).href), sdkEntry: entry };
        }
        const parent = dirname(directory);
        if (parent === directory) throw new Error('Pi package unavailable');
        directory = parent;
    }
}

async function main(args) {
    const [cliPath, agentPath, projectPath, modelName] = args;
    let index = 4;
    const development = args[index] === 'development';
    if (development) index++;
    let requestedThinking;
    if (args[index] === '--thinking' && args[index + 2] === '--selection-file'
            && args.length === index + 4) {
        requestedThinking = args[index + 1];
        selectionFile = args[index + 3];
        if (!thinkingLevels.has(requestedThinking) || !isAbsolute(selectionFile)) {
            throw new Error('invalid selection');
        }
        index += 4;
    }
    if (args.length !== index) throw new Error('invalid arguments');
    if (!isAbsolute(agentPath) || !isAbsolute(projectPath)) throw new Error('invalid paths');
    const agentDir = resolve(agentPath);
    const project = resolve(projectPath);
    const sessionDir = join(agentDir, 'sessions');
    const slash = modelName.indexOf('/');
    const provider = modelName.slice(0, slash);
    const modelId = modelName.slice(slash + 1);
    if (slash <= 0 || !modelId || /[\s\x00-\x1f\x7f]/.test(modelName)
            || provider.startsWith('-') || modelId.startsWith('-')) {
        throw new Error('invalid model');
    }
    let profile;
    try {
        profile = JSON.parse(await readFile(join(agentDir, 'auth-source.json'), 'utf8'));
    } catch (error) {
        if ((!development && requestedThinking === undefined) || error?.code !== 'ENOENT') throw error;
        profile = { path: join(agentDir, 'auth.json'), isolated: true };
    }
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)
            || typeof profile.path !== 'string' || !isAbsolute(profile.path)
            || (inside(agentDir, resolve(profile.path)) && !((development || requestedThinking !== undefined) && profile.isolated === true
                 && profile.path === join(agentDir, 'auth.json')))) {
        throw new Error('invalid native auth source');
    }
    const policy = development
        ? JSON.parse(await readFile(join(agentDir, 'development-policy.json'), 'utf8')) : null;
    if (policy && resolve(policy.project) !== project) throw new Error('development scope changed');

    // Match CLI process markers and avoid startup catalog/version network calls.
    // Model requests and native OAuth refresh remain available to the SDK.
    process.env.AI_AGENT = 'pi';
    process.env.PI_CODING_AGENT = 'true';
    process.env.PI_CODING_AGENT_DIR = agentDir;
    process.env.PI_CODING_AGENT_SESSION_DIR = sessionDir;
    process.env.PI_PROGRAM_STATUS = '1';
    process.env.PI_OFFLINE = '1';
    process.chdir(project);

    const { sdk, sdkEntry } = await publicSdk(cliPath);
    const networkSettings = JSON.parse(await readFile(join(agentDir, 'settings.json'), 'utf8'));
    const dispatcher = configurePiNetwork(sdkEntry, networkSettings);
    let runtime;
    try {
        const modelRuntime = await sdk.ModelRuntime.create({
            authPath: profile.path,
            modelsPath: join(agentDir, 'models.json'),
            modelsStorePath: join(agentDir, 'models-store.json'),
            allowModelNetwork: false,
        });
        const createRuntime = async ({ cwd, agentDir: requestedAgentDir, sessionManager, sessionStartEvent }) => {
            if (typeof cwd !== 'string' || resolve(cwd) !== project
                    || typeof requestedAgentDir !== 'string' || resolve(requestedAgentDir) !== agentDir
                    || !sessionManager || typeof sessionManager.getCwd !== 'function'
                    || typeof sessionManager.getSessionDir !== 'function'
                    || resolve(sessionManager.getCwd()) !== project
                    || resolve(sessionManager.getSessionDir()) !== sessionDir) {
                throw new Error('session scope changed');
            }
            if (typeof sessionManager.getSessionFile === 'function') {
                const file = sessionManager.getSessionFile();
                if (file !== undefined && (typeof file !== 'string' || !inside(sessionDir, resolve(file)))) {
                    throw new Error('session file outside scope');
                }
            }
            const settingsManager = sdk.SettingsManager.create(project, agentDir, { projectTrusted: false });
            const services = await sdk.createAgentSessionServices({
                cwd: project, agentDir, settingsManager, modelRuntime,
                resourceLoaderOptions: {
                    noExtensions: true, disabledBuiltinExtensions: ['mcp'],
                    noSkills: true, noPromptTemplates: true, noThemes: true,
                },
            });
            if (!services || !Array.isArray(services.diagnostics)
                    || services.diagnostics.some((diagnostic) => diagnostic?.type === 'error')) {
                throw new Error('session services unavailable');
            }
            const model = modelRuntime.getModel(provider, modelId);
            if (!model || model.provider !== provider || model.id !== modelId) {
                throw new SelectionError('model_unavailable');
            }
            const thinkingLevel = requestedThinking ?? (typeof settingsManager.getDefaultThinkingLevel === 'function'
                ? settingsManager.getDefaultThinkingLevel() : undefined);
            if (thinkingLevel !== undefined && !thinkingLevels.has(thinkingLevel)) {
                throw new Error('invalid thinking level');
            }
            const created = await sdk.createAgentSessionFromServices({
                services, sessionManager, sessionStartEvent, model,
                tools: ['read', 'grep', 'find', 'ls', ...(development ? ['scoped_edit', 'scoped_write', 'run_check'] : [])],
                ...(development ? { customTools: createDevelopmentTools(sdk, policy) } : {}), thinkingLevel,
            });
            if (requestedThinking !== undefined) {
                const session = created?.session;
                if (!session || typeof session.getAvailableThinkingLevels !== 'function'
                        || typeof sessionManager.getSessionId !== 'function') {
                    throw new SelectionError('selection_validation_unavailable');
                }
                const levels = session.getAvailableThinkingLevels();
                if (!Array.isArray(levels) || !levels.every((level) => thinkingLevels.has(level))) {
                    throw new SelectionError('selection_validation_unavailable');
                }
                if (!levels.includes(requestedThinking)) throw new SelectionError('thinking_unsupported');
                if (session.model?.provider !== provider || session.model?.id !== modelId
                        || session.thinkingLevel !== requestedThinking) {
                    throw new SelectionError('selection_mismatch');
                }
                const sessionId = sessionManager.getSessionId();
                if (typeof sessionId !== 'string' || !sessionId) {
                    throw new SelectionError('selection_validation_unavailable');
                }
                await recordSelection({ state: 'ready', model: modelName,
                    thinking: requestedThinking, session_id: sessionId });
            }
            return { ...created, services, diagnostics: services.diagnostics };
        };
        const sessionManager = sdk.SessionManager.create(project, sessionDir);
        runtime = await sdk.createAgentSessionRuntime(createRuntime, {
            cwd: project, agentDir, sessionManager,
        });
        sdk.initTheme(undefined, false);
        await new sdk.InteractiveMode(runtime, { tuiMode: 'regular' }).run();
    } finally {
        try {
            if (runtime) await runtime.dispose();
        } finally {
            await dispatcher.destroy();
        }
    }
}

try {
    await main(process.argv.slice(2));
} catch (error) {
    // SDK errors can include provider details, auth paths, or credential values.
    // Keep those out of the hc terminal and any public bridge output.
    const reason = selectionFile && error instanceof SelectionError && selectionErrors.has(error.reason)
        ? error.reason : 'startup_failed';
    if (selectionFile && isAbsolute(selectionFile)) {
        await recordSelection({ state: 'failed', reason }).catch(() => {});
    }
    process.stderr.write(reason === 'startup_failed'
        ? 'Pi OAuth session could not start or finish.\n'
        : `Pi selection failed: ${reason}.\n`);
    process.exitCode = 1;
}
