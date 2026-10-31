"""Real API/Chromium recipes with source reuse and isolated parallel services."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import projects
import recipes
import vps
from ops.gateway import WORKER_EXEC


def verify_recipes(container, root, evidence, execute, payload):
    value = {'argv': ['npm', 'run', 'test'],
             'setup': [['npm', 'ci', '--prefer-offline', '--no-audit', '--no-fund']],
             'services': [{'name': 'api', 'argv': ['node', 'api.js', '{port:api}'],
                           'ready_url': 'http://127.0.0.1:{port:api}/health', 'timeout': 20}],
             'artifacts': ['result.json']}
    files = {
        '.atelier-vps.json': json.dumps({'version': 1, 'id': 'recipe-ci', 'recipes': {'e2e': value}}),
        'package.json': json.dumps({'name': 'recipe-ci', 'version': '1.0.0', 'scripts': {'test': 'node test.js'}}),
        'package-lock.json': json.dumps({'name': 'recipe-ci', 'version': '1.0.0', 'lockfileVersion': 3,
                                        'packages': {'': {'name': 'recipe-ci', 'version': '1.0.0'}}}),
        'api.js': "require('http').createServer((q,s)=>{if(q.url==='/health'){s.end('ok')}else{s.setHeader('Content-Type','text/html');s.end('<title>Recipe VPS</title><p id=answer>42</p>')}}).listen(Number(process.argv[2]),'127.0.0.1')",
        'test.js': "const assert=require('assert/strict'),fs=require('fs'),{chromium}=require('playwright-core');"
                   "(async()=>{const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox','--disable-dev-shm-usage']});"
                   "try{const page=await browser.newPage();const origin=process.env.ATELIER_SERVICE_API_ORIGIN;await page.goto(origin);"
                   "assert.equal(await page.title(),'Recipe VPS');assert.equal(await page.locator('#answer').textContent(),'42');"
                   "fs.writeFileSync('result.json',JSON.stringify({answer:42,browser:true,origin}));}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)})",
        'notes.txt': 'source fixture\n' * 2048,
    }
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        source = Path(tmp)
        for name, text in files.items():
            (source / name).write_text(text)
        _, found = recipes.discover(source)
        digest, _ = projects.fingerprint(projects.tree_files(source))
        ref = {'id': 'recipe-ci', 'revision': digest, 'upload': False}
        request = {'recipe_run': True, 'project_task': ref, 'disk_bytes': 256 * 1024 ** 2,
                   'ram_bytes': 768 * 1024 ** 2, 'cpus': 1, 'timeout': 120,
                   'project': 'recipe-ci', 'agent': 'CI · recettes réutilisables'}
        argv = ['python3', '/app/recipes.py', json.dumps(found['e2e'])]
        plan = json.loads(execute(container, 'plan', json.dumps({**request, 'argv': argv}).encode() + b'\n'))
        assert plan['admission']['allowed'] and not plan['project']['present'], plan
        first = json.loads(execute(container, 'run', payload(argv, files=files, include_default=False,
                                **{**request, 'project_task': {**ref, 'upload': True}})))
        assert first['status'] == 'passed' and first['managed_project']['created'], first
        plan = json.loads(execute(container, 'plan', json.dumps({**request, 'argv': argv}).encode() + b'\n'))
        assert plan['project']['present'], plan
        runs = [first]
        pending = []
        try:
            for _ in range(2):
                process = subprocess.Popen(['docker', 'exec', '-i', '--user', '10001:10001', container,
                    *WORKER_EXEC, 'run'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                process.stdin.write(payload(argv, include_default=False, **request))
                process.stdin.close(); process.stdin = None
                pending.append(process)
            for process in pending:
                raw, errors = process.communicate(timeout=180)
                assert process.returncode == 0, errors[-2000:]
                run = json.loads(raw)
                assert run['status'] == 'passed' and not run['managed_project']['created'], run
                runs.append(run)
            assert max(x['started'] for x in runs[1:]) < min(x['ended'] for x in runs[1:])
            results = []
            for run in runs:
                archive = source / (run['id'] + '.tar')
                archive.write_bytes(execute(container, 'fetch', json.dumps({'id': run['id'],
                    'paths': ['work/result.json', 'receipt.json']}).encode() + b'\n'))
                target = source / ('result-' + run['id'])
                assert vps.unpack_result(archive, target) == 2
                result = json.loads((target / 'work/result.json').read_text())
                assert result['answer'] == 42 and result['browser'] is True
                results.append({'job': run['id'], 'result': result,
                                'sha256': hashlib.sha256((target / 'work/result.json').read_bytes()).hexdigest()})
            assert results[1]['result']['origin'] != results[2]['result']['origin']
            (evidence / 'project-recipes.json').write_text(json.dumps({'source_revision': digest,
                'created_once': True, 'reused_without_source_upload': True, 'parallel': True, 'results': results}, indent=2))
            for run in runs:
                execute(container, 'release', json.dumps({'id': run['id']}).encode() + b'\n')
        finally:
            for process in pending:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=10)
    print('Project recipes: real API + Chromium, reusable sources and isolated parallel services passed', flush=True)
