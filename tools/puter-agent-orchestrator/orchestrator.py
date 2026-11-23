"""Local-only, read-only multi-agent analysis orchestrator for DummyDreams.

This first milestone asks role-specialized agents for independent analysis.
It does not read or modify the repository, execute generated code, or deploy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


PUTER_BASE_URL = "https://api.puter.com/puterai/openai/v1/"
DEFAULT_MODEL = "openai/gpt-5.3-codex"


@dataclass(frozen=True)
class AgentRole:
    key: str
    title: str
    responsibility: str
    prompt: str


@dataclass
class AgentResult:
    agent: str
    model: str
    status: str
    output: str = ""
    error: str = ""
    elapsed_seconds: float = 0.0


ROLES: tuple[AgentRole, ...] = (
    AgentRole("core", "Core Bot", "Discord bot runtime and command/event flow",
             "Review the task from the perspective of Discord bot architecture, lifecycle, commands, event handling, and backward compatibility."),
    AgentRole("tiktok", "TikTok Engine", "TikTok LIVE connectivity and event processing",
             "Review the task from the perspective of TikTok LIVE sessions, reconnect behavior, event parsing, rate limits, and resilience."),
    AgentRole("database", "Database", "Persistence, schema, transactions, and query safety",
             "Review the task from the perspective of data integrity, schema compatibility, transactions, concurrency, and query performance."),
    AgentRole("dashboard", "Admin Dashboard", "Flask routes, API, UI, and monitoring",
             "Review the task from the perspective of dashboard/API behavior, authentication boundaries, usability, and observability."),
    AgentRole("qa", "QA & Security", "Regression, authentication, and security review",
             "Review the task from the perspective of security, regression risk, test coverage, abuse cases, and failure modes."),
    AgentRole("devops", "DevOps & Observability", "Staging, deployment, logs, and runtime health",
             "Review the task from the perspective of environment separation, CI, deployment safety, logs, metrics, and rollback."),
    AgentRole("architect", "Lead Architect", "Cross-component architecture and integration",
             "Review cross-component dependencies, interface contracts, sequencing, duplication, and integration risks."),
)


def select_roles(names: Iterable[str] | None = None) -> list[AgentRole]:
    requested = list(names or [role.key for role in ROLES])
    known = {role.key: role for role in ROLES}
    unknown = sorted(set(requested) - set(known))
    if unknown:
        raise ValueError("Unknown agent(s): " + ", ".join(unknown))
    if len(requested) != len(set(requested)):
        raise ValueError("Agent names must be unique.")
    return [known[name] for name in requested]


def build_messages(role: AgentRole, task: str) -> list[dict[str, str]]:
    system = (
        f"You are {role.title}, a specialist agent for the DummyDreams Discord Bot. "
        f"Your scope: {role.responsibility}. {role.prompt} "
        "Work in analysis-only mode. You have no repository or execution tools. "
        "Never claim to have inspected files, run tests, changed code, or deployed. "
        "Clearly separate facts supplied in the task from assumptions. "
        "Return: (1) findings, (2) recommended actions ordered by priority, "
        "(3) risks/dependencies, (4) concrete acceptance checks. Be concise."
    )
    user = (
        "Analyze this task independently. Do not invent repository details.\n\n"
        f"TASK:\n{task.strip()}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def call_agent(client, role: AgentRole, task: str, model: str,
                     semaphore: asyncio.Semaphore, timeout: float) -> AgentResult:
    import time

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
        text = response.choices[0].message.content or ""
        return AgentResult(role.key, model, "success", output=text,
                           elapsed_seconds=round(time.monotonic() - started, 2))
    except Exception as exc:  # report per-agent failures without hiding other results
        return AgentResult(role.key, model, "failed",
                           error=f"{type(exc).__name__}: {exc}",
                           elapsed_seconds=round(time.monotonic() - started, 2))


async def run_agents(roles: list[AgentRole], task: str, model: str,
                     concurrency: int, timeout: float, token: str) -> list[AgentResult]:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency. Install tools/puter-agent-orchestrator/requirements.txt"
        ) from exc

    client = AsyncOpenAI(
        api_key=token,
        base_url=PUTER_BASE_URL,
        timeout=timeout,
        max_retries=0,
    )
    semaphore = asyncio.Semaphore(concurrency)
    try:
        return await asyncio.gather(*(
            call_agent(client, role, task, model, semaphore, timeout)
            for role in roles
        ))
    finally:
        await client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run independent, read-only DummyDreams agent analyses through Puter."
    )
    parser.add_argument("--task", required=True, help="Task description; do not include secrets.")
    parser.add_argument("--agents", default=",".join(role.key for role in ROLES),
                        help="Comma-separated role keys (default: all seven).")
    parser.add_argument("--model", default=os.getenv("PUTER_MODEL", DEFAULT_MODEL))
    parser.add_argument("--concurrency", type=int, default=3,
                        help="Maximum simultaneous requests (default: 3; max: 7).")
    parser.add_argument("--timeout", type=float, default=90.0,
                        help="Per-request timeout in seconds.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print selected roles/prompts without network access.")
    parser.add_argument("--output", type=Path,
                        help="Optional local JSON report path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.concurrency <= len(ROLES):
        print("Error: --concurrency must be between 1 and 7.", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("Error: --timeout must be greater than zero.", file=sys.stderr)
        return 2
    try:
        roles = select_roles(name.strip() for name in args.agents.split(",") if name.strip())
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps({
            "mode": "dry-run",
            "task": args.task,
            "model": args.model,
            "concurrency": args.concurrency,
            "agents": [{"key": r.key, "title": r.title,
                        "messages": build_messages(r, args.task)} for r in roles],
        }, indent=2, ensure_ascii=False))
        return 0

    token = os.getenv("PUTER_AUTH_TOKEN")
    if not token:
        print("Error: PUTER_AUTH_TOKEN is not set. See README.md; never commit the token.",
              file=sys.stderr)
        return 2

    results = asyncio.run(run_agents(roles, args.task, args.model,
                                     args.concurrency, args.timeout, token))
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": "Puter OpenAI-compatible API",
        "model": args.model,
        "task": args.task,
        "concurrency_limit": args.concurrency,
        "results": [asdict(result) for result in results],
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Report saved to {args.output}", file=sys.stderr)
    return 0 if all(result.status == "success" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
