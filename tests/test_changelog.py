"""Tests for the ``forge.changelog`` module.

The single source of truth for recognizing ``## vX.Y.Z`` release headings,
shared by ``verify-forge-changelog-history``, ``forge-next-prep``, and
``forge-release``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import changelog
from tests.conftest import GIT_ENV, init_git_repo, init_single_track_repo


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# release_headings
# ---------------------------------------------------------------------------


def test_release_headings_parses_semver_level2_only() -> None:
    """``release_headings`` picks up ``## vX.Y.Z`` lines and ignores other markup."""
    text = (
        "# Changelog\n"
        "\n"
        "## v1.1.0\n"
        "\n"
        "Some prose about v1.1.0.\n"
        "\n"
        "### Sub-heading ignored\n"
        "\n"
        "## v1.0.0\n"
        "\n"
        "Not a heading: ## v0.9.0 inline\n"
    )
    assert changelog.release_headings(text) == {"v1.0.0", "v1.1.0"}


def test_release_headings_returns_empty_when_no_semver_headings() -> None:
    """No ``## vX.Y.Z`` lines in *text* returns an empty set."""
    text = "# Changelog\n\nSome unreleased notes.\n\n### Details\n"
    assert changelog.release_headings(text) == set()


def test_release_headings_matches_heading_with_trailing_suffix() -> None:
    """A heading with a trailing date suffix still matches on the tag alone."""
    text = "## v1.6.0 — 2026-07-01\n"
    assert changelog.release_headings(text) == {"v1.6.0"}


# ---------------------------------------------------------------------------
# changelog_lacks_entry
# ---------------------------------------------------------------------------


def test_changelog_lacks_entry_true_when_tag_absent() -> None:
    """A tag with no matching heading is reported as missing."""
    text = "## v1.0.0\n"
    assert changelog.changelog_lacks_entry(text, "v1.1.0") is True


def test_changelog_lacks_entry_false_when_tag_present() -> None:
    """A tag with a matching heading is reported as present."""
    text = "## v1.1.0\n\n## v1.0.0\n"
    assert changelog.changelog_lacks_entry(text, "v1.1.0") is False


def test_changelog_lacks_entry_true_on_empty_text() -> None:
    """Empty changelog text has no entries, so any tag is missing."""
    assert changelog.changelog_lacks_entry("", "v1.0.0") is True


# ---------------------------------------------------------------------------
# changelog_version_findings
# ---------------------------------------------------------------------------


def test_version_findings_clean_changelog_passes() -> None:
    """Dated headings, strictly decreasing, top == latest tag → no findings."""
    text = "# Changelog\n\n## v1.1.0 — 2026-07-01\n\n- a\n\n## v1.0.0\n\n- b\n"
    assert changelog.changelog_version_findings(text, "v1.1.0") == []


def test_version_findings_flags_unreleased_heading() -> None:
    """`## Unreleased` is not a recognized version — flagged, not ignored."""
    text = "## Unreleased\n\n## v1.0.0\n"
    findings = changelog.changelog_version_findings(text, "v1.0.0")
    assert any("## Unreleased" in f for f in findings)


def test_version_findings_flags_vless_and_truncated_headings() -> None:
    """v-less `## 1.0.0` and truncated `## v1.2` fail the shared recognizer."""
    text = "## 1.1.0\n\n## v1.2\n"
    findings = changelog.changelog_version_findings(text, None)
    assert len(findings) == 2
    assert all("not a valid vX.Y.Z" in f for f in findings)


def test_version_findings_invalid_only_omits_no_heading_message() -> None:
    """A flagged invalid heading suppresses the separate 'no heading' error."""
    findings = changelog.changelog_version_findings("## v1.2\n", None)
    assert len(findings) == 1
    assert "no `## vX.Y.Z` heading" not in findings[0]


