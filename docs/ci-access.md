# Consumer CI access

Forge is a public repo. Any CI runner — GitHub-hosted, self-hosted,
or third-party — can clone it without credentials. The standard
install line works as-is:

```yaml
- name: Install forge
  run: pip install --upgrade "git+https://github.com/misnaej/forge.git@main"
```

No SSH keys, no deploy keys, no PATs, no `gh auth setup-git`.

## If you fork forge to a private repo

If your fork is private and your CI needs to pull from it, use either:

**A — GitHub Actions secret + HTTPS token**

```yaml
- name: Configure git to use the token for HTTPS
  run: |
    git config --global url."https://x-access-token:${{ secrets.FORGE_READ_TOKEN }}@github.com/".insteadOf "https://github.com/"

- name: Install forge fork
  run: pip install --upgrade "git+https://github.com/<your-org>/<your-fork>.git@main"
```

The token is a fine-grained PAT with read-only contents on the fork.

**B — Deploy key (no PAT)**

1. On your fork → Settings → Deploy keys → add a read-only
   ed25519 key.
2. On the consumer repo → Settings → Secrets and variables → Actions
   → add `FORGE_DEPLOY_KEY` with the private half.
3. In the consumer workflow:
   ```yaml
   - name: SSH for forge fork
     uses: webfactory/ssh-agent@v0.9.0
     with:
       ssh-private-key: ${{ secrets.FORGE_DEPLOY_KEY }}
   ```
4. Use `git+ssh://git@github.com/<your-org>/<your-fork>.git@main` as
   the pip install URL.
