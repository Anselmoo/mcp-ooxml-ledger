"""`mcpb/build.sh`, `mcpb/.mcpbignore`, `README.md` and the CI `mcpb` job -- static content
checks, no bundle build, no network.

F02: `build.sh` ran `uv pip install --python 3.13 --target "$LIB_DIR" "$REPO_ROOT"`, a fresh
resolve that ignored `uv.lock` entirely, contradicting pyproject's "byte-exactly" comment. The
fix removes vendoring altogether -- the uv-type bundle (F01) installs from `uv.lock` with
`--frozen` at launch time, on the user's machine, so `build.sh` has nothing left to lock.

F03: README claimed the bundle "runs with a vendored Python runtime, no `uv` ... needed",
which was false even before F01/F02 (only libraries were vendored) and is now the opposite of
true (uv is required, and resolves the lock at first launch).

F06: `.mcpbignore`'s `*.dist-info/RECORD` never matched a nested path under the `ignore`
package's gitignore semantics (a bare pattern with no `/` matches at any depth, but adding a
directory segment anchors it to the top unless prefixed with `**/`). The uv-type bundle no
longer vendors `*.dist-info` directories at all, but the ignore file is rewritten with
correctly anchored `**/` patterns as defense-in-depth for whatever the staged tree contains.
"""

from pathlib import Path


def _code_lines(text: str, comment: str = "#") -> str:
    """Strip full-line and trailing comments so a check can't be fooled by prose that
    quotes the very pattern it says is gone (this file's own docstrings and build.sh's
    header do exactly that when explaining F02/F06)."""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith(comment)
    )


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SH = (REPO_ROOT / "mcpb" / "build.sh").read_text(encoding="utf-8")
BUILD_SH_CODE = _code_lines(BUILD_SH)
MCPBIGNORE = (REPO_ROOT / "mcpb" / ".mcpbignore").read_text(encoding="utf-8")
MCPBIGNORE_CODE = _code_lines(MCPBIGNORE)
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
CICD = (REPO_ROOT / ".github" / "workflows" / "cicd.yml").read_text(encoding="utf-8")


def test_server_main_py_is_removed():
    """F01's uv-type server has no vendored entry point script to launch."""
    assert not (REPO_ROOT / "mcpb" / "server" / "main.py").exists()


def test_build_script_does_not_vendor_dependencies():
    assert "uv pip install" not in BUILD_SH_CODE
    assert "--target" not in BUILD_SH_CODE
    assert "server/lib" not in BUILD_SH_CODE
    assert "LIB_DIR" not in BUILD_SH_CODE


def test_build_script_stages_the_uv_project_files():
    for staged in ("uv.lock", "pyproject.toml", "README.md", "LICENSE"):
        assert staged in BUILD_SH, f"build.sh must stage {staged} into the bundle"
    assert "src" in BUILD_SH


def test_build_script_no_longer_references_the_deleted_entry_point():
    assert "server/main.py" not in BUILD_SH


def test_mcpbignore_uses_doublestar_anchored_patterns():
    """The old `*.dist-info/RECORD` (no `**/` prefix) only matched a top-level path."""
    assert "**/__pycache__/" in MCPBIGNORE_CODE
    assert "**/*.pyc" in MCPBIGNORE_CODE
    # The exact buggy pattern must be gone, not merely supplemented.
    assert "*.dist-info/RECORD" not in MCPBIGNORE_CODE


def test_mcpbignore_excludes_dev_only_directories():
    for pattern in (".venv/", "tests/", "dist/", "analysis/"):
        assert pattern in MCPBIGNORE


def test_readme_does_not_claim_a_vendored_runtime():
    assert "vendored Python runtime" not in README


def test_readme_desktop_bundle_section_names_uv_as_a_requirement():
    section_start = README.index("Desktop bundle (.mcpb)")
    section = README[section_start : section_start + 1500]
    assert "uv" in section
    assert "uv.lock" in section


def test_cicd_mcpb_job_does_not_pin_a_vendored_abi_python():
    """F01's fallback (bounding `runtimes.python` to `<3.14` and pinning 3.13 everywhere)
    was not chosen; these pins existed only to match `build.sh`'s cp313-only vendoring, which
    no longer exists."""
    job_start = CICD.index("\n  mcpb:")
    next_job = CICD.index("\n  docker-publish:")
    job = CICD[job_start:next_job]
    assert "uv python install 3.13" not in job
    assert "--python 3.13" not in job
    assert "macos-latest" in job
    assert "smoke_mcpb.py" in job
