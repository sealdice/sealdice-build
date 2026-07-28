# SealDice

This package installs the official SealDice desktop release for the current
platform and exposes the `sealdice` command.

## Install

```bash
npm install --global sealdice
```

## Run

SealDice stores its configuration, databases, logs, and bundled clients in the
current working directory. Create or enter a dedicated instance directory
before starting it:

```bash
mkdir my-sealdice
cd my-sealdice
sealdice
```

On the first run, and after an npm package upgrade, the command deploys the
official GitHub Release files into that directory. Files created by SealDice or
by the user are preserved. Release-managed binaries and built-in resources are
updated to the installed npm version.

The command refuses to deploy into an unrelated non-empty directory. An empty
directory, an existing SealDice directory, or a directory previously managed by
this npm package is accepted.

All command-line arguments are forwarded to `sealdice-core`:

```bash
sealdice --version
sealdice --address 0.0.0.0:3211
```

Supported platforms are Windows x64, macOS x64/arm64, and Linux x64/arm64.

Project website: https://sealdice.com

Source releases: https://github.com/sealdice/sealdice-build/releases
