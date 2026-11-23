"""Scoped Pi tools execute only disposable local files and checks."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_agent_bridge.resource_paths import resource_path

MODULE = resource_path("pi_development_tools.mjs")


class ScopedToolsTests(unittest.TestCase):
    def test_write_scope_check_allowlist_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / "allowed.txt").write_text("before")
            (p / "other.txt").write_text("keep")
            policy = {
                "project": directory,
                "allowed_paths": ["allowed.txt"],
                "checks": [
                    {"name": "ok", "argv": ["node", "-e", 'console.log("checked")'], "timeout": 2}
                ],
            }
            script = f"""
import {{ createDevelopmentTools }} from {json.dumps(MODULE.as_uri())};
import {{ writeFile, readFile, unlink, symlink }} from 'node:fs/promises';
import assert from 'node:assert/strict';
const project = {json.dumps(directory)};
const policy = {json.dumps(policy)};
const definition = () => ({{parameters: {{type:'object'}}, execute: async (_id, a) => {{
 await writeFile(project+'/'+a.path, a.content); return {{content: [{{type:'text',text:'ok'}}]}};
}}}});
const sdk = {{createWriteToolDefinition: definition, createEditToolDefinition: definition}};
const tools = createDevelopmentTools(sdk, policy);
const write = tools.find(t=>t.name==='scoped_write');
await write.execute('1',{{path:'allowed.txt',content:'after'}});
assert.equal(await readFile(project+'/allowed.txt','utf8'),'after');
await assert.rejects(()=>write.execute('2',{{path:'other.txt',content:'bad'}}));
await assert.rejects(()=>write.execute('3',{{path:'../outside',content:'bad'}}));
await unlink(project+'/allowed.txt'); await symlink(project+'/other.txt',project+'/allowed.txt');
await assert.rejects(()=>write.execute('4',{{path:'allowed.txt',content:'bad'}}));
assert.equal(await readFile(project+'/other.txt','utf8'),'keep');
const check=tools.find(t=>t.name==='run_check');
await assert.rejects(()=>check.execute('5',{{name:'shell'}}));
const result=await check.execute('6',{{name:'ok'}});
assert.match(result.content[0].text,/checked/);
assert.equal(result.details.exit_code,0);
"""
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
