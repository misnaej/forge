"""verify-forge-test-naming — verify test naming standards compliance.

Analyses test files to ensure they follow the forge testing standards
(see ``docs/testing-standards.md``). Writes
``code_health/test_naming_check.log``. Checks for:
- Test file `test_*` prefix
- Consistent error test naming patterns
- Helper functions with proper _ prefix or in conftest.py
- No duplicate/ambiguous file names
- Parametrization IDs using snake_case
- Module-level constants using UPPERCASE
- Descriptive fixture names (not generic like 'data', 'stuff')

All issues are reported as WARNINGS and do not block commits (exit code 0).

Issue Severity Levels:
    - WARNING: Test naming standard violations that should be reviewed

Usage:
    # Check modified files (compared to main/origin/main)
    verify-forge-test-naming

    # Check specific file
    verify-forge-test-naming test/path/to/test_file.py

File Selection Strategy:
    The script automatically detects which files to check, trying in order:
    1. Files modified compared to 'main' branch (if it exists)
    2. Files modified compared to 'origin/main' (if it exists)
    3. Files modified in last commit (fallback)

Exit Codes:
    0: Always (warnings only, never blocks)

References:
    - Consumer repos may override conventions in their own ``docs/`` and CLAUDE.md.
"""

import argparse
import ast
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from forge.git_utils import (
    capturing_to_step_log,
    configure_cli_logging,
    get_modified_files,
    get_tracked_files,
    repo_root,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


@dataclass
class Issue:
    """Represents a test naming issue found during verification.

    Attributes:
        file: Path to the file containing the issue.
        line: Line number where the issue occurs.
        function: Name of the function/constant with the issue.
        severity: Issue severity level (always 'warning' for this tool).
        description: Human-readable description of the issue.
    """

    file: str
    line: int
    function: str
    severity: str  # Always 'warning'
    description: str


class TestNamingVerifier(ast.NodeVisitor):
    """AST visitor to verify test naming standards.

    This visitor walks through the Abstract Syntax Tree (AST) of a test file
    and checks for violations of naming standards defined in the consumer's
    testing standards doc.

    Attributes:
        filepath: Path to the file being verified.
        issues: List of issues found during verification.
        source_content: Source code content for context.
        is_conftest: Whether this is a conftest.py file.
    """

    def __init__(self, filepath: str, source_content: str) -> None:
        """Initialize the verifier.

        Args:
            filepath: Path to the file being verified.
            source_content: Source code content.
        """
        self.filepath = filepath
        self.issues: list[Issue] = []
        self.source_content = source_content
        self.is_conftest = Path(filepath).name == "conftest.py"
        self.current_class_stack: list[str] = []  # Track nested classes

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Check function definition for naming issues.

        Args:
            node: AST FunctionDef node to check.
        """
        func_name = node.name

        # Check Rule 3: Consistent error test naming
        if func_name.startswith("test_") and (
            "error" in func_name or "raise" in func_name or "exception" in func_name
        ):
            self._check_error_test_naming(node, func_name)

        # Check Rule 4: Helper functions need _ prefix or conftest.py
        # Skip test functions, private functions, fixtures, class methods
        is_test = func_name.startswith("test_")
        is_private = func_name.startswith("_")
        is_fixture = self._is_fixture(node)
        is_class_method = len(self.current_class_stack) > 0

        if (
            not self.is_conftest
            and not is_test
            and not is_private
            and not is_fixture
            and not is_class_method
        ):
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=func_name,
                    severity="warning",
                    description=(
                        f"Helper function '{func_name}' should"
                        " start with '_' prefix or be moved"
                        " to conftest.py (Rule 4)"
                    ),
                ),
            )

        # Check Rule 7: Fixture names should be descriptive
        if is_fixture:
            self._check_fixture_name(node, func_name)

        # Check Rule 8: Parametrization IDs use snake_case
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and (
                isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "parametrize"
            ):
                self._check_parametrize_ids(node, decorator)

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Visit class definition and track class context.

        Args:
            node: AST ClassDef node to visit.
        """
        self.current_class_stack.append(node.name)
        self.generic_visit(node)
        self.current_class_stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        """Check module-level variable assignments for naming conventions.

        Args:
            node: AST Assign node to check.
        """
        # Check Rule 9: Module-level constants use UPPERCASE
        # Only check module-level assignments (col_offset typically 0)
        if node.col_offset == 0:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    var_name = target.id
                    # Skip private variables and already uppercase constants
                    if var_name.startswith("_") or var_name.isupper():
                        continue

                    # Check if it looks like a constant (assigned a literal value)
                    # Constants are usually assigned once and not modified
                    # This is heuristic-based
                    if self._looks_like_constant(node.value):
                        # If it has underscores or mixed case (like snake_case),
                        # it's likely intended as a constant
                        has_underscore = "_" in var_name
                        is_mixed_case = any(c.isupper() for c in var_name) and any(
                            c.islower() for c in var_name
                        )

                        if has_underscore or is_mixed_case:
                            self.issues.append(
                                Issue(
                                    file=self.filepath,
                                    line=node.lineno,
                                    function=var_name,
                                    severity="warning",
                                    description=(
                                        f"Module-level constant"
                                        f" '{var_name}' should use"
                                        " UPPERCASE naming (Rule 9)"
                                    ),
                                ),
                            )

        self.generic_visit(node)

    def _looks_like_constant(self, node: ast.expr) -> bool:
        """Check if an assignment value looks like a constant.

        Args:
            node: AST expression node to check.

        Returns:
            True if it looks like a constant value.
        """
        # Constants are typically: strings, numbers, lists, dicts, tuples, calls
        return isinstance(
            node,
            (
                ast.Constant,
                ast.List,
                ast.Dict,
                ast.Tuple,
                ast.Set,
                ast.Call,
                ast.ListComp,
            ),
        )

    def _is_fixture(self, node: ast.FunctionDef) -> bool:
        """Check if a function is decorated with pytest.fixture.

        Args:
            node: AST FunctionDef node to check.

        Returns:
            True if the function is a pytest fixture.
        """
        for decorator in node.decorator_list:
            # Check for @fixture or @pytest.fixture
            if isinstance(decorator, ast.Name) and decorator.id == "fixture":
                return True
            if isinstance(decorator, ast.Attribute) and decorator.attr == "fixture":
                return True
            # Check for @fixture() or @pytest.fixture()
            if isinstance(decorator, ast.Call):
                if (
                    isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "fixture"
                ):
                    return True
                if (
                    isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "fixture"
                ):
                    return True
        return False

    def _check_error_test_naming(self, node: ast.FunctionDef, func_name: str) -> None:
        """Check error test naming consistency.

        Args:
            node: AST FunctionDef node.
            func_name: Name of the function.
        """
        # Expected pattern: "test_action_condition_raises" with
        # optional "_exception_type" suffix.
        # Bad pattern: "test_action_raise" (missing 's').

        if (
            "raise" in func_name
            and not func_name.endswith("_raises")
            and not any(
                func_name.endswith(f"_raises_{ex}")
                for ex in [
                    "value_error",
                    "type_error",
                    "key_error",
                    "index_error",
                    "runtime_error",
                    "attribute_error",
                    "not_implemented_error",
                    "exception",
                ]
            )
            # Check if it has the wrong form (e.g., "_raise" instead of "_raises")
            and ("_raise_" in func_name or func_name.endswith("_raise"))
        ):
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=func_name,
                    severity="warning",
                    description=(
                        "Error test should use '_raises'"
                        " (not '_raise') pattern (Rule 3)"
                    ),
                ),
            )

    def _check_fixture_name(self, node: ast.FunctionDef, func_name: str) -> None:
        """Check fixture names for descriptiveness.

        Args:
            node: AST FunctionDef node.
            func_name: Name of the fixture.
        """
        # Generic names that should be avoided
        generic_names = {
            "data",
            "stuff",
            "thing",
            "value",
            "result",
            "item",
            "obj",
            "temp",
            "tmp",
        }

        # Check if fixture name is too generic (single word from generic list)
        if func_name in generic_names:
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=func_name,
                    severity="warning",
                    description=(
                        f"Fixture name '{func_name}' is too"
                        " generic. Use descriptive name like"
                        " 'dataset_with_missing_values'"
                        " (Rule 7)"
                    ),
                ),
            )

    def _check_parametrize_ids(
        self,
        node: ast.FunctionDef,
        decorator: ast.Call,
    ) -> None:
        """Check parametrization IDs for snake_case naming.

        Args:
            node: AST FunctionDef node.
            decorator: AST Call node for parametrize decorator.
        """
        # Find 'ids' keyword argument
        for keyword in decorator.keywords:
            # Check if it's a list
            if keyword.arg == "ids" and isinstance(keyword.value, ast.List):
                for _i, elt in enumerate(keyword.value.elts):
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        id_value = elt.value
                        # Check if it contains spaces or uses camelCase
                        if " " in id_value:
                            self.issues.append(
                                Issue(
                                    file=self.filepath,
                                    line=node.lineno,
                                    function=node.name,
                                    severity="warning",
                                    description=(
                                        f"Parametrize ID '{id_value}'"
                                        " should use snake_case"
                                        " (no spaces) (Rule 8)"
                                    ),
                                ),
                            )
                        # Check for camelCase (heuristic: capital letter not at start)
                        elif (
                            any(c.isupper() for c in id_value[1:])
                            and not id_value.isupper()
                        ):
                            self.issues.append(
                                Issue(
                                    file=self.filepath,
                                    line=node.lineno,
                                    function=node.name,
                                    severity="warning",
                                    description=(
                                        f"Parametrize ID '{id_value}'"
                                        " should use snake_case"
                                        " (not camelCase) (Rule 8)"
                                    ),
                                ),
                            )


