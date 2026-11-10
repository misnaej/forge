bump: patch

- The pre-commit ruff step no longer pulls files you left unstaged into a partial commit: after fixing, it re-stages only the files that were already staged. If ruff rewrites a file that is only partly staged, the commit stops before it is made and names the file, so you can stage the rest or undo the extra edits first.
