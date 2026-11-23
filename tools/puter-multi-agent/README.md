# DummyDreams Puter Multi-Agent Workbench (Read-only MVP)

This is a local, browser-based, seven-role repository analysis orchestrator powered by Puter.js.

## Current capabilities
- Puter sign-in through the official Puter.js SDK.
- User-selected local repository folder using the browser File System Access API.
- Seven specialized roles: architect, core bot, TikTok, database, dashboard, QA/security, and DevOps/observability.
- Bounded concurrency: at most three agent requests at a time.
- Read-only tools exposed to agents: list indexed files, read an exact indexed file, and search indexed text.
- Per-agent output and failure reporting.

## Deliberate safety boundaries
- Read-only folder permission; no file writes, shell, subprocesses, Git operations, network access to the repository, deployment, or production credentials.
- Excludes .git, dependency/virtualenv/cache/build folders, .env files, secrets/credentials/tokens/private keys, database files, logs, and unsupported file types.
- Only files up to 100 KB are indexed/read; at most 500 eligible files and 10 MB total are indexed. Text search has a shared 20 MB read budget per folder selection.
- Repository contents that agents read are sent to Puter AI. Do not select workspaces containing secrets, personal data, or confidential material unless you have reviewed and accepted the provider's data handling terms.
- The user-pays model is not a guarantee of unlimited free usage. Account policy, availability, and possible usage charges/limits apply.
- This tool is an analysis orchestrator, not a full autonomous coding agent.

## Run on Windows
1. Use a current Chrome or Edge browser.
2. Check out the branch containing this directory.
3. Open PowerShell in `tools/puter-multi-agent`.
4. Run `py -m http.server 8766`.
5. Open `http://localhost:8766`.
6. Click **Login Puter**, then **Pilih folder repository** and choose a local clone of DummyDreams.
7. Enter a focused task and run the seven agents.

Stop the server with Ctrl+C. Use a focused task to avoid unnecessary context and model usage.

## Validation status
The source is committed in an isolated feature branch and PR. Live browser authentication/model calls require operator validation. This environment cannot complete the browser's interactive Puter sign-in or inspect the user's local filesystem.

## Next milestone (not included)
A separate, reviewed implementation may add explicit approval-based patch proposals and Git worktree creation. Any write capability must use path allow-lists, patch previews, backups, tests, and PR-only output. No agent should deploy to production.