def test_version_findings_empty_text_reports_no_heading() -> None:
    """No headings at all → the single 'no heading' finding."""
    findings = changelog.changelog_version_findings("# Changelog\n", None)
    assert findings == ["CHANGELOG.md has no `## vX.Y.Z` heading."]


def test_version_findings_duplicate_heading_breaks_monotonicity() -> None:
    """A duplicated version fails the strictly-decreasing rule."""
    text = "## v1.0.0\n\n## v1.0.0\n"
    findings = changelog.changelog_version_findings(text, None)
    assert any("not strictly decreasing" in f for f in findings)


def test_version_findings_out_of_order_headings_flagged() -> None:
    """An older version above a newer one fails monotonicity."""
    text = "## v1.0.0\n\n## v1.1.0\n"
    findings = changelog.changelog_version_findings(text, None)
    assert any("not strictly decreasing" in f for f in findings)


def test_version_findings_latest_tag_missing_entry() -> None:
    """The latest tag must have a matching heading."""
    text = "## v1.0.0\n"
    findings = changelog.changelog_version_findings(text, "v1.1.0")
    assert any("has no `## v1.1.0` heading" in f for f in findings)


def test_version_findings_top_behind_latest_tag() -> None:
    """Top heading older than the latest tag → 'behind' finding."""
    text = "## v1.0.0\n\n## v0.9.0\n"
    findings = changelog.changelog_version_findings(text, "v1.1.0")
    assert any("behind the latest tag v1.1.0" in f for f in findings)


def test_version_findings_no_tags_skips_tag_checks() -> None:
    """With no tags yet, only heading validity + ordering are checked."""
    text = "## v0.1.0\n"
    assert changelog.changelog_version_findings(text, None) == []


# ---------------------------------------------------------------------------
# stranded_added_versions
# ---------------------------------------------------------------------------


_STRAND_TEXT = (
    "# Changelog\n\n## v0.2.0 — 2026-07-01\n\n- new bullet\n\n## v0.1.0\n\n- old\n"
)

_STRAND_DIFF = (
    "--- a/CHANGELOG.md\n"
    "+++ b/CHANGELOG.md\n"
    "@@ -1,7 +1,9 @@\n"
    " # Changelog\n"
    " \n"
    " ## v0.2.0 — 2026-07-01\n"
    " \n"
    "+- new bullet\n"
    "+\n"
    " ## v0.1.0\n"
    " \n"
    " - old\n"
)


def test_stranded_detects_entry_under_released_heading() -> None:
    """Added bullet under a heading equal to the latest tag → stranded."""
    result = changelog.stranded_added_versions(_STRAND_TEXT, _STRAND_DIFF, "v0.2.0")
    assert result == ["v0.2.0"]


def test_stranded_silent_when_heading_leads_tag() -> None:
    """Same diff but the receiving heading is ahead of the latest tag → clean."""
    result = changelog.stranded_added_versions(_STRAND_TEXT, _STRAND_DIFF, "v0.1.9")
    assert result == []


def test_stranded_ignores_added_heading_lines_and_blanks() -> None:
    """A diff that only opens a new heading (plus blanks) strands nothing."""
    text = "## v0.3.0\n\n## v0.2.0\n\n- old\n"
    diff = (
        "--- a/CHANGELOG.md\n"
        "+++ b/CHANGELOG.md\n"
        "@@ -1,3 +1,5 @@\n"
        "+## v0.3.0\n"
        "+\n"
        " ## v0.2.0\n"
        " \n"
        " - old\n"
    )
    assert changelog.stranded_added_versions(text, diff, "v0.2.0") == []


def test_stranded_no_tags_returns_empty() -> None:
    """Without any release tag nothing can be stranded."""
    assert changelog.stranded_added_versions(_STRAND_TEXT, _STRAND_DIFF, None) == []


# ---------------------------------------------------------------------------
# top_release_heading
# ---------------------------------------------------------------------------


