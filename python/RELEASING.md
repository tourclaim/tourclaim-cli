# Releasing the Python edition

The `tourclaim` package is published to PyPI by `.github/workflows/release-pypi.yml` when a tag such as `v0.2.0` is pushed. It uses PyPI trusted publishing: GitHub proves to PyPI which repository, workflow and environment is running, so no PyPI token is stored anywhere.

## Existing publishing setup

The package already exists on PyPI. The following settings document how its trusted publisher is configured; do not create a second pending publisher.

1. **Trusted publisher on PyPI.** Sign in at pypi.org, open *Your account > Publishing* (`https://pypi.org/manage/account/publishing/`) and add a pending publisher under *GitHub*:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `tourclaim` |
   | Owner | `tourclaim` |
   | Repository name | `tourclaim-cli` |
   | Workflow name | `release-pypi.yml` |
   | Environment name | `pypi` |

   The first successful publish creates the `tourclaim` project on PyPI and turns the pending publisher into a normal one. Until then, anyone could still register the name, so do this shortly before the first release.

2. **Create the `pypi` environment on GitHub.** In the repository settings, under *Environments*, add an environment named `pypi`. Adding yourself as a required reviewer means every publish waits for your approval. Restrict its deployment branches and tags to `v*` tags.

## Each release

1. Set the version in `python/src/tourclaim/__init__.py` (`__version__ = "X.Y.Z"`) and add a `## [X.Y.Z] - YYYY-MM-DD` section to `python/CHANGELOG.md`. The Node edition's `package.json`, `package-lock.json` and `src/version.ts` use the same version, because one tag releases both. Update `server.json` (server, package and extra dependency versions), MCP examples and the install commands in `README.md`, `python/README.md`, `MCP.md` and `llms.txt` to match.
2. Commit, and check that CI passes on all platforms.
3. Tag and push the tag:

   ```sh
   git tag v0.2.0
   git push origin v0.2.0
   ```

The workflow builds the sdist and wheel with `python -m build` in a job of their own, where nothing is installed but the build tools in `build-requirements.txt`, pinned by hash. Then, on another runner, it runs the Python tests, checks that the tag (without its `v`) equals `tourclaim.__version__` and checks the built files with `twine check`, then publishes them from the `pypi` environment. The test and MCP dependencies never run where the files are built. To change a build tool, update `build-requirements.txt` with every file's hash from PyPI (Dependabot does this). A tag that does not match the version fails before anything is published. PyPI never accepts the same version twice, so a broken release is fixed with a new version, not a re-upload.

## Checking a build locally

```sh
cd python
python -m venv .venv && .venv/bin/pip install -e ".[dev,mcp]" build twine
.venv/bin/python -m pytest
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*
```

## MCP Registry

After the protected PyPI job succeeds, the same workflow publishes `server.json` to the official MCP Registry using GitHub OIDC. No extra token is stored. Keep the PyPI README ownership marker `<!-- mcp-name: io.github.tourclaim/tourclaim -->` and the manifest name in sync. The package identifier remains `tourclaim`; `runtimeArguments` install its `mcp` extra and choose Python 3.12 for registry-generated `uvx` commands.

If the registry job fails after PyPI has published, fix the registry problem and rerun **failed jobs only**. Do not rerun the successful PyPI upload: a version cannot be uploaded twice. Confirm the entry at `https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.tourclaim/tourclaim` and smoke-test a clean installation before announcing availability. Registry indexing by individual client directories is separate.

The `pypi` environment currently requires the `tourclaim` reviewer. Approve its deployment in GitHub Actions after reviewing the tested release; the workflow deliberately waits at that gate. Do not bypass environment protections.
