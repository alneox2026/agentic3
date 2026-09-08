#!/usr/bin/env python3
"""Run a bounded concurrent load test against a gateway buffered chat endpoint."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from getpass import getpass
import json
import os
import statistics
import sys
import time
from typing import Any
from urllib import error, request


DEFAULT_PROMPT = "What does API stand for? Reply in exactly one short sentence."


@dataclass(frozen=True)
class RequestResult:
    index: int
    ok: bool
    status_code: int | None
    latency_ms: int
    thread_id: str
    session_id: str
    turn_id: str
    reply_text_length: int
    error_code: str
    exception: str


def percentile(values: list[int], percentile_rank: float) -> int:
    if not values:
        return 0
    sorted_values = sorted(values)
    index = int(round((percentile_rank / 100) * (len(sorted_values) - 1)))
    return sorted_values[index]


def read_token(env_name: str, prompt_if_missing: bool) -> str:
    token = os.getenv(env_name, "").strip()
    if token:
        return token
    if prompt_if_missing:
        return getpass(f"Paste Firebase ID token for {env_name}: ").strip()
    raise SystemExit(
        f"Missing Firebase ID token. Set {env_name} or pass --prompt-token."
    )


def post_chat(
    *,
    index: int,
    url: str,
    token: str,
    prompt: str,
    timeout_seconds: float,
) -> RequestResult:
    started = time.perf_counter()
    payload = {
        "message": prompt,
        "thread_id": "",
        "session_id": "",
        "metadata": {"load_test": True, "load_test_index": index},
    }
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    status_code: int | None = None
    body: dict[str, Any] = {}
    exception = ""
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            status_code = response.status
            response_body = response.read().decode("utf-8")
            body = json.loads(response_body) if response_body else {}
    except error.HTTPError as exc:
        status_code = exc.code
        raw_error = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw_error) if raw_error else {}
        except json.JSONDecodeError:
            body = {"error": {"code": "non_json_error_body"}}
    except Exception as exc:  # noqa: BLE001 - load-test output must capture all failures.
        exception = f"{type(exc).__name__}: {exc}"

    latency_ms = int((time.perf_counter() - started) * 1000)
    error_body = body.get("error") if isinstance(body.get("error"), dict) else {}
    reply_text = str(body.get("reply_text") or "")
    ok = bool(body.get("ok") is True and status_code == 200 and reply_text.strip())
    error_code = str(error_body.get("code") or "")
    if body.get("ok") is True and status_code == 200 and not reply_text.strip():
        error_code = "empty_reply_text"
    return RequestResult(
        index=index,
        ok=ok,
        status_code=status_code,
        latency_ms=latency_ms,
        thread_id=str(body.get("thread_id") or ""),
        session_id=str(body.get("session_id") or ""),
        turn_id=str(body.get("turn_id") or ""),
        reply_text_length=len(reply_text),
        error_code=error_code,
        exception=exception,
    )


async def run_load_test(args: argparse.Namespace) -> tuple[list[RequestResult], int]:
    token = read_token(args.token_env, args.prompt_token)
    url = f"{args.gateway_url.rstrip('/')}/v1/agents/{args.agent_id}/chat"
    semaphore = asyncio.Semaphore(args.concurrency)

    async def run_one(index: int) -> RequestResult:
        async with semaphore:
            if args.ramp_delay_ms:
                await asyncio.sleep((args.ramp_delay_ms / 1000) * index)
            return await asyncio.to_thread(
                post_chat,
                index=index,
                url=url,
                token=token,
                prompt=args.prompt,
                timeout_seconds=args.timeout_seconds,
            )

    started = time.perf_counter()
    tasks = [asyncio.create_task(run_one(index)) for index in range(args.requests)]
    results = await asyncio.gather(*tasks)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return results, elapsed_ms


def print_summary(results: list[RequestResult], elapsed_ms: int) -> dict[str, Any]:
    latencies = [result.latency_ms for result in results]
    failures = [result for result in results if not result.ok]
    status_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    for result in results:
        status_counts[str(result.status_code)] = status_counts.get(str(result.status_code), 0) + 1
        if result.error_code:
            error_counts[result.error_code] = error_counts.get(result.error_code, 0) + 1
        if result.exception:
            error_counts[result.exception] = error_counts.get(result.exception, 0) + 1

    summary = {
        "requests": len(results),
        "successes": len(results) - len(failures),
        "failures": len(failures),
        "elapsed_ms": elapsed_ms,
        "throughput_rps": round(len(results) / max(elapsed_ms / 1000, 0.001), 3),
        "latency_ms": {
            "min": min(latencies) if latencies else 0,
            "mean": round(statistics.mean(latencies), 2) if latencies else 0,
            "p50": percentile(latencies, 50),
            "p90": percentile(latencies, 90),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies) if latencies else 0,
        },
        "status_counts": status_counts,
        "error_counts": error_counts,
        "sample_thread_ids": [result.thread_id for result in results if result.thread_id][:10],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--agent-id", default="antigravity_agent")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--token-env", default="FIREBASE_ID_TOKEN")
    parser.add_argument("--prompt-token", action="store_true")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--ramp-delay-ms", type=int, default=0)
    parser.add_argument("--output-jsonl", default="load-results.jsonl")
    parser.add_argument("--summary-json", default="load-summary.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.requests <= 0:
        raise SystemExit("--requests must be positive.")
    if args.concurrency <= 0:
        raise SystemExit("--concurrency must be positive.")
    if args.concurrency > args.requests:
        raise SystemExit("--concurrency cannot exceed --requests.")

    results, elapsed_ms = asyncio.run(run_load_test(args))
    with open(args.output_jsonl, "w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(asdict(result), sort_keys=True) + "\n")
    summary = print_summary(results, elapsed_ms)
    with open(args.summary_json, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0 if summary["failures"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
