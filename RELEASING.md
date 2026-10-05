# Releasing

Releases go to npm as [`tourclaim`](https://www.npmjs.com/package/tourclaim). After the first one, a release is a version bump and a pushed tag; GitHub Actions does the rest.

## First release (once, by hand)

npm only allows trusted publishing for a package that already exists, so the first version is published from a maintainer's machine.

1. Check out the release tag in a clean clone:

   ```sh
   git clone https://github.com/tourclaim/tourclaim-cli.git
   cd tourclaim-cli
   git checkout v0.1.0
   npm ci
   npm test
   ```

2. Publish:

   ```sh
   npm login
   npm publish --access public
   ```

   `prepack` builds `dist/` first. Check the file list it prints: only `dist/`, the docs and `package.json`.

3. On npmjs.com, open the `tourclaim` package, go to **Settings > Trusted publishing**, and add a GitHub Actions publisher: organization or user `tourclaim`, repository `tourclaim-cli`, workflow `release.yml`, no environment.

4. Optionally, under **Settings > Publishing access**, require two-factor authentication and disallow tokens, so only the workflow can publish.

## The schema snapshot

`openapi/connectors-v1.json` is a copy of the full schema the CLI uses, served at `/api/connectors/v1/openapi-cli.json` (`/api/connectors/v1/openapi.json` lists only the ten operations assistants load as tools). The types, the field tables and the test mock are checked against it. Refresh it after a server deploy that changes the API, then run the tests:

```sh
node dist/cli.js schema --api-url https://app.getcopernican.com > openapi/connectors-v1.json
npm test
```

The current copy was taken from production (server 1.57.6) on 2026-10-05.

## Every later release

1. Move the `Unreleased` notes in [CHANGELOG.md](CHANGELOG.md) under a new version heading with today's date, and commit.
2. Bump the version and push the tag:

   ```sh
   npm version <patch|minor|major|x.y.z>
   git push --follow-tags
   ```

   `npm version` updates `package.json`, `package-lock.json` and `src/version.ts`, then commits and tags `vX.Y.Z`.

3. The [Release workflow](.github/workflows/release.yml) runs the tests, checks that the tag equals the `package.json` version, and publishes with provenance through npm trusted publishing. No npm token is stored in the repository.

If the workflow fails before `npm publish`, fix the problem, delete the tag (`git push --delete origin vX.Y.Z` and `git tag -d vX.Y.Z`) and tag again. A published version cannot be replaced; release a new one.
