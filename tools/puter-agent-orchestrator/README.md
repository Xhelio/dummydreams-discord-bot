# DummyDreams Puter Multi-Agent Orchestrator (Phase 1)

This is a **local-only, read-only analysis orchestrator**. It runs up to seven role-specialized model requests concurrently through Puter's OpenAI-compatible endpoint. The default concurrency cap is three to reduce burst usage and make failures easier to isolate.

It is not yet an autonomous coding agent:
- It does not inspect or modify repository files.
- It does not run shell commands or tests.
- It does not create branches, commits, or pull requests.
- It cannot deploy or access staging/production.
- Model output is untrusted advice and must be reviewed.

## Requirements

- Python 3.11+
- A Puter account and a Puter auth token created from the account dashboard.
- Network access to Puter's API endpoint.

Puter's Node.js guide currently documents Node.js 24+ for its SDK. This first CLI milestone instead uses Puter's documented OpenAI-compatible endpoint and the Python OpenAI client, keeping the bot's own runtime dependencies untouched.

## Windows setup

Run from the repository root in PowerShell:

```powershell
py -3.11 -m venv .venv-puter-agents
.\.venv-puter-agents\Scripts\Activate.ps1
py -m pip install -r tools/puter-agent-orchestrator/requirements.txt
```

Create a Puter auth token in your Puter account dashboard. Treat it like a password. Set it only in the current PowerShell session:

```powershell
$env:PUTER_AUTH_TOKEN = Read-Host "Paste Puter auth token"
```

The value is held in the current shell environment; do not paste it into source files, chat, GitHub, screenshots, or logs. Close the shell to discard the session variable. For longer-term use, prefer a local secret manager rather than a committed `.env` file.

## Run safely

First run a dry-run. It makes no network request and does not need a token:

```powershell
py tools/puter-agent-orchestrator/orchestrator.py --task "Audit staging health-check design" --dry-run
```

Then run a small two-agent live test:

```powershell
py tools/puter-agent-orchestrator/orchestrator.py --task "Suggest a read-only checklist for validating staging health checks. Do not assume repository implementation details." --agents core,qa --concurrency 2
```

After the small test succeeds, run all seven roles:

```powershell
py tools/puter-agent-orchestrator/orchestrator.py --task "Review the proposed DummyDreams multi-agent workflow. Identify risks and acceptance checks; do not assume repository implementation details." --concurrency 3 --output reports/agent-review.json
```

Supported role keys: `core`, `tiktok`, `database`, `dashboard`, `qa`, `devops`, `architect`.

## Usage and privacy notes

- “Free for the developer” does not mean unlimited or cost-free for the signed-in Puter account. Puter describes a User-Pays model; check the current account terms, balance, model availability, and usage limits before running multiple calls.
- A Puter auth token is a credential that can act on your Puter account. Never commit it or expose it to the bot, Render, GitHub Actions, or a public frontend.
- Send only non-sensitive task descriptions. Do not send Discord tokens, database records, private user data, production logs containing identifiers, or proprietary source files.
- The orchestrator makes one independent chat-completion request per selected role. It does not share outputs between agents or guarantee that the model identifier is available to every account.
- Request failures are reported per agent. There is no automatic retry, to avoid silently multiplying usage.

## Validation

Run unit tests without a network connection:

```powershell
py -m unittest discover -s tools/puter-agent-orchestrator -p "test_*.py" -v
```

A successful dry-run and unit tests validate local orchestration only. A successful live request is still required to validate the Puter token, endpoint, model access, and account limits.

## Next milestone (not included)

Only after an operator-confirmed live test and a separate security review should we add read-only repository inspection, followed by tightly allow-listed local tools and per-task Git worktrees. Generated code must never execute automatically. PR creation and production deployment remain separate approval gates.
