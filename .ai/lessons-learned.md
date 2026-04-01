# Lessons Learned — Workflow & Self-Improvement

Rules and patterns collected from project experience. Update this file after every correction or discovery.

---

### 2026-03-02 — UserEntraToken connection requires top-level `audience` property
**Mistake:** Connection Bicep only set `audience` inside `metadata{}`, but the Foundry runtime reads the top-level `properties.audience` field for token exchange.
**Root cause:** The ARM API schema has both `properties.audience` (used by runtime) and `properties.metadata.audience` (informational). Only setting the metadata one left the runtime field `null`, causing `"Missing required query parameter: audience"` at agent invocation time.
**Rule:** For `UserEntraToken` connections, always set `audience` at **both** `properties.audience` and `properties.metadata.audience` in the Bicep resource definition.

### 2026-03-02 — verify_deployment.py count discrepancy
**Discovery:** The script header says "31 checks" but `check_foundry_agent()` emits an extra sub-check ("Agent MCP tool has connection") making the actual total 32. Updated plan accordingly after observing 32/32 in output.
**Rule:** Trust the runtime count over the docstring; update the docstring when adding sub-checks.
