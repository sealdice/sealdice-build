'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const launcher = require('../bin/sealdice.js');

function checksum(contents) {
  return crypto.createHash('sha256').update(contents).digest('hex');
}

function createSource(root, version, files) {
  const packageDir = path.join(root, `package-${version}`);
  const payloadDir = path.join(packageDir, 'payload');
  fs.mkdirSync(payloadDir, { recursive: true });
  const entries = [];
  for (const [relativePath, contents] of Object.entries(files)) {
    const destination = path.join(payloadDir, ...relativePath.split('/'));
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, contents);
    entries.push({
      path: relativePath,
      sha256: checksum(contents),
      size: Buffer.byteLength(contents),
      mode: relativePath === 'sealdice-core' ? '0755' : '0644',
    });
  }
  return {
    packageName: '@sealtrpg/sealdice-linux-x64',
    packageDir,
    packageJson: { name: '@sealtrpg/sealdice-linux-x64', version },
    payloadDir,
    manifest: {
      schemaVersion: 1,
      packageVersion: version,
      platform: { key: 'linux-x64', os: 'linux', cpu: 'x64', binary: 'sealdice-core' },
      release: {
        repository: 'sealdice/sealdice-build',
        tag: `v${version}`,
        asset: `sealdice-core_${version}_linux_amd64.tar.gz`,
        assetSha256: checksum(`asset-${version}`),
      },
      files: entries,
    },
  };
}

function temporaryDirectory(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sealdice-launcher-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

test('deploys a release payload into an empty instance directory', (t) => {
  const root = temporaryDirectory(t);
  const source = createSource(root, '1.5.1', {
    'sealdice-core': 'binary-v1',
    'data/decks/seal.json': '{}\n',
  });
  const target = path.join(root, 'instance');

  const result = launcher.deployPayload(source, target);

  assert.equal(result.changed, true);
  assert.equal(fs.readFileSync(path.join(target, 'sealdice-core'), 'utf8'), 'binary-v1');
  assert.equal(fs.readFileSync(path.join(target, 'data/decks/seal.json'), 'utf8'), '{}\n');
  const deployment = JSON.parse(
    fs.readFileSync(path.join(target, launcher.DEPLOYMENT_FILE), 'utf8'),
  );
  assert.equal(deployment.packageVersion, '1.5.1');
});

test('upgrades managed files while preserving user and modified obsolete files', (t) => {
  const root = temporaryDirectory(t);
  const target = path.join(root, 'instance');
  const first = createSource(root, '1.5.1', {
    'sealdice-core': 'binary-v1',
    'obsolete.txt': 'remove-me',
    'modified-obsolete.txt': 'original',
  });
  launcher.deployPayload(first, target);
  fs.writeFileSync(path.join(target, 'modified-obsolete.txt'), 'user-change');
  fs.writeFileSync(path.join(target, 'user-config.json'), '{"mine":true}\n');

  const second = createSource(root, '1.6.0', {
    'sealdice-core': 'binary-v2',
    'data/decks/seal.json': '{"updated":true}\n',
  });
  launcher.deployPayload(second, target);

  assert.equal(fs.readFileSync(path.join(target, 'sealdice-core'), 'utf8'), 'binary-v2');
  assert.equal(fs.existsSync(path.join(target, 'obsolete.txt')), false);
  assert.equal(fs.readFileSync(path.join(target, 'modified-obsolete.txt'), 'utf8'), 'user-change');
  assert.equal(fs.readFileSync(path.join(target, 'user-config.json'), 'utf8'), '{"mine":true}\n');
});

test('refuses to deploy into an unrelated non-empty directory', (t) => {
  const root = temporaryDirectory(t);
  const source = createSource(root, '1.5.1', { 'sealdice-core': 'binary' });
  const target = path.join(root, 'unrelated');
  fs.mkdirSync(target);
  fs.writeFileSync(path.join(target, 'project.txt'), 'work');

  assert.throws(
    () => launcher.deployPayload(source, target),
    /Refusing to deploy SealDice into unrelated non-empty directory/,
  );
});

test('leaves a recovery marker after an interrupted deployment and can resume', (t) => {
  const root = temporaryDirectory(t);
  const target = path.join(root, 'instance');
  const broken = createSource(root, '1.5.1', { 'sealdice-core': 'binary' });
  broken.manifest.files.push({
    path: 'data/missing.json',
    sha256: checksum('missing'),
    size: Buffer.byteLength('missing'),
    mode: '0644',
  });

  assert.throws(() => launcher.deployPayload(broken, target), /Payload file is missing/);
  assert.equal(fs.existsSync(path.join(target, launcher.DEPLOYING_FILE)), true);

  const repaired = createSource(root, '1.5.1', {
    'sealdice-core': 'binary',
    'data/missing.json': 'missing',
  });
  const result = launcher.deployPayload(repaired, target);
  assert.equal(result.changed, true);
  assert.equal(fs.existsSync(path.join(target, launcher.DEPLOYING_FILE)), false);
  assert.equal(fs.existsSync(path.join(target, launcher.DEPLOYMENT_FILE)), true);
});

test('rejects unsafe manifest paths', () => {
  assert.throws(() => launcher.normalizeRelativePath('../outside'), /Unsafe payload path/);
  assert.throws(() => launcher.normalizeRelativePath('C:\\outside'), /Unsafe payload path/);
});

test('forwards arguments, cwd, and the child exit code', async () => {
  let observed;
  const fakeSpawn = (binary, args, options) => {
    observed = { binary, args, options };
    const child = new EventEmitter();
    process.nextTick(() => child.emit('exit', 7, null));
    return child;
  };

  const code = await launcher.spawnBinary('/instance/sealdice-core', ['--version'], '/instance', fakeSpawn);

  assert.equal(code, 7);
  assert.deepEqual(observed.args, ['--version']);
  assert.equal(observed.options.cwd, '/instance');
  assert.equal(observed.options.stdio, 'inherit');
});

test('reports unsupported platforms', () => {
  assert.throws(
    () => launcher.resolvePlatformPackage('freebsd', 'x64'),
    /Unsupported platform: freebsd-x64/,
  );
});
