"""verify-forge-docstrings — verify docstring accuracy against code signatures.

Analyses Python files to ensure docstrings accurately document
function / method signatures in Google-style. Writes
``code_health/docstring_verification.log``.

Usage:

- ``verify-forge-docstrings`` — check files modified vs main.
- ``verify-forge-docstrings <path>`` — check a specific file.

Called by ``forge-precommit``; may also be invoked standalone by the
``forge:precommit-fixer`` agent to refresh just the docstring log.


Validation Rules
----------------

ERROR Severity (Must Fix):
    1. Parameter Mismatches:
       - Parameters in signature but not documented in Args section
       - Parameters documented in Args but not in signature
       - Parameter order doesn't match between signature and docstring

    2. Forbidden Patterns:
       - Documenting 'self' parameter in instance methods
       - Documenting 'cls' parameter in class methods
       - Explicit "Returns: None" in docstrings (omit Returns section instead)

    3. Simple Function Exception:
       - Simple methods (0 params beyond self, or @property) can omit Args
       - Single-parameter methods where param is clearly mentioned in summary

WARNING Severity (Should Review):
    1. Missing docstrings in public functions/classes/modules
    2. Missing Returns section when function returns a value (except simple methods)
    3. Function doesn't return but has Returns section (might be generator)

INFO Severity (Optional Suggestions):
    1. Verbose fixture docstrings:
       - Simple pytest fixtures with type hints don't need Returns section
       - Suggests using one-line summary instead

Special Cases:
    - Pytest fixtures (tmp_path, monkeypatch, etc.) are filtered from param checks
    - Private functions (starting with _) don't require docstrings
    - Abstract methods can document returns without implementation
    - @property decorators always treated as simple (no Args needed)
    - Simple fixtures: <= 5 statements, type hint, returns constant/simple object

File Selection Strategy:
    Automatically detects which files to check, trying in order:
    1. Files modified compared to 'main' branch (if it exists)
    2. Files modified compared to 'origin/main' (if it exists)
    3. Files modified in last commit (fallback)

Usage:
    # Check modified files (compared to base branch or last commit)
    verify-forge-docstrings

    # Check specific file
    verify-forge-docstrings path/to/file.py

Exit Codes:
    0: No errors found (warnings and info messages are acceptable)
    1: Errors found (parameter mismatches or syntax errors)

Integration:
    - Called automatically by pre-commit hook (.githooks/pre-commit)
    - Results are printed to stdout/stderr; only ERRORS block commits,
      WARNINGS and INFO are non-blocking
"""

import argparse
import ast
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from forge.git_utils import (
    capturing_to_step_log,
    configure_cli_logging,
    get_modified_files,
)


configure_cli_logging()
logger = logging.getLogger(__name__)

# Paths to exclude from docstring verification
# (test fixtures with intentionally bad docstrings, and setup scripts)
EXCLUDED_PATHS = (
    "test/scripts/",  # Test fixtures for verify_docstrings.py
    ".devcontainer/",  # DevContainer setup scripts
)


@dataclass
class Issue:
    """Represents a docstring issue found during verification.

    Attributes:
        file: Path to the file containing the issue.
        line: Line number where the issue occurs.
        function: Name of the function/class/module with the issue.
        severity: Issue severity level ('error', 'warning', or 'info').
        description: Human-readable description of the issue.
    """

    file: str
    line: int
    function: str
    severity: str  # 'error', 'warning', 'info'
    description: str