def verify_file(filepath: Path) -> list[Issue]:
    """Verify test naming standards in a single file.

    Args:
        filepath: Path to the Python test file to verify.

    Returns:
        List of issues found in the file.
    """
    try:
        with filepath.open("r", encoding="utf-8") as f:
            content = f.read()

        tree = ast.parse(content, filename=str(filepath))
        verifier = TestNamingVerifier(str(filepath), content)
        verifier.visit(tree)

        # Additional file-level checks
        issues = verifier.issues

        # Check Rule 2: File names match module being tested
        issues.extend(_check_file_name_alignment(filepath))

    except SyntaxError as e:
        return [
            Issue(
                file=str(filepath),
                line=e.lineno or 0,
                function="<parse>",
                severity="warning",
                description=f"Syntax error: {e.msg}",
            ),
        ]
    except (OSError, UnicodeDecodeError) as e:
        return [
            Issue(
                file=str(filepath),
                line=0,
                function="<parse>",
                severity="warning",
                description=f"Error reading file: {e!s}",
            ),
        ]
    else:
        return issues


def _check_file_name_alignment(filepath: Path) -> list[Issue]:
    """Verify the ``test_`` prefix on test file names (Rule 2).

    Despite the legacy function name, this currently only checks the
    ``test_*`` prefix. Alignment of test file names with the source
    module under test is not enforced.

    Args:
        filepath: Path to the test file.

    Returns:
        List of issues found.
    """
    issues = []

    # Skip conftest.py and __init__.py
    if filepath.name in ("conftest.py", "__init__.py"):
        return issues

    # Test file should be named test_<module>.py
    if not filepath.name.startswith("test_"):
        issues.append(
            Issue(
                file=str(filepath),
                line=0,
                function="<file>",
                severity="warning",
                description="Test file should start with 'test_' prefix (Rule 2)",
            ),
        )

    return issues


