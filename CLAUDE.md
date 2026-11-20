# Working on agent-memory-kb

Edit the source scripts in `scripts/` and starter notes in `templates/`. The initializer copies them into a user's knowledge base. Use a temporary root and synthetic conversations when verifying changes.

Run the pipeline suite from the repository root:

```sh
PERSONAL_KB_SKIP_KEYCHAIN=1 python3 -m unittest discover -s scripts -p 'test_*.py' -v
```

When changing setup, initialize both the dev and general profiles in fresh directories and run the installed doctor as shown in `.github/workflows/ci.yml`. Core indexing, search and backup must work with Python's standard library. MCP and semantic vectors are optional.

For the instructions that end users install in Claude Code, see `examples/agent-rules.md`.