class DocstringVerifier(ast.NodeVisitor):
    """AST visitor to verify docstrings match function signatures.

    This visitor walks through the Abstract Syntax Tree (AST) of a Python file
    and checks that all docstrings accurately reflect the code they document.

    Attributes:
        filepath: Path to the file being verified.
        issues: List of issues found during verification.
        current_class: Name of the class currently being visited
            (None if not in a class).
    """

    # Complexity thresholds for simple fixtures
    MAX_SIMPLE_FIXTURE_STATEMENTS = 5
    MAX_MULTI_STATEMENT_SIMPLE_FIXTURE = 3

    def __init__(self, filepath: str) -> None:
        """Initialize the verifier.

        Args:
            filepath: Path to the file being verified.
        """
        self.filepath = filepath
        self.issues: list[Issue] = []
        self.current_class: str | None = None
        self._all_fixtures: frozenset[str] = self.PYTEST_FIXTURES

    def visit_Module(self, node: ast.Module) -> None:
        """Check module-level docstring.

        Args:
            node: AST Module node to check.
        """
        self._all_fixtures = (
            self.PYTEST_FIXTURES
            | self._collect_local_fixtures(node)
            | self._collect_conftest_fixtures()
        )

        docstring = ast.get_docstring(node)
        if not docstring:
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=1,
                    function="<module>",
                    severity="warning",
                    description="Missing module docstring",
                ),
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Check class docstring.

        Args:
            node: AST ClassDef node to check.
        """
        old_class = self.current_class
        self.current_class = node.name

        docstring = ast.get_docstring(node)
        if not docstring:
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=f"class {node.name}",
                    severity="warning",
                    description="Missing class docstring",
                ),
            )

        self.generic_visit(node)
        self.current_class = old_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Check function/method docstring.

        Args:
            node: AST FunctionDef node to check.
        """
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Check async function/method docstring.

        Args:
            node: AST AsyncFunctionDef node to check.
        """
        self._check_function(node)
        self.generic_visit(node)

    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Check a function or method node for docstring issues.

        Args:
            node: AST function or async function node to check.
        """
        func_name = node.name
        full_name = (
            f"{self.current_class}.{func_name}" if self.current_class else func_name
        )

        docstring = ast.get_docstring(node)
        if not self._has_required_docstring(node, full_name, docstring):
            return
        # The guard above records the finding and returns False for a
        # missing docstring; past it the docstring is present. Bind the
        # non-optional narrowing so the param/return checks below type-check.
        if docstring is None:
            return

        is_method = self.current_class is not None
        is_classmethod, is_abstractmethod = self._decorator_flags(node)
        sig_params = self._extract_signature_params(
            node,
            is_method=is_method,
            is_classmethod=is_classmethod,
        )
        doc_params, forbidden_params = self._extract_docstring_params(
            docstring,
            is_method=is_method,
            is_classmethod=is_classmethod,
        )

        if forbidden_params:
            self._record_forbidden(node, full_name, forbidden_params)

        is_simple = self._is_simple_method(node, set(sig_params), docstring)
        self._record_param_mismatches(
            node,
            full_name,
            sig_params,
            doc_params,
            is_simple=is_simple,
        )
        self._record_fixture_overdoc(node, full_name, sig_params, doc_params)
        self._check_return_type(
            node,
            docstring,
            full_name,
            is_abstractmethod=is_abstractmethod,
        )

    def _has_required_docstring(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        full_name: str,
        docstring: str | None,
    ) -> bool:
        """Return True if function has a docstring (further checks should run).

        Records a missing-docstring issue for public functions and returns False
        so callers skip the rest of the checks. Private functions silently skip.

        Args:
            node: AST function or async function node to check.
            full_name: Full qualified name of the function.
            docstring: The function's docstring or None.

        Returns:
            True if function has a docstring, False otherwise.
        """
        if docstring:
            return True
        if not node.name.startswith("_"):
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=full_name,
                    severity="warning",
                    description="Missing docstring for public function",
                ),
            )
        return False

    @staticmethod
    def _decorator_flags(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> tuple[bool, bool]:
        """Return (is_classmethod, is_abstractmethod) for a function.

        Args:
            node: AST function or async function node.

        Returns:
            Tuple of (is_classmethod, is_abstractmethod) flags.
        """
        is_classmethod = any(
            isinstance(d, ast.Name) and d.id == "classmethod"
            for d in node.decorator_list
        )
        is_abstractmethod = any(
            (isinstance(d, ast.Name) and d.id == "abstractmethod")
            or (isinstance(d, ast.Attribute) and d.attr == "abstractmethod")
            for d in node.decorator_list
        )
        return is_classmethod, is_abstractmethod

    def _record_forbidden(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        full_name: str,
        forbidden_params: set[str],
    ) -> None:
        """Append an issue for `self`/`cls` documented when they should be implicit.

        Args:
            node: AST function or async function node.
            full_name: Full qualified name of the function.
            forbidden_params: Set of parameters that shouldn't be documented.
        """
        params_str = "', '".join(sorted(forbidden_params))
        self.issues.append(
            Issue(
                file=self.filepath,
                line=node.lineno,
                function=full_name,
                severity="error",
                description=(
                    f"Parameters '{params_str}' should not be "
                    "documented (implicit in methods)"
                ),
            ),
        )

    def _record_param_mismatches(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        full_name: str,
        sig_params: list[str],
        doc_params: list[str],
        *,
        is_simple: bool,
    ) -> None:
        """Append issues for missing/extra params and parameter-order mismatches.

        Args:
            node: AST function or async function node.
            full_name: Full qualified name of the function.
            sig_params: Parameters from the function signature.
            doc_params: Parameters from the docstring.
            is_simple: Whether this is a simple method that can omit Args.
        """
        sig_set = set(sig_params) - self._all_fixtures
        doc_set = set(doc_params) - self._all_fixtures
        missing = sig_set - doc_set
        extra = doc_set - sig_set

        if missing:
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=full_name,
                    severity="warning" if is_simple else "error",
                    description=(
                        f"Parameters in signature but not documented: "
                        f"{', '.join(sorted(missing))}"
                    ),
                ),
            )
        if extra:
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=full_name,
                    severity="error",
                    description=(
                        f"Parameters documented but not in signature: "
                        f"{', '.join(sorted(extra))}"
                    ),
                ),
            )

        sig_ordered = [p for p in sig_params if p not in self._all_fixtures]
        doc_ordered = [p for p in doc_params if p not in self._all_fixtures]
        if (
            not missing
            and not extra
            and len(sig_ordered) > 1
            and sig_ordered != doc_ordered
        ):
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=full_name,
                    severity="error",
                    description=(
                        f"Parameter order mismatch - "
                        f"Signature: [{', '.join(sig_ordered)}], "
                        f"Docstring: [{', '.join(doc_ordered)}]"
                    ),
                ),
            )

    def _record_fixture_overdoc(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        full_name: str,
        sig_params: list[str],
        doc_params: list[str],
    ) -> None:
        """Append a warning if only-fixture params are needlessly documented.

        Args:
            node: AST function or async function node.
            full_name: Full qualified name of the function.
            sig_params: Parameters from the function signature.
            doc_params: Parameters from the docstring.
        """
        doc_fixtures = set(doc_params) & self._all_fixtures
        non_fixture_params = set(sig_params) - self._all_fixtures
        if non_fixture_params or not doc_fixtures:
            return
        self.issues.append(
            Issue(
                file=self.filepath,
                line=node.lineno,
                function=full_name,
                severity="warning",
                description=(
                    f"Pytest fixtures documented unnecessarily (can be removed): "
                    f"{', '.join(sorted(doc_fixtures))}"
                ),
            ),
        )

    def _extract_signature_params(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        is_method: bool,
        is_classmethod: bool,
    ) -> list[str]:
        """Extract parameter names from function signature.

        Args:
            node: AST function or async function node.
            is_method: Whether this is a class method.
            is_classmethod: Whether this has @classmethod decorator.

        Returns:
            List of parameter names in signature order (excluding implicit self/cls).
        """
        params = []
        args = node.args

        # Regular arguments
        for arg in args.args:
            # Skip 'self' in instance methods
            if is_method and arg.arg == "self":
                continue
            # Skip 'cls' in classmethods
            if is_classmethod and arg.arg == "cls":
                continue
            params.append(arg.arg)

        # *args
        if args.vararg:
            params.append(f"*{args.vararg.arg}")

        # Keyword-only arguments
        params.extend(arg.arg for arg in args.kwonlyargs)

        # **kwargs
        if args.kwarg:
            params.append(f"**{args.kwarg.arg}")

        return params

    def _extract_docstring_params(
        self,
        docstring: str,
        *,
        is_method: bool,
        is_classmethod: bool,
    ) -> tuple[list[str], set[str]]:
        """Extract parameter names from Google-style docstring.

        Args:
            docstring: The docstring text to parse.
            is_method: Whether this is a class method.
            is_classmethod: Whether this has @classmethod decorator.

        Returns:
            Tuple of (valid params in order, forbidden implicit params like
            'self'/'cls').
        """
        params = []
        forbidden = set()

        # Look for Args: section
        # Section headers should have minimal indentation (0-4 spaces)
        pattern = (
            r"\n\s*Args?:\s*\n(.*?)"
            r"(?:\n {0,4}(?:Returns?|Yields?|Raises?|"
            r"Notes?|Examples?|Attributes?):|\Z)"
        )
        args_section = re.search(
            pattern,
            docstring,
            re.DOTALL | re.IGNORECASE,
        )

        if not args_section:
            return params, forbidden

        args_text = args_section.group(1)

        # Match parameter lines: param_name, *args, or **kwargs
        # Google style supports both typed and untyped formats
        # Parameters at base indentation (4 spaces), not continuation (8+)
        param_pattern = r"^ {4}(\*?\*?\w+)\s*(?:\([^)]*\))?\s*:"

        for line in args_text.split("\n"):
            match = re.match(param_pattern, line)
            if match:
                param_name = match.group(1)
                # 'self' should not be documented in instance methods
                if (param_name == "self" and is_method) or (
                    param_name == "cls" and is_classmethod
                ):
                    forbidden.add(param_name)
                else:
                    params.append(param_name)

        return params, forbidden

    def _check_return_type(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        docstring: str,
        full_name: str,
        *,
        is_abstractmethod: bool = False,
    ) -> None:
        """Check if return type is documented when function returns something.

        Args:
            node: AST function or async function node.
            docstring: The function's docstring.
            full_name: Full qualified name of the function.
            is_abstractmethod: Whether this is an abstract method.
        """
        is_method = self.current_class is not None
        is_classmethod, _ = self._decorator_flags(node)
        sig_params = self._extract_signature_params(
            node,
            is_method=is_method,
            is_classmethod=is_classmethod,
        )
        # Check if this is a pytest fixture first - if so, use fixture-specific rules
        if self._is_pytest_fixture(node):
            is_simple = self._is_simple_test_fixture(node, set(sig_params))
        else:
            is_simple = self._is_simple_method(node, set(sig_params), docstring)

        # Check if function has return statements with values
        has_return_value = self._has_return_value(node)

        # Check if docstring has Returns section
        has_returns_section = bool(
            re.search(r"\n\s*Returns?:\s*\n", docstring, re.IGNORECASE),
        )

        # If function returns None explicitly or has no return
        returns_none = self._explicitly_returns_none(node)

        # Check for explicit "Returns: None" pattern which should be avoided
        # Matches "Returns:\n    None" with any amount of whitespace
        returns_none_documented = bool(
            re.search(
                r"\n\s*Returns?:\s*\n\s*None\s*$",
                docstring,
                re.IGNORECASE | re.MULTILINE,
            ),
        )
        if returns_none_documented:
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=full_name,
                    severity="error",
                    description=(
                        "Docstring explicitly documents 'Returns: None' - "
                        "omit Returns section for void functions"
                    ),
                ),
            )

        if (
            has_return_value
            and not returns_none
            and not has_returns_section
            and not is_simple
        ):
            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=full_name,
                    severity="warning",
                    description=(
                        "Function returns a value but has no Returns "
                        "section in docstring"
                    ),
                ),
            )

        if not has_return_value and has_returns_section and not returns_none:
            # This might be a generator with Yields instead
            has_yields = bool(
                re.search(r"\n\s*Yields?:\s*\n", docstring, re.IGNORECASE),
            )
            # Skip this check for abstract methods - they document returns
            # but have no implementation
            if not has_yields and not is_abstractmethod:
                self.issues.append(
                    Issue(
                        file=self.filepath,
                        line=node.lineno,
                        function=full_name,
                        severity="info",
                        description=(
                            "Docstring has Returns section but function "
                            "doesn't seem to return anything"
                        ),
                    ),
                )

        # Check for verbose fixture docstrings
        self._check_fixture_docstring_style(node, docstring)

    def _has_return_value(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """Check if function has return statements with values.

        Args:
            node: AST function or async function node.

        Returns:
            True if the function returns a non-None value, False otherwise.
        """
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Return)
                and child.value is not None
                and not (
                    isinstance(child.value, ast.Constant) and child.value.value is None
                )
            ):
                return True
        return False

    def _explicitly_returns_none(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        """Check if function explicitly returns None or has -> None annotation.

        Args:
            node: AST function or async function node.

        Returns:
            True if function explicitly returns None, False otherwise.
        """
        if node.returns:
            if isinstance(node.returns, ast.Constant) and node.returns.value is None:
                return True
            if isinstance(node.returns, ast.Name) and node.returns.id == "None":
                return True
        return False

    # Pytest fixtures that are commonly injected and don't need documentation
    PYTEST_FIXTURES = frozenset(
        {
            "tmp_path",
            "tmp_path_factory",
            "monkeypatch",
            "mocker",  # pytest-mock fixture
            "capsys",
            "capfd",
            "capfdbinary",
            "capsysbinary",
            "caplog",
            "tmpdir",
            "request",
            "cache",
            "record_property",
            "record_testsuite_property",
            "recwarn",
            "pytestconfig",
        },
    )

    def _is_simple_method(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        sig_params: set[str],
        docstring: str,
    ) -> bool:
        """Check if this is a simple method that can have a one-line docstring.

        Simple methods are allowed to have one-line docstrings without Args section:
        - Methods with no parameters (only self)
        - Methods with only pytest fixtures as parameters (e.g., tmp_path)
        - Methods with @property decorator
        - Methods like __str__, __repr__ with no params beyond self
        - Methods with 1 parameter where the parameter is mentioned in summary

        Args:
            node: AST function node.
            sig_params: Set of parameter names from signature (self already excluded).
            docstring: The function's docstring text.

        Returns:
            True if this is a simple method that can omit Args section.
        """
        # Filter out pytest fixtures - they don't need documentation
        sig_params = sig_params - self._all_fixtures

        # No parameters beyond self (and fixtures) - always simple
        if len(sig_params) == 0:
            return True

        # @property decorator - always simple (should have no params beyond self anyway)
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name) and decorator.id == "property":
                return True

        # Magic methods __str__ and __repr__ with no params - simple
        if node.name in ("__str__", "__repr__") and len(sig_params) == 0:
            return True

        # Single parameter that's clearly mentioned in the docstring summary
        # Only allow if parameter is obviously described (word boundary match)
        if len(sig_params) == 1:
            param_name = next(iter(sig_params))
            # Remove *args and **kwargs prefix for checking
            clean_param = param_name.lstrip("*")
            summary = docstring.split("\n", maxsplit=1)[0].lower()

            # Check for common obvious patterns:
            # "Returns {param}" or "Get {param}" or "{param} to use" etc.
            obvious_patterns = [
                f"returns {clean_param.lower()}",
                f"return {clean_param.lower()}",
                f"get {clean_param.lower()}",
                f"gets {clean_param.lower()}",
                f"for {clean_param.lower()}",
                f"the {clean_param.lower()}",
                f"{clean_param.lower()} to",
                f"{clean_param.lower()} for",
            ]

            for pattern in obvious_patterns:
                if pattern in summary:
                    return True

        return False

    def _collect_local_fixtures(self, module: ast.Module) -> frozenset[str]:
        """Collect names of @pytest.fixture functions defined in this module.

        Scans the module (and classes within it) for functions decorated with
        @pytest.fixture so they can be treated like built-in fixtures when
        checking parameter documentation in test methods.

        Args:
            module: The AST module node to scan.

        Returns:
            Frozenset of fixture function names found in this file.
        """
        local = set()
        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if self._is_pytest_fixture(node):
                local.add(node.name)
        return frozenset(local)

    def _collect_conftest_fixtures(self) -> frozenset[str]:
        """Collect @pytest.fixture names from conftest.py up the directory tree.

        Pytest auto-discovers fixtures from any conftest.py in the current
        test file's directory or any ancestor directory. This walks up from
        the file being checked, parsing each conftest.py it finds, so
        conftest fixtures are treated like built-in fixtures and don't get
        flagged as undocumented parameters.

        Returns:
            Frozenset of fixture function names found in ancestor conftest.py
            files. Returns an empty frozenset if the file path is not under a
            recognizable test root or no conftest.py files exist.
        """
        try:
            file_path = Path(self.filepath).resolve()
        except (OSError, ValueError):
            return frozenset()

        fixtures: set[str] = set()
        # Walk up directories looking for conftest.py, stopping at the
        # repository root (the dir containing pyproject.toml or .git).
        for directory in (file_path.parent, *file_path.parents):
            conftest = directory / "conftest.py"
            if conftest.is_file():
                try:
                    tree = ast.parse(conftest.read_text(encoding="utf-8"))
                except (SyntaxError, OSError):
                    continue
                for node in ast.walk(tree):
                    if isinstance(
                        node,
                        (ast.FunctionDef, ast.AsyncFunctionDef),
                    ) and self._is_pytest_fixture(node):
                        fixtures.add(node.name)
            # Stop walking up at the repo root
            if (directory / "pyproject.toml").exists() or (directory / ".git").exists():
                break

        return frozenset(fixtures)

    def _is_pytest_fixture(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """Check if this function has @pytest.fixture decorator.

        Recognises both bare and call forms:
            @pytest.fixture
            @pytest.fixture(scope="module")
            @fixture
            @fixture(scope="session")

        Args:
            node: AST function node.

        Returns:
            True if function has @pytest.fixture decorator.
        """
        for decorator in node.decorator_list:
            # Unwrap call form: @pytest.fixture(scope=...) → inspect the func
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            # Handle @pytest.fixture / @pytest.fixture(...)
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "pytest"
                and target.attr == "fixture"
            ):
                return True
            # Handle direct @fixture / @fixture(...) (if imported as fixture)
            if isinstance(target, ast.Name) and target.id == "fixture":
                return True
        return False

    def _is_simple_test_fixture(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        sig_params: set[str],
    ) -> bool:
        """Check if this is a simple test fixture that can have one-line docstring.

        Simple test fixtures are allowed to omit Returns section if:
        - Has @pytest.fixture decorator
        - Has return type annotation
        - Has no parameters (beyond pytest-injected fixtures)
        - Has simple body (< 5 statements, returns constant or simple object)

        Args:
            node: AST function node.
            sig_params: Set of parameter names from signature.

        Returns:
            True if this is a simple fixture that can omit Returns section.
        """
        # Must be a pytest fixture
        if not self._is_pytest_fixture(node):
            return False

        # Must have return type annotation
        if node.returns is None:
            return False

        # Should have no parameters beyond pytest fixtures
        non_pytest_params = sig_params - self._all_fixtures
        if non_pytest_params:
            return False  # Has real parameters, needs Args section

        # Check if body is simple (< 5 statements excluding docstring)
        body_statements = len(node.body)

        # Filter out docstring
        if (
            body_statements > 0
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        ):
            body_statements -= 1

        if body_statements > self.MAX_SIMPLE_FIXTURE_STATEMENTS:
            return False  # Too complex

        # Check if it's a simple return statement
        # Look for pattern: single return statement
        actual_body = (
            node.body[1:]
            if (len(node.body) > 0 and isinstance(node.body[0], ast.Expr))
            else node.body
        )

        if len(actual_body) == 1 and isinstance(actual_body[0], ast.Return):
            # Single return statement - this is simple
            return True

        # Multiple statements but still simple (e.g., create dict, return it)
        # Check if <= MAX_MULTI_STATEMENT_SIMPLE_FIXTURE statements and
        # last statement is return
        return (
            body_statements <= self.MAX_MULTI_STATEMENT_SIMPLE_FIXTURE
            and bool(actual_body)
            and isinstance(actual_body[-1], ast.Return)
        )

    def _check_fixture_docstring_style(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        docstring: str,
    ) -> None:
        """Check if simple fixture has unnecessarily verbose docstring.

        Issues INFO-level suggestion to simplify when a simple fixture
        has a Returns section (type hint already provides this info).

        Args:
            node: AST function node.
            docstring: The docstring text.
        """
        if not self._is_pytest_fixture(node):
            return

        # Check if this is a simple fixture with Returns section
        sig_params = self._extract_signature_params(
            node,
            is_method=False,
            is_classmethod=False,
        )
        has_returns = "Returns:" in docstring or "Return:" in docstring
        if self._is_simple_test_fixture(node, set(sig_params)) and has_returns:
            func_name = node.name

            self.issues.append(
                Issue(
                    file=self.filepath,
                    line=node.lineno,
                    function=func_name,
                    severity="info",
                    description=(
                        "Simple fixture has verbose docstring. "
                        "Consider one-line summary "
                        "(type hint already documents return type)."
                    ),
                ),
            )


