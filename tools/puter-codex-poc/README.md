# Puter Codex proof of concept

This is an isolated, browser-based feasibility check for Puter.js Codex access. It deliberately does **not** modify the live bot, read the repository, execute generated code, access GitHub, create branches, or deploy anything.

## Why this is browser-first

Puter.js is a JavaScript SDK whose documented integration uses the signed-in user's Puter session and the User-Pays model. It is not a drop-in Python API key for a headless worker. Keep the first validation in a user-controlled browser session; do not copy browser session material into Python, Render, Discord, or GitHub Actions.

Official references:
- https://developer.puter.com/tutorials/free-unlimited-codex-api/
- https://developer.puter.com/ai/codex/
- https://docs.puter.com/

## Run on Windows

1. Clone the repository and check out branch `chore/puter-codex-poc` (or download this folder from the PR).
2. Open PowerShell in this folder.
3. Run `py -m http.server 8765`.
4. Open `http://localhost:8765` in a browser.
5. Select **Login Puter**, complete the sign-in flow, then select **Jalankan tes paralel (3 agent)**.
6. Record whether each of the three cards returns a response, the elapsed time, and any authentication/rate-limit error. Do not paste secrets or private repository contents into the prompts.

Stop the server with Ctrl+C.

## What this proves — and does not prove

The page checks:
- Puter authentication in a browser session.
- Whether the documented `openai/gpt-5.3-codex` model identifier is currently callable.
- Whether three independent requests can be submitted concurrently.
- Basic latency and per-request failure visibility.

It does **not** prove:
- Unlimited usage or zero cost for every account. Puter's User-Pays policy means the signed-in user is responsible for usage under Puter's current account terms, limits, and any applicable overage.
- A complete autonomous coding agent, tool calling, filesystem access, Git worktrees, or safe code execution.
- That Codex model access is equivalent to the Codex CLI/IDE agent product.

## Security and next gate

Do not add Puter session tokens, GitHub PATs, Discord tokens, Render secrets, or production data to this PoC. Do not deploy this page to the public bot dashboard.

Only after the operator confirms successful login and parallel responses should the next isolated milestone be designed: a local-only orchestrator with explicit task approval, per-agent worktrees, allow-listed tools, execution timeouts, audit logs, and PR-only output. Production deployment remains a human-controlled action after QA and staging verification.
