# Releasing the Python edition

The `tourclaim` package is published to PyPI by `.github/workflows/release-pypi.yml` when a tag such as `v0.1.0` is pushed. It uses PyPI trusted publishing: GitHub proves to PyPI which repository, workflow and environment is running, so no PyPI token is stored anywhere.

## One-time setup (Tate)

1. **Register a pending trusted publisher on PyPI.** Sign in at pypi.org, open *Your account > Publishing* (`https://pypi.org/manage/account/publishing/`) and add a pending publisher under *GitHub*:

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

1. Set the version in `python/src/tourclaim/__init__.py` (`__version__ = "X.Y.Z"`) and add a `## [X.Y.Z] - YYYY-MM-DD` section to `python/CHANGELOG.md`. The Node edition's `package.json` uses the same version, because one tag releases both.
2. Commit, and check that CI passes on all platforms.
3. Tag and push the tag:

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

The workflow then runs the Python tests, checks that the tag (without its `v`) equals `tourclaim.__version__`, builds the sdist and wheel with `python -m build`, checks them with `twine check`, and publishes them from the `pypi` environment. A tag that does not match the version fails before anything is published. PyPI never accepts the same version twice, so a broken release is fixed with a new version, not a re-upload.

## Checking a build locally

```sh
cd python
python -m venv .venv && .venv/bin/pip install -e ".[dev]" build twine
.venv/bin/python -m pytest
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*
```
