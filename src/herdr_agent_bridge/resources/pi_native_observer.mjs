// Additive Pi CLI extension: observe native state without modifying resources.
import { randomUUID } from 'node:crypto';
import { rename, unlink, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, join } from 'node:path';
import { thinkingLevels } from './bridge_protocol.mjs';

async function record(path, value) {
    const temporary = `${path}.${randomUUID()}.tmp`;
    try {
        await writeFile(temporary, `${JSON.stringify(value)}\n`, { flag: 'wx', mode: 0o600 });
        await rename(temporary, path);
    } finally {
        await unlink(temporary).catch(() => {});
    }
}

function resourcesSnapshot(pi) {
    const commands = pi.getCommands();
    const tools = pi.getAllTools();
    if (!Array.isArray(commands) || !Array.isArray(tools)) throw new Error('unavailable');
    const commandPaths = { extension: new Set(), prompt: new Set(), skill: new Set() };
    const toolPaths = new Set();
    let truncated = false;
    const add = (set, source) => {
        const path = source?.path;
        if (typeof path !== 'string' || path.length > 4096 || /[\x00-\x1f\x7f]/.test(path)
                || !(isAbsolute(path) || /^builtin:[a-zA-Z0-9._/-]+$/.test(path))) return;
        if (set.size >= 512 && !set.has(path)) truncated = true;
        else set.add(path);
    };
    for (const command of commands.slice(0, 4096)) {
        if (Object.hasOwn(commandPaths, command?.source)) add(commandPaths[command.source], command.sourceInfo);
    }
    for (const tool of tools.slice(0, 4096)) add(toolPaths, tool?.sourceInfo);
    if (commands.length > 4096 || tools.length > 4096) truncated = true;
    return { state: 'partial', observed_at: 'session_start',
        command_paths: Object.fromEntries(Object.entries(commandPaths).map(([type, paths]) => [type, [...paths].sort()])),
        tool_paths: [...toolPaths].sort(), truncated };
}

function validId(value) {
    return typeof value === 'string' && value.length > 0 && value.length <= 512
        && !/[\s\x00-\x1f\x7f]/.test(value);
}

function selectionSnapshot(pi, ctx, requestedModel, requestedThinking) {
    const separator = requestedModel.indexOf('/');
    if ((requestedModel && (separator <= 0 || separator === requestedModel.length - 1
            || !validId(requestedModel) || requestedModel.startsWith('-')
            || requestedModel[separator + 1] === '-'))
            || (requestedThinking && !thinkingLevels.has(requestedThinking))) {
        return { state: 'failed', reason: 'startup_failed' };
    }
    if (!ctx.model) return { state: 'failed', reason: 'model_unavailable' };
    const { provider, id } = ctx.model;
    const thinking = pi.getThinkingLevel();
    const sessionId = ctx.sessionManager.getSessionId();
    if (!validId(provider) || provider.includes('/') || !validId(id)
            || !thinkingLevels.has(thinking) || !validId(sessionId)) {
        return { state: 'failed', reason: 'selection_validation_unavailable' };
    }
    const model = `${provider}/${id}`;
    if ((requestedModel && requestedModel !== model)
            || (requestedThinking && requestedThinking !== thinking)) {
        return { state: 'failed', reason: 'selection_mismatch' };
    }
    return { state: 'ready', model, thinking, session_id: sessionId,
        ...(process.env.HAB_PI_RECOVERY_TOKEN ? { startup_token: process.env.HAB_PI_RECOVERY_TOKEN } : {}) };
}

