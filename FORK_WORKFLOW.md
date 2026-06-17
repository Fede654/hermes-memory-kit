# Fork Workflow — Fede654/hermes-memory-kit

This is Fede's fork of HMK, downstream of `Mar-IA-no/hermes-memory-kit`
(upstream). It mirrors the branch-topology discipline of the
`Fede654/hermes-agent` fork: **a single canonical branch that every deployed
agent tracks**, so the memory kit stays identical across agents while feature
work is in flight upstream.

## Layers

```
Mar-IA-no/hermes-memory-kit   (upstream — canonical HMK)
        │  fetch, read-only (origin)
        ▼
Fede654/hermes-memory-kit     (this fork — carries open-PR deltas on a
                               canonical feat/integration until they merge)
```

## Branch / remote roles

| Ref | Role | Rules |
|-----|------|-------|
| `origin/main` | `Mar-IA-no` upstream main | Read-only base. Never commit directly. |
| `fork` (`Fede654`) | This fork | Push target. |
| `feat/*` | One open PR each | Each applies cleanly over `origin/main`. Opened as a PR to upstream `main`. |
| `feat/integration` | **THE canonical deploy target** | What every deployed agent's HMK checkout tracks (`/opt/<agent>/hmk`). Rebuilt from `origin/main` + the cherry-picks of the currently-open `feat/*` PRs. Force-updated as PRs land or new deltas appear. |

## Current delta (open PRs carried on `feat/integration`)

| PR | Branch | What |
|----|--------|------|
| `Mar-IA-no/hermes-memory-kit#1` | `feat/memory-write-tools-and-distiller` | hmk-memory write tools (remember/recall) + `on_session_end` distiller + interlocutor tagging |
| `Mar-IA-no/hermes-memory-kit#2` | `feat/research-library` | `library-acquisition` skill + consolidated `library_extract.py` + librarian cross-refs |

`feat/integration` = `origin/main` + cherry-pick(#1) + cherry-pick(#2).

## Rebuild (when upstream advances or a PR merges)

```bash
git fetch origin
git checkout -B feat/integration origin/main
# drop any PR that has since merged upstream (it rides in from main now)
git cherry-pick <feat/memory-write-tools-and-distiller tip>   # if still open
git cherry-pick <feat/research-library tip>                   # if still open
git push fork +feat/integration:feat/integration
```

The delta should only ever **shrink** toward nothing as PRs merge upstream.

## Deploy to an agent

```bash
# on the agent's HMK checkout, e.g. /opt/chiwa/hmk
git remote add fork https://github.com/Fede654/hermes-memory-kit.git   # once
git fetch fork
git checkout -B feat/integration fork/feat/integration
# re-project templates/skills into the agent workspace if needed (bootstrap_agent.py)
```

Per-agent config (`.env`, `HMK_*` paths, embedding provider) stays out of git.
