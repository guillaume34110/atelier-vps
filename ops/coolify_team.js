// Executed only by the root-owned settings helper inside Coolify 3.12.
const {prisma, decrypt} = require('./lib/common');
const crypto = require('crypto');
const assert = require('assert');

async function configureTeam(request) {
  const owner = await prisma.user.findFirst({where: {permission: {some: {permission: 'owner'}}}, include: {permission: true}});
  assert(owner, 'Coolify owner unavailable');
  const permission = owner.permission.find(p => p.permission === 'owner');
  const enc = value => Buffer.from(JSON.stringify(value)).toString('base64url');
  const unsigned = enc({alg: 'HS256', typ: 'JWT'}) + '.' + enc({userId: owner.id, teamId: permission.teamId,
    permission: 'owner', isAdmin: true, exp: Math.floor(Date.now() / 1000) + 90});
  const token = unsigned + '.' + crypto.createHmac('sha256', process.env.COOLIFY_SECRET_KEY_BETTER || process.env.COOLIFY_SECRET_KEY)
    .update(unsigned).digest('base64url');
  async function api(path, body, method = body ? 'POST' : 'GET') {
    const response = await fetch('http://127.0.0.1:3000/api/v1/applications' + path, {
      method, headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'},
      ...(body ? {body: JSON.stringify(body)} : {})});
    if (!response.ok) throw Error('Coolify HTTP ' + response.status);
    const text = await response.text(); return text ? JSON.parse(text) : {};
  }
  const base = (await api('/' + request.base_application)).application;
  assert(base.branch === 'main' && base.repository === 'guillaume34110/atelier-vps');
  assert(/^[a-f0-9]{40}$/.test(base.gitCommitHash) && base.settings.autodeploy === false);
  if (request.action === 'create') return await api('/new', {});
  const id = request.application;
  assert(/^[a-z0-9]+$/.test(id));
  if (request.action === 'deploy') return await api('/' + id + '/deploy', {forceRebuild: false});
  if (request.action === 'stop') return await api('/' + id + '/stop', {});
  assert(request.action === 'configure');
  const dev = request.mount + '/.usevps-dev';
  const binds = [[request.workspace, '/workspace'], [request.mount + '/.usevps-stat', '/volume-stat:ro'],
    ...[['usr', '/usr'], ['usr/bin', '/bin'], ['usr/sbin', '/sbin'], ['usr/lib', '/lib'],
      ['usr/lib64', '/lib64'], ['etc', '/etc'], ['opt', '/opt']].map(([source, target]) => [dev + '/' + source, target + ':ro'])];
  const existing = await prisma.applicationPersistentStorage.findMany({where: {applicationId: id}});
  assert(existing.every(row => binds.some(([host, path]) => host === row.hostPath && path === row.path && !row.oldPath)),
    'unexpected storage; refusing to change unrelated mounts');
  if (id !== request.base_application) {
    await api('/' + id + '/configuration/source', {gitSourceId: base.gitSourceId});
    await api('/' + id + '/configuration/repository', {repository: base.repository, branch: 'main',
      projectId: base.projectId, autodeploy: false, isPublicRepository: true});
    await api('/' + id + '/configuration/destination', {destinationId: base.destinationDockerId});
    await api('/' + id + '/configuration/buildpack', {buildPack: 'compose'});
  }
  const app = (await api('/' + id)).application;
  await api('/' + id, {...app, name: 'atelier-vps-' + request.tenant, gitCommitHash: base.gitCommitHash,
    dockerComposeFileLocation: '/docker-compose.yaml', dockerComposeConfiguration: JSON.stringify({worker: {port: 8080}})});
  const env = {USEVPS_VOLUME_ID: request.volume_id, USEVPS_VOLUME_STATS: '/volume-stat', USEVPS_RESERVE_GIB: '0.5',
    USEVPS_HOST_RAM_RESERVE_GIB: '2', USEVPS_TENANT: request.tenant, USEVPS_MODE: request.mode,
    USEVPS_MEMORY_LIMIT: String(request.limits.ram_bytes), USEVPS_POOL_RAM_GIB: String(request.limits.ram_bytes / 1073741824),
    USEVPS_CPU_LIMIT: String(request.limits.cpus), USEVPS_CPU_SHARES: String(request.limits.cpu_shares),
    USEVPS_MAX_JOBS: String(request.limits.slots)};
  for (const [name, value] of Object.entries(env)) {
    const secrets = await prisma.secret.findMany({where: {applicationId: id, name}});
    if (secrets.length && secrets.every(s => decrypt(s.value) === value)) continue;
    await api('/' + id + '/secrets', {name, oldName: name, value, isBuildSecret: false}, secrets.length ? 'PUT' : 'POST');
  }
  for (const [hostPath, path] of binds)
    if (!existing.some(row => row.hostPath === hostPath && row.path === path)) await api('/' + id + '/storages', {hostPath, path, newStorage: true});
  return {id, commit: base.gitCommitHash, configured: true};
}
