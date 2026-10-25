# Standing instructions from Stefan

- **NEVER run git commands unless Stefan explicitly asks for them.**
  No autonomous commits, pushes, pulls, branch operations or cleanups — not
  even with tests passing, not "to keep main current", never as proactive
  cleanup. All git actions wait for a direct instruction in that session.

- Run the test suite (`cd atc && uv run pytest tests`) before every push.
