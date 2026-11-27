# A double quote in a C4 description breaks out of its Mermaid node label

- surface: src/forge/gen_c4.py _m
- release: forge 17.0.0
- seen: 2026-11-27
- branch: fix/c4-model-coverage-and-direction

## Evidence

`_m` is `html.escape(text, quote=False)` (`src/forge/gen_c4.py:1565`), so `&`,
`<` and `>` are escaped and `"` is not. Node labels are emitted as
`id["<label>"]`, so a `"` inside a description closes the label early and the
rest becomes Mermaid syntax.

Not reached today: no string in `c4.toml` contains a `"` — checked by walking
the parsed model, 0 hits — and the generated block carries none either. Found
by the security review of the model-coverage change, rated LOW and hypothetical.

The DSL side is unaffected: `_q` escapes `"` there.

## What it costs

Nothing yet. A consumer writing `description = "The \"single\" gate"` gets a
corrupted diagram, not an error — the drift check compares generated text to
committed text, so both sides carry the same broken label and `--check` stays
green. Silent, and the failure appears only when a human looks at the picture.

## What would fix it

Escape `"` in `_m` as Mermaid's own `#quot;` entity, not `&quot;` — the HTML
form renders literally inside a Mermaid label. Needs a rendering check that
the entity displays as a quote, which is why it was not folded into the
model-coverage change: forge's own output is byte-identical either way, so the
fix would ship untested against the case it exists for.

Not filed as an issue: FOUNDATION §1 keeps a hardening idea with no incident
out of the backlog until it recurs. File when a consumer hits it.
