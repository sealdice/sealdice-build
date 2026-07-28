#!/usr/bin/env node
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

const DEPLOYMENT_FILE = '.sealdice-npm.json';
const DEPLOYING_FILE = '.sealdice-npm-deploying';
const MANIFEST_SCHEMA_VERSION = 1;
const PLATFORM_PACKAGES = Object.freeze({
  'win32-x64': '@sealtrpg/sealdice-win32-x64',
  'darwin-x64': '@sealtrpg/sealdice-darwin-x64',
  'darwin-arm64': '@sealtrpg/sealdice-darwin-arm64',
  'linux-x64': '@sealtrpg/sealdice-linux-x64',
  'linux-arm64': '@sealtrpg/sealdice-linux-arm64',
});

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (error) {
    throw new Error(`Failed to read ${filePath}: ${error.message}`);
  }
}

function normalizeRelativePath(value) {
  if (typeof value !== 'string' || value.length === 0 || value.includes('\0')) {
    throw new Error('Payload manifest contains an invalid file path');
  }

  const normalized = value.replace(/\\/g, '/');
  const parts = normalized.split('/');
  if (
    normalized.startsWith('/') ||
    /^[A-Za-z]:/.test(normalized) ||
    parts.some((part) => part === '' || part === '.' || part === '..')
  ) {
    throw new Error(`Unsafe payload path: ${value}`);
  }
  return parts.join('/');
}

function pathFromManifest(root, relativePath) {
  const normalized = normalizeRelativePath(relativePath);
  const resolvedRoot = path.resolve(root);
  const resolved = path.resolve(resolvedRoot, ...normalized.split('/'));
  if (resolved !== resolvedRoot && !resolved.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw new Error(`Payload path escapes its root: ${relativePath}`);
  }
  return resolved;
}

function sha256(filePath) {
  const hash = crypto.createHash('sha256');
  const descriptor = fs.openSync(filePath, 'r');
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    let bytesRead;
    while ((bytesRead = fs.readSync(descriptor, buffer, 0, buffer.length, null)) > 0) {
      hash.update(buffer.subarray(0, bytesRead));
    }
  } finally {
    fs.closeSync(descriptor);
  }
  return hash.digest('hex');
}

function validatePayloadManifest(manifest, packageJson) {
  if (!manifest || manifest.schemaVersion !== MANIFEST_SCHEMA_VERSION) {
    throw new Error('Unsupported payload manifest schema');
  }
  if (manifest.packageVersion !== packageJson.version) {
    throw new Error(
      `Platform package version ${packageJson.version} does not match payload version ${manifest.packageVersion}`,
    );
  }
  if (!manifest.platform || typeof manifest.platform.binary !== 'string') {
    throw new Error('Payload manifest does not define a binary');
  }
  if (!Array.isArray(manifest.files) || manifest.files.length === 0) {
    throw new Error('Payload manifest does not contain files');
  }

  const seen = new Set();
  for (const entry of manifest.files) {
    const relativePath = normalizeRelativePath(entry.path);
    const key = relativePath.toLowerCase();
    if (seen.has(key)) {
      throw new Error(`Duplicate payload path: ${entry.path}`);
    }
    seen.add(key);
    if (!/^[a-f0-9]{64}$/.test(entry.sha256 || '')) {
      throw new Error(`Invalid checksum for payload file: ${entry.path}`);
    }
    if (!Number.isSafeInteger(entry.size) || entry.size < 0) {
      throw new Error(`Invalid size for payload file: ${entry.path}`);
    }
    if (!/^[0-7]{3,4}$/.test(entry.mode || '')) {
      throw new Error(`Invalid mode for payload file: ${entry.path}`);
    }
  }
  normalizeRelativePath(manifest.platform.binary);
}

