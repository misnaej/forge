bump: patch
- The changelog fragment check now refuses a fragment whose body lines are not `- ` bullets (or indented continuations). The release assembler copies each body as written, so a fragment without bullets used to land in `CHANGELOG.md` as loose prose.
