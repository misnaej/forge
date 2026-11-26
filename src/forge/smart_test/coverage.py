"""Coverage-validated test selection.

Reads a coverage JSON export (``coverage json --show-contexts``, recorded
with per-test contexts via ``pytest --cov-context=test``) and returns the
tests whose contexts touch a changed source line. The caller unions that
set with the static import selection so tests linked to changed code only
at runtime — fixtures, dynamic dispatch, ``importlib`` indirection — are
still selected. A stale export under-selects, so regenerate it when the
source tree changes.

The export is read streaming: :func:`iter_export_files` yields one file's
entry at a time from a chunked buffer, so peak memory stays bounded by the
largest single entry instead of scaling with the whole export. A
whole-document parse measured ~1.8x the file size in peak RSS — enough to
get the process killed by the OS on large suites, with no output of its
own. A pre-flight check declines diagnosably when the export is too large
for the memory actually available, so a non-gating consumer degrades with
a message instead of dying on a signal.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from forge.git_utils import configure_cli_logging


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from typing import TextIO


configure_cli_logging()
logger = logging.getLogger(__name__)


# Whole-document json.loads of a coverage export measured ~1.8x the file
# size in peak RSS; the pre-flight guard uses 2.0 as a conservative bound
# for when parsing the export could not fit at all.
PARSE_RSS_FACTOR = 2.0
# Read-buffer granularity for the streaming parser.
READ_CHUNK_SIZE = 1 << 20
_KIB = 1024
# Characters that may legally follow a complete JSON value — including
# ``:``, which follows a key string; without it a key never counts as
# complete and the buffer refills to EOF on every member.
_DELIMITERS = " \t\r\n:,}]"


class ExportUnavailableError(Exception):
    """A coverage export cannot be used (missing, malformed, or too large).

    Consumers catch this and degrade to an empty result with a warning;
    it never reaches the CLI, whose reporters promise to always exit 0.
    """


def _fmt_bytes(size: int) -> str:
    """Render a byte count in human units, e.g. ``12.1 GB``.

    Args:
        size: The byte count.

    Returns:
        The count with a binary unit suffix.
    """
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < _KIB:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= _KIB
    return f"{value:.1f} TB"


def available_memory_bytes() -> int | None:
    """Return the best-effort available memory in bytes, or ``None``.

    The minimum of the cgroup v2 limit, Linux ``MemAvailable``, and total
    physical memory — the last is the only portable figure (macOS has no
    ``/proc``), and the minimum means a container limit or a contended
    host both lower the estimate. ``None`` when nothing can be determined,
    in which case the caller's pre-flight check is skipped.

    Returns:
        Available memory in bytes, or ``None`` when unknown.
    """
    candidates: list[int] = []
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text(encoding="utf-8").strip()
        if raw != "max":
            candidates.append(int(raw))
    except (OSError, ValueError):
        pass
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                candidates.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError):
        pass
    with contextlib.suppress(AttributeError, OSError, ValueError):
        candidates.append(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    return min(candidates) if candidates else None


class _StreamReader:
    """Incrementally decode JSON values from a chunked text stream.

    ``json`` has no streaming parser; this reads the file in chunks and
    applies :meth:`json.JSONDecoder.raw_decode` at a moving cursor,
    refilling whenever a decode fails for lack of a complete value. A
    decoded value is accepted only at EOF or when a delimiter follows it —
    a number is the only JSON token that can be a valid prefix of a longer
    one (``123`` vs ``1234``), so this rule keeps a number split across
    chunks from being read as a shorter value.
    """

    def __init__(self, handle: TextIO, chunk_size: int) -> None:
        """Bind the reader to an open text *handle* and a *chunk_size*.

        Args:
            handle: The open export file, read in ``chunk_size`` pieces.
            chunk_size: Read-buffer granularity in bytes.
        """
        self._handle = handle
        self._chunk_size = chunk_size
        self._buf = ""
        self._pos = 0
        self._eof = False
        self._decoder = json.JSONDecoder()

    def _fill(self) -> bool:
        """Append one chunk to the buffer, compacting the consumed prefix.

        Returns:
            ``True`` when a chunk was read, ``False`` at end of input.
        """
        if self._eof:
            return False
        chunk = self._handle.read(self._chunk_size)
        if not chunk:
            self._eof = True
            return False
        if self._pos:
            self._buf = self._buf[self._pos :]
            self._pos = 0
        self._buf += chunk
        return True

    def _skip_ws(self) -> None:
        """Advance the cursor past whitespace, reading more if needed."""
        while True:
            while self._pos < len(self._buf) and self._buf[self._pos] in " \t\r\n":
                self._pos += 1
            if self._pos < len(self._buf) or not self._fill():
                return

    def peek(self) -> str:
        """Return the next significant character, or ``""`` at EOF.

        Returns:
            The next non-whitespace character without consuming it.
        """
        self._skip_ws()
        if self._pos < len(self._buf):
            return self._buf[self._pos]
        return ""

    def take(self) -> str:
        """Consume and return the next significant character.

        Returns:
            The next non-whitespace character, or ``""`` at EOF.
        """
        char = self.peek()
        if char:
            self._pos += 1
        return char

    def expect(self, char: str) -> None:
        """Consume *char*, raising ``ValueError`` on anything else.

        Args:
            char: The single character the grammar requires here.

        Raises:
            ValueError: When the stream does not hold *char* next.
        """
        if self.take() != char:
            msg = f"expected {char!r} at stream offset {self._pos}"
            raise ValueError(msg)

    def decode(self) -> object:
        """Decode one complete JSON value at the cursor.

        Returns:
            The decoded value.

        Raises:
            ValueError: When the remaining input is not a complete value.
        """
        self._skip_ws()
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buf, self._pos)
            except json.JSONDecodeError:
                if self._fill():
                    continue
                raise
            if self._eof or (end < len(self._buf) and self._buf[end] in _DELIMITERS):
                self._pos = end
                return value
            if not self._fill():
                continue


def _iter_files_entries(reader: _StreamReader) -> Iterator[tuple[str, object]]:
    """Yield ``(filename, entry)`` pairs from a ``files`` object member.

    Args:
        reader: The stream positioned at the opening ``{``.

    Yields:
        Each file's name and its info object, in document order.

    Raises:
        ValueError: On malformed structure.
    """
    reader.expect("{")
    while True:
        char = reader.peek()
        if char == "}":
            reader.take()
            return
        if not char:
            msg = "unterminated files object"
            raise ValueError(msg)
        fname = reader.decode()
        if not isinstance(fname, str):
            msg = "files key is not a string"
            raise TypeError(msg)
        reader.expect(":")
        info = reader.decode()
        yield fname, info
        char = reader.peek()
        if char == ",":
            reader.take()
        elif char == "}":
            reader.take()
            return
        else:
            msg = f"malformed files object after {fname!r}"
            raise ValueError(msg)


def _iter_top_level(reader: _StreamReader) -> Iterator[tuple[str, object]]:
    """Yield ``(filename, entry)`` pairs from a coverage export's top level.

    Members other than ``files`` (``meta``, ``totals``, ``version``) are
    decoded and discarded, so only the per-file entries are ever held.

    Args:
        reader: The stream positioned at the opening ``{``.

    Yields:
        Each file's name and its info object, in document order.

    Raises:
        ValueError: On malformed structure.
    """
    reader.expect("{")
    while True:
        char = reader.peek()
        if char == "}":
            reader.take()
            return
        if not char:
            msg = "unterminated top-level object"
            raise ValueError(msg)
        key = reader.decode()
        if not isinstance(key, str):
            msg = "top-level key is not a string"
            raise TypeError(msg)
        reader.expect(":")
        if key == "files":
            yield from _iter_files_entries(reader)
        else:
            reader.decode()
        char = reader.peek()
        if char == ",":
            reader.take()
        elif char == "}":
            reader.take()
            return
        else:
            msg = f"malformed top-level object after {key!r}"
            raise ValueError(msg)


def iter_export_files(
    coverage_json: Path, *, chunk_size: int = READ_CHUNK_SIZE
) -> Iterator[tuple[str, object]]:
    """Stream ``(filename, file-info)`` entries from a coverage export.

    Peak memory is bounded by the largest single file entry plus one read
    chunk, not by the whole document. Raises :class:`ExportUnavailableError`
    when the export is missing, malformed, or too large for the available
    memory — the pre-flight check compares the file size times
    :data:`PARSE_RSS_FACTOR` against :func:`available_memory_bytes` and
    names both figures in its warning — or when parsing hits
    ``MemoryError``, so a non-gating consumer degrades diagnosably instead
    of being killed by a signal with no output.

    Args:
        coverage_json: Path to the coverage JSON export.
        chunk_size: Read-buffer granularity in bytes; tests shrink it to
            force chunk-boundary cases.

    Yields:
        ``(filename, file-info)`` pairs in document order.

    Raises:
        ExportUnavailableError: When the export cannot be streamed safely.
    """
    if not coverage_json.is_file():
        logger.warning(
            "coverage: %s not found — skipping the coverage-backed step.",
            coverage_json,
        )
        msg = f"missing export: {coverage_json}"
        raise ExportUnavailableError(msg)
    size = coverage_json.stat().st_size
    available = available_memory_bytes()
    if available is not None and size * PARSE_RSS_FACTOR > available:
        logger.warning(
            "coverage: %s is %s but only %s of memory is available and a "
            "whole-document parse would need ~%s — skipping the coverage-backed "
            "step (rerun on a less contended runner or export a smaller suite).",
            coverage_json,
            _fmt_bytes(size),
            _fmt_bytes(available),
            _fmt_bytes(int(size * PARSE_RSS_FACTOR)),
        )
        msg = f"export too large for available memory: {coverage_json}"
        raise ExportUnavailableError(msg)
    try:
        with coverage_json.open("r", encoding="utf-8") as handle:
            yield from _iter_top_level(_StreamReader(handle, chunk_size))
    except (OSError, TypeError, ValueError, RecursionError, MemoryError) as exc:
        logger.warning("coverage: could not parse %s: %s", coverage_json, exc)
        msg = f"unreadable export {coverage_json}: {exc}"
        raise ExportUnavailableError(msg) from exc


def _context_to_test(context: str) -> str | None:
    """Reduce a coverage context to a repo-relative test file path.

    A per-test context is the pytest node id (``tests/test_x.py::test_fn``);
    the file path is the segment before ``::``. The empty context (code run
    outside any test) and non-``.py`` contexts yield ``None``.

    Args:
        context: A coverage dynamic-context string.

    Returns:
        The test file path, or ``None`` when the context names no test file.
    """
    if not context:
        return None
    head = context.split("::", 1)[0]
    return head if head.endswith(".py") else None


def _entry_tests(info: dict[str, object]) -> set[str]:
    """Map one file entry's contexts to the test files covering it.

    Args:
        info: One ``files`` member of a coverage export.

    Returns:
        Repo-relative test files named by the entry's contexts.
    """
    tests: set[str] = set()
    contexts = info.get("contexts")
    for ctx_list in (contexts if isinstance(contexts, dict) else {}).values():
        if not isinstance(ctx_list, list):
            continue
        for ctx in ctx_list:
            if (test := _context_to_test(ctx)) is not None:
                tests.add(test)
    return tests


def tests_covering(coverage_json: Path, changed_files: Iterable[str]) -> set[str]:
    """Return repo-relative test files whose coverage touches a changed file.

    Streams the export via :func:`iter_export_files` and keeps only the
    entries whose filename is in *changed_files*. A missing, malformed, or
    oversized export yields an empty set with a warning — coverage
    validation never hard-fails the run.

    Args:
        coverage_json: Path to the coverage JSON export.
        changed_files: Repo-relative changed source paths.

    Returns:
        Repo-relative test files to union into the static selection.
    """
    changed = set(changed_files)
    tests: set[str] = set()
    try:
        for fname, info in iter_export_files(coverage_json):
            if fname in changed and isinstance(info, dict):
                tests |= _entry_tests(info)
    except ExportUnavailableError:
        return set()
    return tests