def _check_duplicate_file_names(all_files: list[Path]) -> list[Issue]:
    """Check for duplicate or ambiguous file names.

    Args:
        all_files: List of all test file paths.

    Returns:
        List of issues found.
    """
    issues = []

    # Group files by their base name (without test_ prefix)
    name_groups: dict[str, list[Path]] = {}

    for filepath in all_files:
        if filepath.name in ("conftest.py", "__init__.py"):
            continue

        # Normalize: remove test_ prefix, convert to lowercase
        if filepath.name.startswith("test_"):
            base_name = filepath.name[5:].lower()  # Remove 'test_'
        else:
            base_name = filepath.name.lower()

        # Normalize underscores vs no underscores
        normalized = base_name.replace("_", "")

        if normalized not in name_groups:
            name_groups[normalized] = []
        name_groups[normalized].append(filepath)

    # Check for duplicates
    for file_list in name_groups.values():
        if len(file_list) > 1:
            # Check if they're in the same directory
            dirs = {f.parent for f in file_list}
            if len(dirs) == 1:
                # Same directory - definitely an issue
                issues.extend(
                    Issue(
                        file=str(filepath),
                        line=0,
                        function="<file>",
                        severity="warning",
                        description=(
                            "Duplicate/ambiguous file name."
                            " Found similar files:"
                            f" {', '.join(f.name for f in file_list)}"
                            " (Rule 5)"
                        ),
                    )
                    for filepath in file_list
                )

    return issues


SEPARATOR = "=" * 80


def _resolve_test_files(repo_root: Path, target: str | None, scope: str) -> list[str]:
    """Return repo-relative test file paths from CLI arg, scope, or git diff.

    Args:
        repo_root: Repository root path.
        target: Optional file path from the CLI. Overrides *scope*.
        scope: ``"all"`` (every tracked test file) or ``"diff"`` (test files
            modified vs main).

    Returns:
        List of repo-relative test file paths.
    """
    if target is not None:
        test_file = Path(target)
        if not test_file.is_absolute():
            test_file = repo_root / test_file
        if not test_file.exists():
            logger.error("File '%s' does not exist", test_file)
            return []
        logger.info("Checking %s...\n", test_file)
        try:
            return [str(test_file.relative_to(repo_root))]
        except ValueError:
            return [str(test_file)]
    prefix = ("test/", "tests/")
    if scope == "all":
        return get_tracked_files(prefix=prefix)
    return get_modified_files(prefix=prefix)


