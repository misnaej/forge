bump: patch

- The git guards now read a quoted or escaped value with a space in it as the one word the shell passes on, so `git -C 'a b' push -f`, `git -C a\ b push -f` and `FOO='a b' git push -f` are caught by the force-push, no-verify and protected-branch guards instead of being misread as a different command.
- A restore that names a modified file through quotes or escapes — `git checkout -- 'a;b'`, `git checkout -- a\ b` — is now refused; it was judged against paths that do not exist, allowed, and overwrote the file.
- A line continuation inside or just before a quoted word (`git \`, newline, `'push' -f`) no longer hides the command.
- Commands the shell assembles from several words are checked the way they run: `eval 'git' 'push -f'`, `ssh host git push -f` (with ssh's own options read as ssh reads them) and `bash -c '"$@"' _ git push -f` or `bash -c 'git $1 $2' _ push -f`, slices such as `"${@:1}"` included.
- Inline interpreter code that runs git is caught when it is written with quoted parentheses or braces (`perl -e "system(q(git push))"`) and under `deno eval`.
- Fewer false blocks: a quoted message or refspec that merely contains a flag (`git commit -m 'x -n'`, `git push origin 'x -f'`) is no longer taken for that flag.
- Accepted cost: an `ssh` word read anywhere, not only as the command (`echo ssh h git push -f`), now has its following words checked as a remote command; such a false block costs a `!`.