def verify_file(filepath: Path) -> list[Issue]:
    """Verify docstrings in a single file.

    Args:
        filepath: Path to the Python file to verify.

    Returns:
        List of issues found in the file.
    """
    try:
        with filepath.open("r", encoding="utf-8") as f:
            content = f.read()

        tree = ast.parse(content, filename=str(filepath))
        verifier = DocstringVerifier(str(filepath))
        verifier.visit(tree)
    except SyntaxError as e:
        return [
            Issue(
                file=str(filepath),
                line=e.lineno or 0,
                function="<parse>",
                severity="error",
                description=f"Syntax error: {e.msg}",
            ),
        ]
    except (OSError, UnicodeDecodeError) as e:
        return [
            Issue(
                file=str(filepath),
                line=0,
                function="<parse>",
                severity="error",
                description=f"Error reading file: {e!s}",
            ),
        ]
    else:
        return verifier.issues


def _group_issues_by_severity(
    all_issues: list[Issue],
) -> tuple[list[Issue], list[Issue], list[Issue]]:
    """Split issues into errors, warnings, and info lists.

    Args:
        all_issues: All issues to categorize.

    Returns:
        Tuple of (errors, warnings, infos).
    """
    errors = [i for i in all_issues if i.severity == "error"]
    warnings = [i for i in all_issues if i.severity == "warning"]
    infos = [i for i in all_issues if i.severity == "info"]
    return errors, warnings, infos


