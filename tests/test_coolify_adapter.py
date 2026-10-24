import json
from pathlib import Path
import subprocess
import unittest


class CoolifyAdapterTests(unittest.TestCase):
    def exercise(self, mode):
        script = r'''
const fs = require('fs'), vm = require('vm');
const calls = [];
const base = {id:'base',branch:'main',repository:'guillaume34110/atelier-vps',gitCommitHash:'a'.repeat(40),
  settings:{autodeploy:process.argv[1]==='unverified'},gitSourceId:'source',destinationDockerId:'destination',projectId:123};
const common = {decrypt:x=>x,prisma:{user:{findFirst:async()=>({id:'user',permission:[{permission:'owner',teamId:'team'}]})},
  secret:{findMany:async()=>[]},applicationPersistentStorage:{findMany:async()=>process.argv[1]==='unsafe-mount'?[{hostPath:'/another/project',path:'/workspace'}]:[]}}};
const sandbox = {require:name=>name==='./lib/common'?common:require(name),Buffer,
  process:{env:{COOLIFY_SECRET_KEY:'fixture-only'}},fetch:async(url,options)=>{
    calls.push({path:url.replace('http://127.0.0.1:3000/api/v1/applications',''),method:options.method,
      body:options.body?JSON.parse(options.body):null});
    return {ok:true,text:async()=>JSON.stringify({application:base})};
  }};
const configure = vm.runInNewContext(fs.readFileSync('ops/coolify_team.js','utf8')+'; configureTeam',sandbox);
configure({action:'configure',base_application:'base',application:'bob',tenant:'bob',mode:'allocated',
  mount:'/mnt/data',workspace:'/mnt/data/users/bob',volume_id:'volume',limits:{ram_bytes:1073741824,cpus:0.5,cpu_shares:64,slots:1}})
  .then(result=>console.log(JSON.stringify({result,calls})))
  .catch(error=>console.log(JSON.stringify({error:error.message,calls})));
'''
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', '-e', script, mode], cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        return json.loads(result.stdout)

    def test_private_mounts_pinned_commit_and_resource_settings(self):
        result = self.exercise('normal')
        self.assertTrue(result['result']['configured'])
        writes = [c for c in result['calls'] if c['method'] != 'GET']
        storages = [c['body'] for c in writes if c['path'].endswith('/storages')]
        self.assertEqual(len(storages), 9)
        self.assertEqual([s['hostPath'] for s in storages if s['path'] == '/workspace'], ['/mnt/data/users/bob'])
        self.assertTrue(all(s['path'].endswith(':ro') for s in storages if s['path'] != '/workspace'))
        env = {c['body']['name']:c['body']['value'] for c in writes if c['path'].endswith('/secrets')}
        self.assertEqual(env['USEVPS_MEMORY_LIMIT'], '1073741824')
        self.assertEqual(env['USEVPS_CPU_LIMIT'], '0.5')
        self.assertEqual(env['USEVPS_CPU_QUOTA'], '50000')
        self.assertEqual(env['USEVPS_HOST_RAM_RESERVE_GIB'], '1')
        self.assertEqual(env['USEVPS_TENANT'], 'bob')
        settings = next(c['body'] for c in writes if c['path'] == '/bob')
        self.assertEqual(settings['gitCommitHash'], 'a' * 40)

    def test_unsafe_mount_or_unverified_base_blocks_all_mutations(self):
        for mode in ['unsafe-mount', 'unverified']:
            with self.subTest(mode=mode):
                result = self.exercise(mode)
                self.assertIn('error', result)
                self.assertFalse(any(c['method'] != 'GET' for c in result['calls']))
