bump: patch
- The changelog fragment check now refuses a fragment whose body is not `- ` bullets (with indented continuations). The release assembler copies each body as written, so a fragment without bullets used to land in `CHANGELOG.md` as loose prose. Pending fragments written without bullets fail after upgrading: prefix each entry with `- `.
