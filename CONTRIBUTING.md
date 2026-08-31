# Contributing

## Setup

```bash
uv sync --dev
uv run pre-commit install --install-hooks -t pre-commit -t commit-msg
uv run pytest -q
```

The hooks are not optional decoration — CI runs `pre-commit run --all-files`, so
anything the hooks would have caught locally fails the pull request instead.

## Branch names

`rrt-branch-name` (pre-commit) and the `repo-release-tools` action (CI) both enforce
`<type>/<description>`. Allowed types: `feat`, `fix`, `chore`, `docs`, `refactor`,
`test`, `ci`, `perf`, `style`, `build`. Release branches are `release/v<version>` and are
created by `rrt bump`, not by hand.

## Commit subjects

Conventional Commits, using one of `feat`, `fix`, `chore`, `docs`, `refactor`, `test`,
`ci`, `perf`, `style`, `build`, `deps`.

`spec:` is **not** accepted, despite appearing in `.rrt.toml`'s `extra_commit_types`. The
pinned hook rejects it; `.rrt.toml` documents the measurement. Use `docs(spec): …`, which
routes to the same changelog section.

## Changelog

Every pull request must update `CHANGELOG.md`. `.rrt.toml` sets no `changelog_workflow`,
so rrt's default `incremental` applies and the CI check resolves to `per-commit`. Add
your entry under `## [Unreleased]` using the Keep a Changelog headings already in the
file (`Added`, `Changed`, `Fixed`, `Removed`).

## Tests

`uv run pytest -q`. With coverage: `uv run pytest --cov=src/ooxml_ledger
--cov-report=term-missing`. CI runs the suite on Python 3.13 and 3.14; the project floor
is 3.13.

## Releasing

Maintainers only.

1. `rrt bump <major|minor|patch>` — rewrites every version string
   (`pyproject.toml`, `src/ooxml_ledger/__init__.py`, `mcpb/manifest.json`, and three
   places in `server.json`: the top-level `version`, the pypi package's `version`, and
   the oci package identifier's `:<version>` suffix), moves `[Unreleased]` into a
   versioned section, refreshes `uv.lock`, and creates `release/v<version>`.
2. Open and merge the release PR. Merging to `main` publishes to TestPyPI.
3. Push the `v<version>` tag. That, and only that, publishes to PyPI, pushes and
   publishes the `ghcr.io/anselmoo/mcp-ooxml-ledger` Docker image, and cuts the GitHub
   Release.

Publishing uses OIDC trusted publishing. **No credential of any kind takes part in the
release path**, and none should ever be added to it: `publish-pypi`,
`mcp-registry-publish` and `release` authenticate by OIDC or by the auto-minted
`GITHUB_TOKEN` alone. The repository holds exactly one human-created secret,
`CODECOV_TOKEN`, and it is scoped to uploading a coverage report -- it cannot publish a
package, push an image, or write to this repository. A PreToolUse hook under
`.claude/hooks/` allows only that token and `GITHUB_TOKEN` in a workflow file and
refuses every other `secrets.*` reference. The trusted-publisher registration is bound to the workflow
filename `cicd.yml` and the environment names `pypi` / `testpypi`; renaming any of the
three breaks publishing until the registrations are updated by hand on PyPI. Pushing to
`ghcr.io` uses `GITHUB_TOKEN`, which GitHub Actions mints per job run — not a secret
anyone stores, and the correct credential for it.

### Required one-time manual step: make the ghcr.io package public

**New GitHub Container Registry packages default to private.** The first time
`docker-publish` pushes `ghcr.io/anselmoo/mcp-ooxml-ledger`, the package is created
private, and the very next job, `mcp-registry-publish`, will fail with:

```
400 Bad Request: registry validation failed for package 1
(ghcr.io/anselmoo/mcp-ooxml-ledger:X.Y.Z): OCI image is private or
requires authentication. Only public images are supported
```

This exact failure hit a sibling project, `Anselmoo/mcp-server-analyzer`, on
2026-08-30. There is **no reliable GitHub REST API** to flip a package's visibility —
the package resource has a `visibility` field but no documented endpoint sets it; the
only supported way is the web UI. Do not script around this with an unverified API
call.

**Before the first release that includes the Docker image publishes** (or immediately
after `docker-publish` runs once and `mcp-registry-publish` fails), a maintainer must:

1. Go to the repository's GitHub page → **Packages** → `mcp-ooxml-ledger`.
2. **Package settings** → **Danger Zone** → **Change visibility** → **Public**.
3. Re-run the failed `mcp-registry-publish` job (or push a new tag).

This is a **one-time** step per package: once public, later pushes of new tags to the
same package stay public.
