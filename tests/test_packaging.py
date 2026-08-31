import ooxml_ledger
from ooxml_ledger.errors import (
    OoxmlLedgerError,
    PackageError,
    VerificationError,
    XmlSecurityError,
)


def test_version_is_exposed():
    """A version exists and is a real release identifier.

    Deliberately NOT pinned to a literal. Both this and the test below asserted
    `"0.1.0.dev0"`, which meant they failed on the first release the project ever
    cut — a test that cannot survive the event it exists to protect. What matters
    is that the attribute is present and parses, not what it currently says.
    """
    from packaging.version import Version

    assert Version(ooxml_ledger.__version__)


def test_distribution_name_differs_from_import_name():
    """`mcp-ooxml-ledger` installs as `ooxml_ledger`, and the two agree on version.

    The distribution/import split is deliberate (design §7): the wire name advertises
    the protocol, the import name stays readable. The agreement is the real invariant
    — `rrt` maintains two version targets, and a release where they disagree is one
    where `importlib.metadata` and `__version__` tell a caller different things.
    """
    from importlib.metadata import version

    assert version("mcp-ooxml-ledger") == ooxml_ledger.__version__


def test_error_hierarchy():
    for cls in (PackageError, XmlSecurityError, VerificationError):
        assert issubclass(cls, OoxmlLedgerError)


def test_every_declared_version_agrees():
    """`server.json` and `mcpb/manifest.json` carry the project version too.

    Neither is a Python file, so neither is covered by `importlib.metadata` or by
    the test above. Both were `0.1.0.dev0` while `pyproject.toml` said `0.1.0`, and
    `rrt release check` passed regardless because neither was a configured target.
    `.rrt.toml` now keeps all four strings in sync on `rrt bump`; this test is the
    check that FAILS when they drift, rather than warning about it.

    `server.json` declares the version twice -- once at the top level and once
    inside `packages[0]` -- and both are asserted, because the registry reads the
    nested one and a human reads the outer one.
    """
    import json
    import pathlib

    import ooxml_ledger

    root = pathlib.Path(__file__).resolve().parent.parent
    expected = ooxml_ledger.__version__

    server = json.loads((root / "server.json").read_text(encoding="utf-8"))
    assert server["version"] == expected
    assert server["packages"][0]["version"] == expected

    manifest = json.loads((root / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == expected

    # server.json declares the version a THIRD time, inside the OCI package's
    # `identifier` (`ghcr.io/anselmoo/mcp-ooxml-ledger:0.1.0`) rather than as its
    # own `version` field -- the registry's oci packageType carries the version
    # embedded in the reference, not alongside it. `.rrt.toml` gained a second
    # pin_target on server.json (pattern anchored on the `ghcr.io/...:` prefix)
    # specifically so `rrt bump` reaches this string too; without a test pinned
    # to it, a regression in that pin target would drift silently exactly the
    # way the original three-string drift did before this test file existed.
    oci_packages = [p for p in server["packages"] if p.get("registryType") == "oci"]
    assert oci_packages, "server.json carries no oci package entry"
    for package in oci_packages:
        identifier = package["identifier"]
        assert identifier.rsplit(":", 1)[-1] == expected, (
            f"oci package identifier {identifier!r} does not end with the current "
            f"version {expected!r}"
        )


def test_registry_namespace_matches_the_repository_owner_exactly():
    """The MCP registry compares `server.json`'s name to the OIDC-granted namespace
    case-SENSITIVELY, so a casing slip is a release-time 403, not a warning.

    MEASURED 2026-08-30 against a sibling repository (Anselmoo/mcp-zen-of-languages,
    Actions run 33332718848). Its `server.json` declared
    `io.github.anselmoo/mcp-zen-of-languages` while the workflow's OIDC token granted
    `io.github.Anselmoo/*`. The registry refused:

        403 "You do not have permission to publish this server.
             You have permission to publish: io.github.Anselmoo/*.
             Attempting to publish: io.github.anselmoo/mcp-zen-of-languages"

    The match is a plain prefix comparison with no case folding, and the same slip was
    present in three of that author's MCP repositories at the time -- so this is a
    systemic trap, not one typo.

    Why this is a TEST and not only a CI step: the registry publish job runs
    `needs: [publish-pypi]`, so the 403 arrives AFTER the wheel is irreversibly on
    PyPI. `cicd.yml`'s `verify` job checks the same invariant against
    `github.repository_owner` (the authoritative casing) before anything ships; this
    test is its offline half, so the drift is caught by `pytest` and by pre-commit
    rather than only on a tag.

    The owner is derived from `pyproject.toml`'s Repository URL rather than hardcoded,
    so renaming the project or transferring it cannot leave this test asserting a
    stale name that happens to still pass.
    """
    import json
    import pathlib
    import re
    import tomllib

    root = pathlib.Path(__file__).resolve().parent.parent

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    repo_url = pyproject["project"]["urls"]["Repository"]
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+?)/?", repo_url)
    assert match, f"Repository URL is not a github.com project URL: {repo_url!r}"
    owner, repo_name = match.group(1), match.group(2)

    declared = json.loads((root / "server.json").read_text(encoding="utf-8"))["name"]
    expected = f"io.github.{owner}/{repo_name}"
    assert declared == expected, (
        f"server.json name {declared!r} != {expected!r}. The MCP registry prefix-matches "
        f"this against the OIDC-granted namespace WITHOUT case folding; a mismatch is a "
        f"403 at publish time, after PyPI has already published."
    )

    # The registry proves package ownership by fetching the PyPI-rendered description
    # (this README, via pyproject's readme=) and grepping for this exact marker. If it
    # drifts from server.json's name, ownership validation fails for a different reason
    # than the namespace check above -- so both are pinned to the same string.
    readme = (root / "README.md").read_text(encoding="utf-8")
    markers = re.findall(r"mcp-name:\s*(\S+)", readme)
    assert markers, (
        "README.md carries no `mcp-name:` marker; registry publish would fail"
    )
    assert markers[0] == declared, (
        f"README marker {markers[0]!r} != server.json name {declared!r}"
    )

    # A DIFFERENT, easily-conflated rule: OCI image references must be all-lowercase
    # (ghcr.io refuses a mixed-case repository path), while the `name` field checked
    # above must keep GitHub's exact `Anselmoo` casing. Asserting both in the same
    # test file, right next to each other, is deliberate -- a reader who "fixes" one
    # casing to match the other breaks a real invariant either way.
    server = json.loads((root / "server.json").read_text(encoding="utf-8"))
    oci_identifiers = [
        p["identifier"] for p in server["packages"] if p.get("registryType") == "oci"
    ]
    assert oci_identifiers, "server.json carries no oci package entry"
    for identifier in oci_identifiers:
        assert identifier == identifier.lower(), (
            f"oci package identifier {identifier!r} is not all-lowercase; ghcr.io "
            f"rejects a mixed-case image reference at push time"
        )