def test_top_release_heading_returns_first_version() -> None:
    """The topmost recognized heading wins, dated form included."""
    text = "# Changelog\n\n## v1.1.0 — 2026-07-24\n\n## v1.0.0\n"
    assert changelog.top_release_heading(text) == "v1.1.0"


def test_top_release_heading_skips_non_version_headings() -> None:
    """A stray non-version heading above the top version is skipped."""
    text = "## Unreleased\n\n## v1.0.0\n"
    assert changelog.top_release_heading(text) == "v1.0.0"


def test_top_release_heading_none_without_versions() -> None:
    """No recognized release heading → None."""
    assert changelog.top_release_heading("# Changelog\n") is None


# ---------------------------------------------------------------------------
# action_items
# ---------------------------------------------------------------------------


def test_action_items_extracts_marker_under_heading() -> None:
    """A single ``**Action:**`` line under a heading is attributed to it."""
    text = "## v1.2.0\n\n**Action:** do the thing.\n"
    assert changelog.action_items(text) == [("v1.2.0", "do the thing.")]


def test_action_items_attributes_to_nearest_governing_heading() -> None:
    """Two headings each with their own marker, independently in file order."""
    text = (
        "## v2.0.0\n"
        "\n"
        "**Action:** upgrade config.\n"
        "\n"
        "## v1.0.0\n"
        "\n"
        "**Action:** migrate data.\n"
    )
    assert changelog.action_items(text) == [
        ("v2.0.0", "upgrade config."),
        ("v1.0.0", "migrate data."),
    ]


def test_action_items_ignores_markers_above_first_heading() -> None:
    """A marker line in prose before any release heading is excluded."""
    text = "**Action:** should not count.\n\n## v1.0.0\n\n- normal bullet\n"
    assert changelog.action_items(text) == []


@pytest.mark.parametrize("bullet", ["- ", "* ", ""])
def test_action_items_accepts_optional_list_bullet(bullet: str) -> None:
    """The marker matches with a ``-`` bullet, a ``*`` bullet, or bare.

    Args:
        bullet: The list-bullet prefix (or empty string) placed before
            the ``**Action:**`` marker.
    """
    text = f"## v1.0.0\n\n{bullet}**Action:** do it.\n"
    assert changelog.action_items(text) == [("v1.0.0", "do it.")]


def test_action_items_returns_empty_when_no_markers() -> None:
    """Headings with no ``**Action:**`` lines yield an empty list."""
    text = "## v1.1.0\n\n- a bullet\n\n## v1.0.0\n\n- another bullet\n"
    assert changelog.action_items(text) == []


def test_action_items_ignores_near_miss_marker_text() -> None:
    """Text resembling the marker but missing the exact bold-colon shape is excluded."""
    text = (
        "## v1.0.0\n"
        "\n"
        "Action: not bolded, should not match.\n"
        "**Action**: colon outside bold, should not match.\n"
    )
    assert changelog.action_items(text) == []


def test_action_items_skips_action_under_unrecognized_heading() -> None:
    """Markers under unrecognized headings (e.g. `## Unreleased`) are excluded."""
    text = "## Unreleased\n\n**Action:** should be excluded.\n"
    assert changelog.action_items(text) == []


