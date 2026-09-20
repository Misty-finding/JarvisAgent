# JarvisAgent Project Context

- This repository targets Python 3.12 and uses a `src/` package layout.
- The primary package is `src/jarvis`.
- On Windows, use native PowerShell commands instead of Unix-only shell syntax.
- Keep runtime state under `.jarvis/runtime/` and session data under
  `.jarvis/sessions/`; never commit those generated files.
- Preserve user changes in a dirty worktree.
- After code changes, run the focused tests first, then `uv run pytest tests -q`.
- Run `uv run ruff check src tests scripts` and `uv run mypy src` before release.
