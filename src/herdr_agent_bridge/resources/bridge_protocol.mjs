// One checked-in wire contract shared with bridge_protocol.py.
import { readFileSync } from 'node:fs';

const protocol = JSON.parse(readFileSync(new URL('./bridge_protocol.json', import.meta.url), 'utf8'));
export const thinkingLevels = new Set(protocol.thinking_levels);
