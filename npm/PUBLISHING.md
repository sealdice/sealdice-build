# Publishing SealDice to npm

The npm release is built from an existing stable GitHub Release. It does not
compile SealDice or initialize the repository submodules.

## First publish

The packages must exist before npm Trusted Publishing can be configured. The
initial release is therefore published locally by an npm user who can publish
the unscoped `sealdice` package and public packages under `@sealtrpg`.

```bash
npm login
python scripts/build-npm-packages.py --release-tag v1.5.1
python scripts/publish-npm-packages.py --dry-run
python scripts/publish-npm-packages.py
```

The publisher always publishes the five platform packages first and the main
package last. It can be rerun after a partial failure; an existing package is
skipped only when its Release tag and asset hashes match the local build.

## Trusted Publishing

After the first publish, configure a GitHub Actions trusted publisher in the
npm settings for all six packages:

- GitHub owner: `sealdice`
- Repository: `sealdice-build`
- Workflow filename: `npm-publish.yml`
- Allowed action: `npm publish`
- GitHub environment: leave blank

Packages:

- `sealdice`
- `@sealtrpg/sealdice-win32-x64`
- `@sealtrpg/sealdice-darwin-x64`
- `@sealtrpg/sealdice-darwin-arm64`
- `@sealtrpg/sealdice-linux-x64`
- `@sealtrpg/sealdice-linux-arm64`

Do not add an `NPM_TOKEN` secret to the workflow. Later releases use the GitHub
Actions OIDC token and receive npm provenance automatically.

## Later releases

Run the `Publish to npm` workflow. Leave `release_tag` blank to select the
latest stable GitHub Release, or enter an exact stable tag such as `v1.6.0`.
Drafts, GitHub prereleases, and tags other than `vX.Y.Z` are rejected.

The download code honors the standard `HTTPS_PROXY` and `HTTP_PROXY`
environment variables when a local proxy is required.
