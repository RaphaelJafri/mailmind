# mailmind

Multi-agent Gmail manager built on Vertex AI / Gemini 2.5 / Google ADK / MCP, packaged as a Tauri desktop app.

The architectural rebuild ("v2") of [gmail-ops](https://github.com/raphaeljafri/gmail-ops) — same problem (local Gmail organization with read → classify → roll up → follow up → drafted reply → human-approved send), different shape: deterministic plumbing in Node, multi-agent reasoning in Python (ADK on Gemini), single-installer Tauri shell, MCP server so Claude Code / Cursor / ChatGPT can query the same knowledge graph.

## Status

Under active build. See [WORKPLAN-V2-EXECUTION.md](WORKPLAN-V2-EXECUTION.md) for the phase-by-phase plan and current state.

## Docs

- [WORKPLAN-V2.md](WORKPLAN-V2.md) — vision + phases (P0–P6).
- [WORKPLAN-V2-BUILD.md](WORKPLAN-V2-BUILD.md) — operational reference: schemas, prompts, process model, acceptance scripts, "ask the user" triggers.
- [WORKPLAN-V2-EXECUTION.md](WORKPLAN-V2-EXECUTION.md) — chronological build playbook.

## License

MIT (pending file).
