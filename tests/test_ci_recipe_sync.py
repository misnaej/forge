"""Keep the gitleaks step in forge's CI and in the shipped CI recipe identical.

Both files pin the scanner by version and digest, and dependabot updates
neither. A stale digest in the shipped recipe fails every consumer's CI, so
drift is caught here rather than by consumers.
"""

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_RECIPE = REPO_ROOT / "forge-docs" / "ci-recipe.md"

_FENCE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
_SEMVER = re.compile(r"\d+\.\d+\.\d+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _pin(text: str, key: str) -> str:
    """Return the quoted value of the line-anchored ``key:`` in text.

    Args:
        text: Source text to scan.
        key: Env variable name whose value is extracted.

    Returns:
        The value between the quotes; asserts exactly one uncommented
        ``key:`` line exists.
    """
    pattern = rf"^\s*{key}:\s*['\"]([^'\"]+)['\"]"
    assert len(re.findall(rf"^\s*{key}:", text, re.MULTILINE)) == 1, (
        f"expected exactly one uncommented {key}: line"
    )
    match = re.search(pattern, text, re.MULTILINE)
    assert match is not None, f"no {key} value found"
    return match.group(1)


def _raw_pin(text: str, key: str) -> str:
    """Return the unquoted value of the line-anchored ``key:`` in text.

    Args:
        text: Source text to scan.
        key: Env variable name whose value is extracted.

    Returns:
        The rest of the line after ``key:``; asserts exactly one uncommented
        ``key:`` line exists.
    """
    assert len(re.findall(rf"^\s*{key}:", text, re.MULTILINE)) == 1, (
        f"expected exactly one uncommented {key}: line"
    )
    match = re.search(rf"^\s*{key}:\s*(\S.*?)\s*$", text, re.MULTILINE)
    assert match is not None, f"no {key} value found"
    return match.group(1)


def _detect_line(text: str) -> str:
    """Return the ``./gitleaks detect`` command, continuations joined.

    Args:
        text: Source text to scan.

    Returns:
        The single detect command line with whitespace normalised.
    """
    joined = re.sub(r"\\\n", " ", text)
    pattern = r"^\s*(\./gitleaks detect .*)$"
    assert len(re.findall(pattern, joined, re.MULTILINE)) == 1, (
        "expected exactly one ./gitleaks detect line"
    )
    match = re.search(pattern, joined, re.MULTILINE)
    assert match is not None, "no ./gitleaks detect line found"
    return " ".join(match.group(1).split())


def _recipe_step() -> str:
    """Return the one yaml fence of the recipe that holds the gitleaks pins."""
    fences = [
        fence
        for fence in _FENCE.findall(CI_RECIPE.read_text())
        if "GITLEAKS_VERSION" in fence
    ]
    assert len(fences) == 1, (
        f"expected one fence with GITLEAKS_VERSION, got {len(fences)}"
    )
    return fences[0]


def _sources() -> dict[str, str]:
    """Return the scanned text of each file, keyed by repo-relative path."""
    return {
        str(CI_WORKFLOW.relative_to(REPO_ROOT)): CI_WORKFLOW.read_text(),
        str(CI_RECIPE.relative_to(REPO_ROOT)): _recipe_step(),
    }


def test_gitleaks_pins_match_between_ci_and_recipe() -> None:
    """Version and digest are equal in both files.

    Proves the two files agree, not that the digest is correct against the
    real release.
    """
    ci, recipe = _sources().values()
    assert _pin(ci, "GITLEAKS_VERSION") == _pin(recipe, "GITLEAKS_VERSION")
    assert _pin(ci, "GITLEAKS_SHA256") == _pin(recipe, "GITLEAKS_SHA256")


def test_gitleaks_detect_invocation_matches_between_ci_and_recipe() -> None:
    """Detect command is equal in both files, so flags like --redact stay in sync."""
    ci, recipe = _sources().values()
    assert _detect_line(ci) == _detect_line(recipe)


def test_gitleaks_base_ref_derivation_matches_between_ci_and_recipe() -> None:
    """BASE_REF is defined identically, so the scanned range cannot diverge."""
    ci, recipe = _sources().values()
    assert _raw_pin(ci, "BASE_REF") == _raw_pin(recipe, "BASE_REF")


@pytest.mark.parametrize(
    "name",
    [str(CI_WORKFLOW.relative_to(REPO_ROOT)), str(CI_RECIPE.relative_to(REPO_ROOT))],
)
def test_gitleaks_extraction_finds_values_in_both_files(name: str) -> None:
    """Each file yields a real-shaped version, digest and redacted detect line.

    Args:
        name: Repo-relative path selecting which file's text is checked.
    """
    text = _sources()[name]
    assert _SEMVER.fullmatch(_pin(text, "GITLEAKS_VERSION")), name
    assert _SHA256.fullmatch(_pin(text, "GITLEAKS_SHA256")), name
    detect = _detect_line(text)
    assert detect.startswith("./gitleaks detect"), name
    assert "--redact" in detect, name
    # A hardcoded base branch breaks consumers whose base is not that branch.
    assert "github.base_ref" in _raw_pin(text, "BASE_REF"), name


def test_gitleaks_extraction_ignores_commented_out_pin() -> None:
    """A commented-out pin before the real line is not what gets extracted."""
    text = "# GITLEAKS_VERSION: '1.0.0'\nGITLEAKS_VERSION: '2.3.4'\n"
    assert _pin(text, "GITLEAKS_VERSION") == "2.3.4"