def _short_path(full: str, repo_root_str: str) -> str:
    """Return *full* relative to repo root (best-effort).

    Args:
        full: Full path to shorten.
        repo_root_str: Repository root path string.

    Returns:
        Path relative to repo root.
    """
    return full.removeprefix(repo_root_str).lstrip("/")


def _log_at_level(
    level: int,
    label: str,
    issues: list[Issue],
    repo_root_str: str,
) -> None:
    """Log issues at a given severity level, one per line.

    Args:
        level: Logging level to emit at (e.g. `logging.ERROR`).
        label: Heading label for the group; should match the meaning of
            `level` (e.g. `"ERRORS"` with `logging.ERROR`).
        issues: Issues to log.
        repo_root_str: Repository root path string for shortening file paths.
    """
    if not issues:
        return
    logger.log(level, "%s (%s):", label, len(issues))
    for issue in issues:
        logger.log(
            level,
            "  %s:%s (%s): %s",
            _short_path(issue.file, repo_root_str),
            issue.line,
            issue.function,
            issue.description,
        )


def _log_warnings_grouped(warnings: list[Issue], repo_root_str: str) -> None:
    """Log warnings grouped by file.

    Args:
        warnings: List of warning-severity issues to log.
        repo_root_str: Repository root path string for shortening file paths.
    """
    if not warnings:
        return
    logger.warning("WARNINGS (%s):", len(warnings))
    by_file: dict[str, list[Issue]] = {}
    for issue in warnings:
        by_file.setdefault(issue.file, []).append(issue)
    for filepath_str, file_issues in sorted(by_file.items()):
        path = _short_path(filepath_str, repo_root_str)
        for issue in file_issues:
            logger.warning(
                "  %s:%s (%s): %s",
                path,
                issue.line,
                issue.function,
                issue.description,
            )


