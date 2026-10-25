# Standing instructions from Stefan

- **Just do git.** Never ask for confirmation before committing, pushing,
  or other git operations. When work is complete and tests pass, commit and
  push to `origin main` proactively — reading status/diff/log is fine and
  expected, only the "shall I commit?" permission-seeking step is unwanted.

- Run the test suite (`cd atc && uv run pytest tests`) before every push.
