bump: patch

- A conftest edit to non-`autouse` fixtures selects only the tests requesting them.
- Edits that cannot be proven narrow still select the whole subtree.
- A changed file that no longer parses stays in the selection graph.