def _log_issues(
    errors: list[Issue],
    warnings: list[Issue],
    infos: list[Issue],
    repo_root: Path,
    *,
    file_count: int,
) -> None:
    """Log categorized issues and print a summary line.

    Args:
        errors: Error-severity issues.
        warnings: Warning-severity issues.
        infos: Info-severity issues.
        repo_root: Repository root for shortening paths.
        file_count: Number of files checked (for the summary).
    """
    repo_root_str = str(repo_root)
    # Warnings use file-grouped display; errors and infos are flat lists.
    _log_at_level(logging.ERROR, "ERRORS", errors, repo_root_str)
    _log_warnings_grouped(warnings, repo_root_str)
    _log_at_level(logging.INFO, "INFO", infos, repo_root_str)

    logger.info(
        "Checked %s files: %s errors, %s warnings, %s info",
        file_count,
        len(errors),
        len(warnings),
        len(infos),
    )
    if not (errors or warnings or infos):
        logger.info("No issues found.")


def main() -> int:
    """Main entry point for docstring verification.

    Returns:
        Exit code (0 for success, 1 for errors found).
    """
    parser = argparse.ArgumentParser(
        prog="verify-forge-docstrings",
        description=("Verify docstring accuracy against actual code signatures."),
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Optional file path to check. Defaults to modified files vs main.",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()

    with capturing_to_step_log(repo_root, "docstring_verification"):
        if args.target is not None:
            target = Path(args.target)
            if not target.is_absolute():
                target = repo_root / target
            if not target.exists():
                logger.error("File '%s' does not exist", target)
                return 1
            logger.info("Checking %s...\n", target)
            try:
                py_files = [str(target.relative_to(repo_root))]
            except ValueError:
                py_files = [str(target)]
        else:
            py_files = get_modified_files()

        # Remove duplicates, filter out excluded paths, and sort
        py_files = [
            f for f in py_files if not any(f.startswith(exc) for exc in EXCLUDED_PATHS)
        ]
        py_files = sorted(set(py_files))

        all_issues = []
        files_with_issues = []

        for filepath in py_files:
            candidate = Path(filepath)
            full_path = candidate if candidate.is_absolute() else repo_root / filepath
            if not full_path.exists():
                continue

            issues = verify_file(full_path)
            if issues:
                all_issues.extend(issues)
                files_with_issues.append(filepath)

        errors, warnings, infos = _group_issues_by_severity(all_issues)
        _log_issues(errors, warnings, infos, repo_root, file_count=len(py_files))

        return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