# ---------------------------------------------------------------------------
# wants_no_version
# ---------------------------------------------------------------------------
#
# Real git repos, not mocks: wants_no_version's branch-token and commit-tag
# signals are thin wrappers over `git branch --show-current` / `git log`, so
# a real repo is cheaper to reason about than a `run_git` transcript.


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A minimal git repo (`main`, one commit) for `wants_no_version` tests."""
    init_git_repo(tmp_path)
    return tmp_path


def _checkout_branch(repo: Path, name: str) -> None:
    """Create and switch to a new branch *name* in *repo*.

    Args:
        repo: Git repo working tree.
        name: Branch name to create and check out.
    """
    subprocess.run(
        ["git", "checkout", "-q", "-b", name], cwd=repo, env=GIT_ENV, check=True
    )


def _detach_head(repo: Path) -> None:
    """Detach HEAD in *repo* at its current commit (empties `--show-current`).

    Args:
        repo: Git repo working tree.
    """
    subprocess.run(
        ["git", "checkout", "-q", "--detach", "HEAD"], cwd=repo, env=GIT_ENV, check=True
    )


def _commit(repo: Path, message: str, *, allow_empty: bool = True) -> None:
    """Create a commit with *message* in *repo*.

    Args:
        repo: Git repo working tree.
        message: Commit message.
        allow_empty: Whether to pass `--allow-empty`, so tests don't need
            to stage real file changes just to produce a commit.
    """
    args = ["git", "commit", "-q", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    subprocess.run(args, cwd=repo, env=GIT_ENV, check=True)


def test_wants_no_version_env_no_version_truthy(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NO_VERSION=1` opts out via the env signal."""
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.setenv("NO_VERSION", "1")
    assert changelog.wants_no_version(git_repo) == "NO_VERSION env var set"


