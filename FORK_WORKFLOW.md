# Fork workflow

`origin` is Fede654/hermes-memory-kit. The original toolkit is
Mar-IA-no/hermes-memory-kit; fetch it read-only when comparing changes.

- `main` mirrors the original toolkit without fork-specific changes.
- `feat/integration` is the maintained runtime branch.
- Feature/fix branches start from `origin/feat/integration`. Publish them
  without force and open PRs targeting `feat/integration`.
- Keep integration ancestry when incorporating upstream changes. Do not rebuild
  or force-update the deployed branch.
- Hermes installations consume a reviewed 40-character commit, recorded in the
  Hermes fork's `fork-memory.json`. Advancing this branch alone never changes
  an installed runtime.

The root plugin is the profile-scoped Hermes provider. The `templates/` tree
also supports standalone toolkit workspaces. See
[installation and validation](docs/hermes-install.md).