function resolvePlatformPackage(platform = process.platform, arch = process.arch, resolver = require.resolve) {
  const platformKey = `${platform}-${arch}`;
  const packageName = PLATFORM_PACKAGES[platformKey];
  if (!packageName) {
    throw new Error(
      `Unsupported platform: ${platformKey}. Supported platforms: ${Object.keys(PLATFORM_PACKAGES).join(', ')}`,
    );
  }

  let packageJsonPath;
  try {
    packageJsonPath = resolver(`${packageName}/package.json`);
  } catch (error) {
    throw new Error(
      `The optional package ${packageName} is missing. Reinstall sealdice without omitting optional dependencies.`,
    );
  }

  const packageDir = path.dirname(packageJsonPath);
  const packageJson = readJson(packageJsonPath);
  const payloadDir = path.join(packageDir, 'payload');
  const manifest = readJson(path.join(packageDir, 'payload-manifest.json'));
  validatePayloadManifest(manifest, packageJson);

  return { packageName, packageDir, packageJson, payloadDir, manifest, platformKey };
}

function directoryIsEmpty(directory) {
  return fs.readdirSync(directory).length === 0;
}

function targetIsSealDice(directory) {
  return (
    fs.existsSync(path.join(directory, DEPLOYMENT_FILE)) ||
    fs.existsSync(path.join(directory, DEPLOYING_FILE)) ||
    fs.existsSync(path.join(directory, 'sealdice-core')) ||
    fs.existsSync(path.join(directory, 'sealdice-core.exe'))
  );
}

function ensureSafeTarget(directory) {
  fs.mkdirSync(directory, { recursive: true });
  if (!directoryIsEmpty(directory) && !targetIsSealDice(directory)) {
    throw new Error(
      `Refusing to deploy SealDice into unrelated non-empty directory: ${directory}. ` +
        'Create and enter a dedicated instance directory first.',
    );
  }
}

function writeFileAtomically(destination, contents, mode) {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const temporary = `${destination}.sealdice-npm-${process.pid}-${crypto.randomBytes(6).toString('hex')}.tmp`;
  try {
    if (Buffer.isBuffer(contents) || typeof contents === 'string') {
      fs.writeFileSync(temporary, contents);
    } else {
      fs.copyFileSync(contents.source, temporary);
    }
    if (process.platform !== 'win32' && mode !== undefined) {
      fs.chmodSync(temporary, mode);
    }
    try {
      fs.renameSync(temporary, destination);
    } catch (error) {
      if (!['EEXIST', 'EPERM'].includes(error.code)) {
        throw error;
      }
      if (fs.existsSync(destination)) {
        fs.unlinkSync(destination);
      }
      fs.renameSync(temporary, destination);
    }
  } finally {
    if (fs.existsSync(temporary)) {
      fs.unlinkSync(temporary);
    }
  }
}

function readDeployment(directory) {
  const marker = path.join(directory, DEPLOYMENT_FILE);
  if (!fs.existsSync(marker)) {
    return null;
  }
  const deployment = readJson(marker);
  if (deployment.schemaVersion !== MANIFEST_SCHEMA_VERSION || !Array.isArray(deployment.files)) {
    throw new Error(`Unsupported deployment marker: ${marker}`);
  }
  return deployment;
}

function deploymentIsCurrent(directory, deployment, source) {
  if (!deployment) {
    return false;
  }
  if (
    deployment.packageVersion !== source.manifest.packageVersion ||
    deployment.packageName !== source.packageName ||
    deployment.releaseAssetSha256 !== source.manifest.release.assetSha256
  ) {
    return false;
  }
  return source.manifest.files.every((entry) => fs.existsSync(pathFromManifest(directory, entry.path)));
}

function removeEmptyParents(directory, root) {
  const resolvedRoot = path.resolve(root);
  let current = path.resolve(directory);
  while (current !== resolvedRoot && current.startsWith(`${resolvedRoot}${path.sep}`)) {
    try {
      fs.rmdirSync(current);
    } catch (error) {
      if (['ENOTEMPTY', 'ENOENT'].includes(error.code)) {
        return;
      }
      throw error;
    }
    current = path.dirname(current);
  }
}

function removeObsoleteManagedFiles(directory, previousFiles, nextFiles) {
  const nextPaths = new Set(nextFiles.map((entry) => normalizeRelativePath(entry.path).toLowerCase()));
  for (const entry of previousFiles) {
    const relativePath = normalizeRelativePath(entry.path);
    if (nextPaths.has(relativePath.toLowerCase())) {
      continue;
    }
    const target = pathFromManifest(directory, relativePath);
    if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
      continue;
    }
    if (/^[a-f0-9]{64}$/.test(entry.sha256 || '') && sha256(target) === entry.sha256) {
      fs.unlinkSync(target);
      removeEmptyParents(path.dirname(target), directory);
    }
  }
}

