bump: patch

- A `conftest.py` edit confined to non-`autouse` fixtures now selects only the tests that request those fixtures (parameter, `getfixturevalue`, or `parametrize` name), instead of the whole folder subtree.
- Conftest edits that cannot be proven narrow — `autouse` fixtures, decorator or module-level changes, `pytest_generate_tests`, unparseable files — still select the whole subtree.
- A changed file that no longer parses now stays reachable in the selection graph (wide) instead of being silently dropped, which could report a false green.