def _scan_files(
    py_files: list[str],
    repo_root: Path,
) -> tuple[list[Issue], list[str], int]:
    """Verify each file, plus a cross-file duplicate-name check.

    Args:
        py_files: Repo-relative test file paths.
        repo_root: Repository root.

    Returns:
        Tuple of (all issues, files that had issues, count of files scanned).
        The scan count excludes paths that didn't exist on disk.
    """
    all_issues: list[Issue] = []
    files_with_issues: list[str] = []
    all_file_paths: list[Path] = []

    for filepath in py_files:
        full_path = repo_root / filepath
        if not full_path.exists():
            continue
        all_file_paths.append(full_path)
        issues = verify_file(full_path)
        if issues:
            all_issues.extend(issues)
            files_with_issues.append(filepath)

    if all_file_paths:
        all_issues.extend(_check_duplicate_file_names(all_file_paths))
    return all_issues, files_with_issues, len(all_file_paths)


def _log_warnings(warnings: list[Issue]) -> None:
    """Print warnings grouped by file.

    Args:
        warnings: List of warning issues to display.
    """
    logger.info(SEPARATOR)
    logger.info("WARNINGS (Test naming standard violations)")
    logger.info(SEPARATOR)
    logger.info(
        "\nThese are suggestions based on the forge testing standards "
        "(docs/testing-standards.md).",
    )
    logger.info("Review and fix as appropriate.\n")

    by_file: dict[str, list[Issue]] = {}
    for issue in warnings:
        by_file.setdefault(issue.file, []).append(issue)

    for filepath_str, file_issues in sorted(by_file.items()):
        logger.info("\n%s:", filepath_str)
        for issue in file_issues:
            if issue.line > 0:
                logger.info(
                    "  Line %4d - %s: %s",
                    issue.line,
                    issue.function,
                    issue.description,
                )
            else:
                logger.info("  %s: %s", issue.function, issue.description)


def _report(
    files_scanned: int,
    all_issues: list[Issue],
    files_with_issues: list[str],
) -> None:
    """Print the verification summary.

    Args:
        files_scanned: Number of files scanned.
        all_issues: List of all issues found.
        files_with_issues: List of files that had issues.
    """
    warnings = [i for i in all_issues if i.severity == "warning"]

    logger.info(SEPARATOR)
    logger.info("TEST NAMING STANDARDS VERIFICATION REPORT")
    logger.info(SEPARATOR)
    logger.info("\nFiles checked: %d", files_scanned)
    logger.info("Files with issues: %d", len(set(files_with_issues)))
    logger.info("\nIssues found:")
    logger.info("  Warnings: %d", len(warnings))

    if warnings:
        _log_warnings(warnings)
        logger.info("\n%s", SEPARATOR)
        logger.info("SUMMARY: %d naming issues found", len(warnings))
        logger.info(SEPARATOR)
        logger.info(
            "\nFor detailed naming conventions, "
            "see the forge testing standards (docs/testing-standards.md)",
        )
    else:
        logger.info(SEPARATOR)
        logger.info("No test naming issues found!")
        logger.info(SEPARATOR)


def main() -> int:
    """Main entry point for test naming verification.

    Returns:
        Exit code (always 0 - warnings only).
    """
    parser = argparse.ArgumentParser(
        prog="verify-forge-test-naming",
        description="Verify test naming standards on auto-detected or given files.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Optional test file to check. Overrides --scope.",
    )
    parser.add_argument(
        "--scope",
        choices=("all", "diff"),
        default="all",
        help="'all' (every tracked test file, the default) or 'diff' (test "
        "files modified vs main). Ignored when a target is given.",
    )
    args = parser.parse_args()

    root = repo_root()

    with capturing_to_step_log(root, "test_naming_check"):
        py_files = sorted(set(_resolve_test_files(root, args.target, args.scope)))

        if not py_files:
            logger.info("No test files to check.")
            return 0

        logger.info("Checking %d test files...\n", len(py_files))
        all_issues, files_with_issues, files_scanned = _scan_files(py_files, root)
        _report(files_scanned, all_issues, files_with_issues)
        return 0


if __name__ == "__main__":
    sys.exit(main())
