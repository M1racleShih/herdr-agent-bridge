// Minimal Pi development surface: exact writable files and named check argv.
// This is not an OS sandbox: authorized checks execute project code.
import { lstat, readFile, realpath } from 'node:fs/promises';
import { dirname, relative, resolve, sep } from 'node:path';
import { spawn } from 'node:child_process';

async function writable(policy, name) {
    await active(policy);
    if (typeof name !== 'string') throw new Error('Expected a file path');
    const root = resolve(policy.project);
    const target = resolve(root, name);
    const local = relative(root, target);
    if (!policy.allowed_paths.includes(local) || local.startsWith(`..${sep}`)) {
        throw new Error('File is outside the authorized write scope');
    }
    for (let cursor = target; cursor !== root; cursor = dirname(cursor)) {
        try {
            const info = await lstat(cursor);
            if (info.isSymbolicLink()) throw new Error('Symlink in writable path');
        } catch (error) {
            if (error?.code !== 'ENOENT') throw error;
        }
    }
    if (await realpath(root) !== root) throw new Error('Project path changed');
}

async function active(policy) {
    if (policy.journal) {
        const journal = JSON.parse(await readFile(policy.journal, 'utf8'));
        if (journal.closed || journal.development?.state !== 'active') {
            throw new Error('Development task is no longer active');
        }
    }
}

function runCheck(policy, check, signal) {
    return new Promise((resolveResult, reject) => {
        const child = spawn(check.argv[0], check.argv.slice(1), {
            cwd: policy.project, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
            stdio: ['ignore', 'pipe', 'pipe'], detached: true,
        });
        let text = '', bytes = 0, stopped = null;
        const stop = (reason) => {
            stopped = reason;
            try { process.kill(-child.pid, 'SIGKILL'); } catch { /* exited */ }
        };
        const timer = setTimeout(() => stop('timeout'), check.timeout * 1000);
        const abort = () => stop('aborted');
        signal?.addEventListener('abort', abort, { once: true });
        if (signal?.aborted) abort();
        const append = (chunk) => {
            bytes += chunk.length;
            if (bytes <= 1_048_576) text += chunk.toString();
            else stop('output_limit');
        };
        child.stdout.on('data', append);
        child.stderr.on('data', append);
        const clear = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); };
        child.on('error', () => { clear(); reject(new Error('Named check could not start')); });
        child.on('close', (code) => {
            clear();
            const details = { name: check.name, argv: check.argv, exit_code: code, stopped };
            resolveResult({ content: [{ type: 'text', text: JSON.stringify(details) + '\n' + text }], details });
        });
    });
}

export function createDevelopmentTools(sdk, policy) {
    const tools = [
        ['scoped_edit', sdk.createEditToolDefinition(policy.project)],
        ['scoped_write', sdk.createWriteToolDefinition(policy.project)],
    ].map(([name, original]) => ({
        ...original, name, label: name,
        description: `${original.description || name} Only these project files: ${policy.allowed_paths.join(', ')}`,
        execute: async (id, args, ...rest) => {
            await writable(policy, args.path);
            return original.execute(id, args, ...rest);
        },
    }));
    tools.push({
        name: 'run_check', label: 'Run authorized check',
        description: `Execute one preauthorized check by name: ${policy.checks.map(c => c.name).join(', ')}. No shell commands accepted.`,
        parameters: { type: 'object', properties: { name: { type: 'string', enum: policy.checks.map(c => c.name) } },
                      required: ['name'], additionalProperties: false },
        execute: async (_id, args, signal) => {
            await active(policy);
            const check = policy.checks.find(c => c.name === args.name);
            if (!check) throw new Error('Unknown check; scope expansion needs operator decision');
            return runCheck(policy, check, signal);
        },
    });
    return tools;
}