export default function (pi) {
    const selectionFile = process.env.HAB_PI_SELECTION_FILE;
    const resourcesFile = process.env.HAB_PI_RESOURCES_FILE;
    const settledFile = process.env.HAB_PI_SETTLED_FILE;
    const requestedModel = process.env.HAB_PI_MODEL ?? '';
    const requestedThinking = process.env.HAB_PI_THINKING ?? '';
    const canRecordSettled = settledFile && isAbsolute(settledFile)
        && settledFile !== selectionFile && settledFile !== resourcesFile;
    const nameCandidate = selectionFile && isAbsolute(selectionFile)
        ? join(dirname(selectionFile), 'pi-name.json') : undefined;
    const nameFile = nameCandidate && ![selectionFile, settledFile, resourcesFile].includes(nameCandidate)
        ? nameCandidate : undefined;
    let runStart;
    let selectionWrites = Promise.resolve();
    let nameWrites = Promise.resolve();
    function refreshName(ctx) {
        if (!nameFile) return nameWrites;
        let name, sessionId;
        try {
            name = ctx.sessionManager.getSessionName() ?? null;
            sessionId = ctx.sessionManager.getSessionId();
        } catch { return nameWrites; }
        if (!validId(sessionId) || (name !== null && (typeof name !== 'string' || name.length > 4096))) {
            return nameWrites;
        }
        const value = { session_id: sessionId, name, revision: randomUUID() };
        nameWrites = nameWrites.then(() => record(nameFile, value)).catch(() => {});
        return nameWrites;
    }
    function refreshSelection(ctx) {
        if (!selectionFile || !isAbsolute(selectionFile)) return selectionWrites;
        let selection;
        try { selection = selectionSnapshot(pi, ctx, requestedModel, requestedThinking); }
        catch { selection = { state: 'failed', reason: 'selection_validation_unavailable' }; }
        selectionWrites = selectionWrites.then(() => record(selectionFile, selection)).catch(() => {});
        return selectionWrites;
    }
    pi.on('session_start', async (_event, ctx) => {
        runStart = undefined;
        if (canRecordSettled) {
            let sessionId;
            try { sessionId = ctx.sessionManager?.getSessionId?.(); } catch { /* unavailable */ }
            const state = validId(sessionId) ? { state: 'starting', session_id: sessionId }
                : { state: 'unavailable' };
            await record(settledFile, state).catch(() => {});
        }
        if (resourcesFile && isAbsolute(resourcesFile) && resourcesFile !== selectionFile) {
            let snapshot;
            try { snapshot = resourcesSnapshot(pi); }
            catch { snapshot = { state: 'unavailable', observed_at: 'session_start' }; }
            await record(resourcesFile, snapshot).catch(() => {});
        }
        await Promise.all([refreshSelection(ctx), refreshName(ctx)]);
    });
    pi.on('session_info_changed', (_event, ctx) => refreshName(ctx));
    pi.on('model_select', (_event, ctx) => refreshSelection(ctx));
    pi.on('thinking_level_select', (_event, ctx) => refreshSelection(ctx));
    pi.on('agent_start', async (_event, ctx) => {
        if (!canRecordSettled) return;
        runStart = undefined;
        try {
            const sessionId = ctx.sessionManager.getSessionId();
            const entries = ctx.sessionManager.getEntries();
            if (validId(sessionId) && Array.isArray(entries)) {
                runStart = { sessionId, count: entries.length };
                await record(settledFile, { state: 'working', session_id: sessionId });
                return;
            }
        } catch { /* Keep unavailable state below; do not expose extension errors. */ }
        await record(settledFile, { state: 'unavailable' }).catch(() => {});
    });
    pi.on('agent_settled', async (event, ctx) => {
        if (!canRecordSettled) return;
        try {
            const sessionId = ctx.sessionManager.getSessionId();
            const entries = ctx.sessionManager.getEntries();
            if (!validId(sessionId) || !Array.isArray(entries)) throw new Error();
            let finalMessageId = null;
            if (runStart?.sessionId === sessionId) {
                for (let index = entries.length - 1; index >= runStart.count; index--) {
                    const entry = entries[index];
                    if (entry?.type !== 'message') continue;
                    if (entry.message?.role === 'assistant' && validId(entry.id)) {
                        finalMessageId = entry.id;
                    }
                    break;
                }
            }
            runStart = undefined;
            await record(settledFile, { state: 'settled', session_id: sessionId,
                final_message_id: event.aborted === false ? finalMessageId : null,
                ...(event.aborted !== false && finalMessageId ? { aborted_message_id: finalMessageId } : {}),
                aborted: event.aborted !== false });
        } catch {
            runStart = undefined;
            await record(settledFile, { state: 'unavailable' }).catch(() => {});
        }
    });
}