def test_wants_no_version_env_skip_changelog_check_truthy(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SKIP_CHANGELOG_CHECK=1` opts out via the legacy env signal."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.setenv("SKIP_CHANGELOG_CHECK", "1")
    assert changelog.wants_no_version(git_repo) == "SKIP_CHANGELOG_CHECK env var set"


@pytest.mark.parametrize("name", ["NO_VERSION", "SKIP_CHANGELOG_CHECK"])
@pytest.mark.parametrize("value", ["0", "false"])
def test_wants_no_version_env_falsy_value_does_not_opt_out(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """A leftover `NO_VERSION=0` / `SKIP_CHANGELOG_CHECK=false` etc. does NOT opt out.

    Args:
        name: The env var under test (`NO_VERSION` or `SKIP_CHANGELOG_CHECK`).
        value: The falsy string value assigned to `name`.
    """
    other = "SKIP_CHANGELOG_CHECK" if name == "NO_VERSION" else "NO_VERSION"
    monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, value)
    assert changelog.wants_no_version(git_repo) is None


def test_wants_no_version_branch_token_delimited_match(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delimited `no-version` token in the branch name opts out."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    _checkout_branch(git_repo, "chore/tidy-no-version")
    signal = changelog.wants_no_version(git_repo)
    assert signal is not None
    assert "chore/tidy-no-version" in signal


def test_wants_no_version_branch_token_non_delimited_no_match(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`no-versioning` is not a delimited token — the branch alone doesn't opt out."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    _checkout_branch(git_repo, "fix/no-versioning")
    assert changelog.wants_no_version(git_repo) is None


def test_wants_no_version_github_head_ref_fallback_when_detached(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a detached HEAD, `GITHUB_HEAD_REF` stands in for the missing branch name."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    _detach_head(git_repo)
    monkeypatch.setenv("GITHUB_HEAD_REF", "chore/x-no-version")
    signal = changelog.wants_no_version(git_repo)
    assert signal is not None
    assert "chore/x-no-version" in signal
    assert "GITHUB_HEAD_REF" in signal


def test_wants_no_version_github_head_ref_without_token_falls_through(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `GITHUB_HEAD_REF` without the `no-version` token doesn't opt out."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    _detach_head(git_repo)
    monkeypatch.setenv("GITHUB_HEAD_REF", "chore/plain")
    assert changelog.wants_no_version(git_repo) is None


def test_wants_no_version_local_branch_wins_over_github_head_ref(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local branch name takes precedence over GITHUB_HEAD_REF."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    _checkout_branch(git_repo, "feat/plain")
    monkeypatch.setenv("GITHUB_HEAD_REF", "chore/x-no-version")
    assert changelog.wants_no_version(git_repo) is None


def test_wants_no_version_commit_tag_match(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `[no-version]` tag in a commit message over `base..HEAD` opts out."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    _checkout_branch(git_repo, "feat/x")
    _commit(git_repo, "docs: tweak wording [no-version]")
    signal = changelog.wants_no_version(git_repo)
    assert signal is not None
    assert "[no-version]" in signal
    assert "main..HEAD" in signal


def test_wants_no_version_commit_tag_case_insensitive(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `[no-version]` tag match is case-insensitive."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    _checkout_branch(git_repo, "feat/y")
    _commit(git_repo, "chore: bump deps [NO-VERSION]")
    assert changelog.wants_no_version(git_repo) is not None


def test_wants_no_version_commit_tag_survives_ci_detached_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI simulation: no local base branch, base resolved via `origin/<base>`.

    Mirrors a CI checkout of a detached `refs/pull/N/merge`: no local
    `main` branch exists, but the fetch that created the checkout left
    `origin/main` behind — the commit-tag scan must still find it.
    """
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    work, _bare = init_single_track_repo(tmp_path)
    _checkout_branch(work, "feat/z")
    _commit(work, "fix: something [no-version]")
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "--detach", head_sha],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    signal = changelog.wants_no_version(work)
    assert signal is not None
    assert "origin/main" in signal


def test_wants_no_version_respects_configured_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[tool.forge].base_branch = "dev"` walks `dev..HEAD`, not `main..HEAD`."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    init_git_repo(tmp_path)
    _checkout_branch(tmp_path, "dev")
    (tmp_path / "pyproject.toml").write_text('[tool.forge]\nbase_branch = "dev"\n')
    subprocess.run(
        ["git", "add", "pyproject.toml"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    _commit(tmp_path, "chore: configure base branch", allow_empty=False)
    _checkout_branch(tmp_path, "feat/w")
    _commit(tmp_path, "feat: add thing [no-version]")
    signal = changelog.wants_no_version(tmp_path)
    assert signal is not None
    assert "dev..HEAD" in signal


def test_wants_no_version_no_signal_returns_none(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env, no branch token, no commit tag → None."""
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    _checkout_branch(git_repo, "feat/plain")
    _commit(git_repo, "feat: add plain thing")
    assert changelog.wants_no_version(git_repo) is None


def test_wants_no_version_flag_shaped_base_branch_skips_commit_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flag-shaped `[tool.forge].base_branch` is rejected, not passed to git.

    `resolve_base_branch_ref` refuses a `base_branch` starting with `-` outright
    (option injection guard) rather than handing it to `git rev-parse` /
    `git log`, so the commit-tag signal is skipped entirely — even though
    the `[no-version]` tag IS present in the commit log. Also asserts no
    file matching the injected flag's value was created in the repo,
    confirming git never executed the flag.
    """
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "--output=pwned"\n'
    )
    subprocess.run(
        ["git", "add", "pyproject.toml"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    _commit(tmp_path, "chore: configure flag-shaped base branch", allow_empty=False)
    _checkout_branch(tmp_path, "feat/injection")
    _commit(tmp_path, "feat: add thing [no-version]")

    assert changelog.wants_no_version(tmp_path) is None
    assert not any(p.name.startswith("pwned") for p in tmp_path.iterdir())


def test_wants_no_version_routes_through_resolve_base_branch_ref(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit-tag signal resolves its base via `resolve_base_branch_ref`.

    MOCK SETUP: `changelog.resolve_base_branch_ref` is replaced by a spy
    returning `None` (no base resolves), so the commit-tag scan is
    skipped entirely.
    EXPECTED BEHAVIOR: the spy is called with `(repo_root, "main")` — the
    single home for the origin-first policy, not a hand-rolled resolution
    in `changelog`.
    """
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    calls: list[tuple[object, object]] = []

    def _spy(root: object, base_branch: object) -> None:
        calls.append((root, base_branch))

    monkeypatch.setattr(changelog, "resolve_base_branch_ref", _spy)
    assert changelog.wants_no_version(git_repo) is None
    assert calls == [(git_repo, "main")]
