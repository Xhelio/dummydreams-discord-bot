# DummyDreams Hybrid Multi-Agent Runner (Phase 2 draft)

This extension adds explicit provider routing to the read-only Python orchestrator:
- **Ollama local** handles the default five routine specialist roles.
- **Puter cloud** handles Architect and QA/Security by default.
- Each provider has an independent concurrency cap (both default to 1).
- Routing can be inspected with `--dry-run` before any network request.

## Safety boundary
This remains analysis-only. It does not read repository files, write patches, execute shell commands, create Git branches/PRs, access staging/production, or deploy. Model output is untrusted advice. Do not send secrets, private user data, database records, or sensitive production logs.

Puter's User-Pays model does not guarantee unlimited free usage. A Puter auth token is a credential: keep it in the local shell environment and never commit it or place it in Render/GitHub Actions.

## Requirements
- Python 3.11+
- Existing dependencies from `requirements.txt`
- Ollama installed and running locally for local-routed roles
- Pull a compatible small model before first run, e.g. `ollama pull qwen2.5-coder:1.5b`
- Puter auth token only when cloud-routed roles are selected

## Windows quick start
From the repository root:

```powershell
py -3.11 -m venv .venv-puter-agents
.\.venv-puter-agents\Scripts\Activate.ps1
py -m pip install -r tools/puter-agent-orchestrator/requirements.txt
ollama pull qwen2.5-coder:1.5b
$env:PUTER_AUTH_TOKEN = Read-Host "Puter auth token"
```

Inspect routing first (no network calls):

```powershell
py tools/puter-agent-orchestrator/hybrid_orchestrator.py --task "Audit staging health checks" --dry-run
```

Then test only two agents:

```powershell
py tools/puter-agent-orchestrator/hybrid_orchestrator.py --task "Review staging health-check risks; do not assume repository details." --agents core,qa --provider hybrid --cloud-agents qa --cloud-concurrency 1 --local-concurrency 1
```

Run all seven only after the smoke test succeeds:

```powershell
py tools/puter-agent-orchestrator/hybrid_orchestrator.py --task "Review the current architecture and list prioritized risks." --provider hybrid --output reports/hybrid-review.json
```

Use `--provider ollama` for fully local operation or `--provider puter` for cloud-only operation. Configure `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, and `PUTER_MODEL` as needed.

## Validation gates
1. Run the existing offline unit tests and the new hybrid routing tests.
2. Run `--dry-run` and verify routing.
3. Confirm Ollama responds locally.
4. Run one local-only agent.
5. Run one cloud-only agent with a non-sensitive prompt.
6. Run the two-agent hybrid smoke test.
7. Keep this draft PR unmerged until all gates pass.

This phase intentionally does not add repository write access, autonomous shell execution, Git worktrees, PR creation, or deployment. Those require a separate security-reviewed phase with explicit human approval and staging-only validation.
