"""Hybrid provider runner layered on the read-only DummyDreams orchestrator.

Routes selected roles to local Ollama or cloud Puter. It does not access files,
run shell commands, modify Git, create PRs, or deploy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone

from orchestrator import ROLES, AgentResult, build_messages, select_roles

PUTER_BASE_URL = "https://api.puter.com/puterai/openai/v1/"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
DEFAULT_CLOUD_MODEL = os.getenv("PUTER_MODEL", "openai/gpt-5.3-codex")
DEFAULT_LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:1.5b")
DEFAULT_CLOUD_ROLES = ("architect", "qa")
DEFAULT_MAX_CLOUD_CALLS = 2
DEFAULT_MAX_TASK_CHARS = 12000


def validate_cloud_budget(assignments, max_cloud_calls: int):
    """Fail closed when a run would exceed its explicit cloud-call budget."""
    if max_cloud_calls < 0:
        raise ValueError("max_cloud_calls must be zero or greater")
    cloud_count = sum(provider == "puter" for _, provider in assignments)
    if cloud_count > max_cloud_calls:
        raise ValueError(
            f"Cloud budget exceeded: {cloud_count} Puter calls requested, "
            f"but --max-cloud-calls is {max_cloud_calls}. "
            "Reduce --cloud-agents/--agents or raise the limit explicitly."
        )


def route_roles(roles, provider: str, cloud_roles=DEFAULT_CLOUD_ROLES):
    """Return (role, provider) pairs; routing is deterministic and inspectable."""
    if provider not in {"hybrid", "puter", "ollama"}:
        raise ValueError("provider must be hybrid, puter, or ollama")
    cloud = set(cloud_roles)
    known = {role.key for role in ROLES}
    unknown = cloud - known
    if unknown:
        raise ValueError("Unknown cloud role(s): " + ", ".join(sorted(unknown)))
    if provider == "puter":
        return [(role, "puter") for role in roles]
    if provider == "ollama":
        return [(role, "ollama") for role in roles]
    return [(role, "puter" if role.key in cloud else "ollama") for role in roles]


async def _call(client, role, provider, model, semaphore, task, timeout):
    started = time.monotonic()
    try:
        async with semaphore:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=build_messages(role, task),
                    temperature=0.2,
                ),
                timeout=timeout,
            )
        content = response.choices[0].message.content or ""
        return AgentResult(
            agent=role.key, model=model, status="success", output=content,
            elapsed_seconds=round(time.monotonic() - started, 2),
        ), provider
    except Exception as exc:
        return AgentResult(
            agent=role.key, model=model, status="failed",
            error=f"{type(exc).__name__}: {exc}",
            elapsed_seconds=round(time.monotonic() - started, 2),
        ), provider


async def run_hybrid(assignments, task, cloud_model, local_model,
                     cloud_concurrency, local_concurrency, timeout, puter_token):
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError("Install tools/puter-agent-orchestrator/requirements.txt") from exc

    clients = {}
    semaphores = {
        "puter": asyncio.Semaphore(cloud_concurrency),
        "ollama": asyncio.Semaphore(local_concurrency),
    }
    if any(provider == "puter" for _, provider in assignments):
        if not puter_token:
            raise RuntimeError("PUTER_AUTH_TOKEN is required for cloud-routed roles.")
        clients["puter"] = AsyncOpenAI(
            api_key=puter_token, base_url=PUTER_BASE_URL,
            timeout=timeout, max_retries=0,
        )
    if any(provider == "ollama" for _, provider in assignments):
        clients["ollama"] = AsyncOpenAI(
            api_key="ollama-local", base_url=OLLAMA_BASE_URL,
            timeout=timeout, max_retries=0,
        )

    async def run_one(role, provider):
        model = cloud_model if provider == "puter" else local_model
        return await _call(
            clients[provider], role, provider, model, semaphores[provider],
            task, timeout,
        )

    try:
        return await asyncio.gather(*(run_one(role, provider) for role, provider in assignments))
    finally:
        await asyncio.gather(*(client.close() for client in clients.values()))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run DummyDreams agents across Puter and Ollama.")
    parser.add_argument("--task", required=True, help="Non-sensitive task description.")
    parser.add_argument("--agents", default=",".join(role.key for role in ROLES))
    parser.add_argument("--provider", choices=("hybrid", "puter", "ollama"), default="hybrid")
    parser.add_argument("--cloud-agents", default="architect,qa",
                        help="Roles sent to Puter in hybrid mode.")
    parser.add_argument("--cloud-model", default=DEFAULT_CLOUD_MODEL)
    parser.add_argument("--local-model", default=DEFAULT_LOCAL_MODEL)
    parser.add_argument("--cloud-concurrency", type=int, default=1)
    parser.add_argument("--local-concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-cloud-calls", type=int, default=DEFAULT_MAX_CLOUD_CALLS,
                        help="Hard cap on Puter requests per run (default: 2; 0 disables cloud).")
    parser.add_argument("--max-task-chars", type=int, default=DEFAULT_MAX_TASK_CHARS,
                        help="Reject oversized task prompts before sending them to providers.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    if min(args.cloud_concurrency, args.local_concurrency, args.timeout) <= 0:
        parser.error("concurrency and timeout values must be greater than zero")
    if args.max_cloud_calls < 0 or args.max_task_chars <= 0:
        parser.error("--max-cloud-calls must be >= 0 and --max-task-chars must be > 0")
    if len(args.task) > args.max_task_chars:
        parser.error(
            f"Task is {len(args.task)} characters; limit is {args.max_task_chars}. "
            "Summarize it or explicitly raise --max-task-chars."
        )
    try:
        roles = select_roles(name.strip() for name in args.agents.split(",") if name.strip())
        cloud_roles = tuple(name.strip() for name in args.cloud_agents.split(",") if name.strip())
        assignments = route_roles(roles, args.provider, cloud_roles)
        validate_cloud_budget(assignments, args.max_cloud_calls)
    except ValueError as exc:
        parser.error(str(exc))

    plan = [{
        "agent": role.key,
        "provider": provider,
        "model": args.cloud_model if provider == "puter" else args.local_model,
    } for role, provider in assignments]
    if args.dry_run:
        print(json.dumps({
            "mode": "dry-run", "provider_mode": args.provider,
            "cloud_concurrency": args.cloud_concurrency,
            "local_concurrency": args.local_concurrency,
            "max_cloud_calls": args.max_cloud_calls,
            "max_task_chars": args.max_task_chars,
            "routing": plan,
        }, indent=2))
        return 0

    try:
        results = asyncio.run(run_hybrid(
            assignments, args.task, args.cloud_model, args.local_model,
            args.cloud_concurrency, args.local_concurrency, args.timeout,
            os.getenv("PUTER_AUTH_TOKEN"),
        ))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    entries = []
    for (result, provider) in results:
        item = asdict(result)
        item["provider"] = provider
        entries.append(item)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.provider, "task": args.task, "routing": plan,
        "results": entries,
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        from pathlib import Path
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if all(item["status"] == "success" for item in entries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
