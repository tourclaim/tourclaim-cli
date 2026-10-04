# Contributing

Thanks for helping. Bug reports and pull requests are welcome; for security problems, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Setup

```sh
npm install
npm test          # builds dist/, compiles the tests and runs them with node:test
npm run mock      # a mock API on http://127.0.0.1:4010 for trying the tool by hand
```

With the mock running, set `TOURCLAIM_API_URL=http://127.0.0.1:4010` and use `node dist/cli.js`. The links the tool prints stand in for the traveler's browser: opening the sign-in link approves the sign-in (the mock also approves it after two polls), and opening a draft's review link signs it.

## Rules for changes

- No runtime dependencies. Use Node's built-ins (`fetch`, `node:util` `parseArgs`, `node:crypto`, `node:fs`) and keep Node 18.3 working.
- Never print, log or accept on the command line an API key. Never share evidence without consent, and never sign for the traveler.
- Keep `--json` output and exit codes stable; document any change in README.md and CHANGELOG.md.
- When the API changes, update `openapi/connectors-v1.json` (`tourclaim schema > openapi/connectors-v1.json`), the types in `src/types.ts` and `src/fields.ts`, and the mock in `test/mock-server.ts`. `test/meta.test.ts` checks the field lists against the schema.
- Write plain, direct prose in docs and messages.
- Pin GitHub Actions to a full commit SHA with the version in a comment; Dependabot keeps them current.

Releases are described in [RELEASING.md](RELEASING.md).