function deployPayload(source, targetDirectory) {
  const target = path.resolve(targetDirectory);
  const payload = path.resolve(source.payloadDir);
  ensureSafeTarget(target);
  if (target === payload || target.startsWith(`${payload}${path.sep}`)) {
    throw new Error('The SealDice instance directory cannot be inside the npm payload directory');
  }

  const previous = readDeployment(target);
  if (deploymentIsCurrent(target, previous, source)) {
    const staleDeployingMarker = path.join(target, DEPLOYING_FILE);
    if (fs.existsSync(staleDeployingMarker)) {
      fs.unlinkSync(staleDeployingMarker);
    }
    return { changed: false, target, binaryPath: pathFromManifest(target, source.manifest.platform.binary) };
  }

  const deployingMarker = path.join(target, DEPLOYING_FILE);
  writeFileAtomically(
    deployingMarker,
    `${JSON.stringify({ packageName: source.packageName, packageVersion: source.manifest.packageVersion })}\n`,
    0o644,
  );

  for (const entry of source.manifest.files) {
    const sourcePath = pathFromManifest(payload, entry.path);
    if (!fs.existsSync(sourcePath) || !fs.statSync(sourcePath).isFile()) {
      throw new Error(`Payload file is missing: ${entry.path}`);
    }
    if (fs.statSync(sourcePath).size !== entry.size) {
      throw new Error(`Payload file size does not match its manifest: ${entry.path}`);
    }
    if (sha256(sourcePath) !== entry.sha256) {
      throw new Error(`Payload file checksum does not match its manifest: ${entry.path}`);
    }
    const destination = pathFromManifest(target, entry.path);
    writeFileAtomically(destination, { source: sourcePath }, Number.parseInt(entry.mode, 8));
  }

  if (previous) {
    removeObsoleteManagedFiles(target, previous.files, source.manifest.files);
  }

  const deployment = {
    schemaVersion: MANIFEST_SCHEMA_VERSION,
    packageName: source.packageName,
    packageVersion: source.manifest.packageVersion,
    releaseTag: source.manifest.release.tag,
    releaseAsset: source.manifest.release.asset,
    releaseAssetSha256: source.manifest.release.assetSha256,
    files: source.manifest.files,
  };
  writeFileAtomically(
    path.join(target, DEPLOYMENT_FILE),
    `${JSON.stringify(deployment, null, 2)}\n`,
    0o644,
  );
  if (fs.existsSync(deployingMarker)) {
    fs.unlinkSync(deployingMarker);
  }

  return { changed: true, target, binaryPath: pathFromManifest(target, source.manifest.platform.binary) };
}

function signalExitCode(signal) {
  const number = os.constants.signals && os.constants.signals[signal];
  return number ? 128 + number : 1;
}

function spawnBinary(binaryPath, args, cwd, spawnImpl = spawn) {
  return new Promise((resolve, reject) => {
    const child = spawnImpl(binaryPath, args, {
      cwd,
      stdio: 'inherit',
      windowsHide: false,
    });
    child.once('error', reject);
    child.once('exit', (code, signal) => resolve(signal ? signalExitCode(signal) : code ?? 1));
  });
}

async function run(options = {}) {
  const source = resolvePlatformPackage(
    options.platform || process.platform,
    options.arch || process.arch,
    options.resolver || require.resolve,
  );
  const deployed = deployPayload(source, options.cwd || process.cwd());
  return spawnBinary(
    deployed.binaryPath,
    options.args || process.argv.slice(2),
    deployed.target,
    options.spawnImpl || spawn,
  );
}

if (require.main === module) {
  run()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      console.error(`sealdice: ${error.message}`);
      process.exitCode = 1;
    });
}

module.exports = {
  DEPLOYMENT_FILE,
  DEPLOYING_FILE,
  PLATFORM_PACKAGES,
  deployPayload,
  normalizeRelativePath,
  resolvePlatformPackage,
  run,
  sha256,
  spawnBinary,
};
