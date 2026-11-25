bump: patch

- A `conftest.py` edit confined to non-`autouse` fixtures now selects only the tests that request those fixtures (parameter or `getfixturevalue`), instead of the whole subtree; `autouse` fixtures, decorator or module-level edits, `pytest_generate_tests`, and anything unparseable still select the whole subtree.
