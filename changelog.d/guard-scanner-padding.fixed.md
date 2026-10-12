bump: patch

The safety guards can no longer be slipped past by padding a wrapper command. Before, a guard looked back only about 240 characters to decide whether a command was wrapped (`bash -c "…"`, `ssh host "…"`, `bash <<EOF`), so `bash -o posix -o posix … -c "git push -f"` with enough harmless options let the force-push through. Wrapper detection now reads the whole simple command, still in linear time (a 50 KB one-line command is scanned in a fraction of a second). The newly blocked commands are wrapped commands whose payload a guard already blocks when run directly.
