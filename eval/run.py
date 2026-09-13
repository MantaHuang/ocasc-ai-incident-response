"""Frozen-task runner. Preview by default; persistent HTTP budget includes retries."""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
TEMPERATURE, MAX_TOKENS, SYSTEM = 0.0, 1024, None
MODELS = [("claude-opus-5", "messages"), ("claude-sonnet-5", "messages"),
          ("claude-fable-5-1", "messages"), ("gpt-6-astra", "chat")]
PARAMS = {"max_tokens": MAX_TOKENS, "system": SYSTEM, "temperature": "omitted_gateway_rejects"}


class MockResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class MockSession:
    """Offline fixture for T2 transport smoke only; not a model capability test."""
    def post(self, url, **kwargs):
        if url.endswith("/v1/messages"):
            return MockResponse({"content": [{"type": "text", "text": "6"}],
                                 "stop_reason": "end_turn",
                                 "usage": {"input_tokens": 0, "output_tokens": 0}})
        return MockResponse({"choices": [{"message": {"content": "6"}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 0, "completion_tokens": 0}})


def stamp():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def key(row):
    return tuple(row[x] for x in ("task_id", "variant", "model", "round"))


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError(f"Invalid JSONL line {n}; preserve and reconcile before resuming") from None
        rows.append(row)
    return rows


def append(path, row):
    needs_newline = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            needs_newline = existing.read(1) != b"\n"
    with path.open("a", encoding="utf-8", newline="\n") as f:
        if needs_newline:
            f.write("\n")
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def load_config(path):
    config = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                config[k.strip()] = v.strip().strip("\"'")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or config.get("ANTHROPIC_API_KEY")
    if not api_key or api_key == "YOUR_API_KEY":
        raise ValueError("API key missing; configure locally, never in command arguments")
    return api_key


def payload(model, prompt):
    # Deliberately no system message, tools, history, or autonomous agent loop.
    # temperature 被网关拒绝（"deprecated for this model"），且发送它会使请求落入
    # 另一条带 agent 脚手架的路径：input 从约 300 膨胀到约 5000，模型改以工具调用作答。
    # 2026-09-13 实测确认，故移除。参数变更记入报告附录。
    return {"model": model, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}]}


def parse_response(data, api):
    usage = data.get("usage") or {}
    if api == "messages":
        blocks = data["content"]
        text = "".join(b["text"] for b in blocks if b.get("type") == "text")
        reason = data.get("stop_reason")
        inp, out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    else:
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        # A refusal returned in the dedicated field is data, never a retry trigger.
        text = content if content else (message.get("refusal") or "")
        reason = choice.get("finish_reason")
        inp, out = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    if not isinstance(text, str):
        raise ValueError("Invalid text response")
    return text, {"input_tokens": int(inp), "output_tokens": int(out)}, reason


def call(session, base, api_key, api, body, timeout):
    endpoint = "/v1/messages" if api == "messages" else "/v1/chat/completions"
    headers = {"content-type": "application/json"}
    if api == "messages":
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    else:
        headers["authorization"] = "Bearer " + api_key
    try:
        response = session.post(base + endpoint, json=body, headers=headers,
                                timeout=(15, timeout), allow_redirects=False)
    except requests.Timeout:
        return None, "timeout", "", {}, None
    except requests.RequestException:
        # Do not print exception bodies/URLs/headers, which may contain credentials.
        return None, "network_error", "", {}, None
    status = response.status_code
    if not 200 <= status < 300:
        return status, f"http_{status}", "", {}, None
    try:
        text, usage, reason = parse_response(response.json(), api)
    except (ValueError, KeyError, TypeError, IndexError):
        return status, "invalid_response", "", {}, None
    if api_key in text:
        text = text.replace(api_key, "[REDACTED_API_KEY]")
        return status, "credential_echo_redacted", text, usage, reason
    return status, None, text, usage, reason


def make_plan(tasks, task_ids, variants, models, rounds):
    by_id = {t["id"]: t for t in tasks["tasks"]}
    if len(by_id) != len(tasks["tasks"]):
        raise ValueError("Duplicate task IDs")
    if set(task_ids) - by_id.keys() or set(models) - dict(MODELS).keys():
        raise ValueError("Unknown task/model selection")
    return [(by_id[t], v, m, r) for t in task_ids for v in variants
            for m in models for r in range(1, rounds + 1)]


def execute(plan, output, manifest, api_key, max_requests, max_retries=2,
            retry_delay=5, timeout=125, session=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError("Runner lock exists; check for an active/interrupted process") from None
    os.close(fd)
    try:
        return _execute(plan, output, manifest, api_key, max_requests, max_retries,
                        retry_delay, timeout, session or requests.Session())
    finally:
        lock.unlink()


def _execute(plan, output, manifest, api_key, max_requests, max_retries,
             retry_delay, timeout, session):
    mf = output.with_suffix(".manifest.json")
    ledger = output.with_suffix(".attempts.jsonl")
    if mf.exists():
        if json.loads(mf.read_text(encoding="utf-8")) != manifest:
            raise ValueError("Manifest changed; do not mix parameters, budget, or task versions")
    else:
        if output.exists() or ledger.exists():
            raise ValueError("Existing data lacks manifest; reconcile before running")
        mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = read_jsonl(output)
    attempts = read_jsonl(ledger)
    expected = {(t["id"], v, m, r): digest(t[f"prompt_{v}"]) for t, v, m, r in plan}
    done = {}
    for row in rows:
        k = key(row)
        if k not in expected or row["prompt_sha256"] != expected[k] or k in done:
            raise ValueError("Duplicate, out-of-plan, or mismatched result")
        done[k] = row
    starts = [a for a in attempts if a["event"] == "start"]
    finishes = {a["attempt_id"] for a in attempts if a["event"] == "finish"}
    if any(a["attempt_id"] not in finishes or key(a) not in done for a in starts):
        raise ValueError("Unreconciled attempt: server may have charged it; no automatic replay")
    pending = [p for p in plan if (p[0]["id"], *p[1:]) not in done]
    if len(starts) + len(pending) > max_requests:
        raise ValueError("Pending calls exceed persistent HTTP budget")
    used = len(starts)
    for task, variant, model, rnd in pending:
        if used >= max_requests:
            break
        row = {"task_id": task["id"], "variant": variant, "model": model, "round": rnd,
               "ts": stamp(), "prompt_sha256": digest(task[f"prompt_{variant}"]),
               "response_text": "", "usage": {"input_tokens": 0, "output_tokens": 0},
               "retries": 0, "http_status": None, "error": None,
               "gateway": manifest["gateway"], "parameters": PARAMS.copy(),
               "data_source": manifest.get("data_source", "live_gateway")}
        for retry in range(max_retries + 1):
            if used >= max_requests:
                break
            if retry:
                time.sleep(retry_delay)
            used += 1
            attempt = {k: row[k] for k in ("task_id", "variant", "model", "round")}
            attempt.update(attempt_id=used, event="start", ts=stamp())
            append(ledger, attempt)  # Durably reserve budget before sending.
            status, error, text, usage, reason = call(session, manifest["gateway"], api_key,
                dict(MODELS)[model], payload(model, task[f"prompt_{variant}"]), timeout)
            append(ledger, {**attempt, "event": "finish", "ts": stamp(),
                            "http_status": status, "error": error, "usage": usage})
            row.update(response_text=text, usage=usage or {"input_tokens": 0, "output_tokens": 0},
                       retries=retry, http_status=status, error=error, stop_reason=reason,
                       truncated=reason in ("length", "max_tokens"))
            if error not in ("timeout", "network_error", "http_408", "http_429", "http_500",
                              "http_502", "http_503", "http_504", "http_524"):
                break
        append(output, row)
        rows.append(row)
        print(f"{task['id']} {variant} {model} round={rnd}: {row['error'] or 'ok'}", flush=True)
    return rows, used


def report(rows, requests_used, expected, input_price, output_price):
    success = sum(r["error"] is None for r in rows)
    print(f"Total={len(rows)}/{expected}; success={success}; failed={len(rows)-success}; HTTP requests={requests_used}")
    total = 0
    for model in sorted({r["model"] for r in rows}):
        units = Counter()
        for row in rows:
            if row["model"] == model:
                units.update(row["usage"])
        cost = (units['input_tokens'] * input_price + units['output_tokens'] * output_price) / 1_000_000
        total += cost
        print(f"{model}: input={units['input_tokens']} output={units['output_tokens']} estimated_USD={cost:.6f}")
    print(f"Estimated total USD={total:.6f}; ASSUMPTION rates per million: input={input_price}, output={output_price}.")
    print("Not a verified tariff/invoice; retries with unknown usage and cache pricing are excluded.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tasks-file", type=Path, default=ROOT / "eval/tasks.json")
    p.add_argument("--tasks", nargs="+", default=["T2"])
    p.add_argument("--variants", nargs="+", choices=["A", "B", "C"], default=["A", "B", "C"])
    p.add_argument("--models", nargs="+", choices=list(dict(MODELS)), default=["claude-sonnet-5", "gpt-6-astra"])
    p.add_argument("--rounds", type=int, choices=[1, 2, 3], default=1)
    p.add_argument("--output", type=Path, default=ROOT / "eval/results.jsonl")
    p.add_argument("--env-file", type=Path, default=ROOT / "tools/opendev.env")
    p.add_argument("--max-requests", type=int, default=6)
    p.add_argument("--max-retries", type=int, choices=[0, 1, 2], default=2)
    p.add_argument("--retry-delay", type=float, default=5)
    p.add_argument("--timeout", type=float, default=125)
    p.add_argument("--input-price", type=float, default=100, help="Assumed USD/million, not verified gateway tariff")
    p.add_argument("--output-price", type=float, default=100)
    p.add_argument("--execute", action="store_true", help="Actually send API requests; default is preview")
    p.add_argument("--mock", action="store_true", help="Offline T2 fixtures only; never reads keys or contacts models")
    a = p.parse_args()
    if a.mock and a.execute:
        p.error("--mock and --execute are mutually exclusive")
    if a.mock and a.tasks != ["T2"]:
        p.error("Offline smoke fixture supports T2 only")
    if a.max_requests < 1 or a.retry_delay < 5 or a.timeout <= 0 or min(a.input_price, a.output_price) < 0:
        p.error("Invalid budget, timeout, retry delay, or rate")
    if any(len(x) != len(set(x)) for x in (a.tasks, a.variants, a.models)):
        p.error("Duplicate selection")
    tasks = json.loads(a.tasks_file.read_text(encoding="utf-8-sig"))
    plan = make_plan(tasks, a.tasks, a.variants, a.models, a.rounds)
    print(f"Plan={len(plan)} units; max HTTP requests including retries={a.max_requests}; parameters={PARAMS}")
    if len(plan) > a.max_requests:
        p.error("Plan exceeds request budget; no calls sent")
    manifest = {"schema_version": 1, "gateway": "https://api.openai-next.com", "parameters": PARAMS,
                "data_source": "offline_fixture" if a.mock else "live_gateway",
                "tasks_sha256": hashlib.sha256(a.tasks_file.read_bytes()).hexdigest(),
                "selection": {"tasks": a.tasks, "variants": a.variants, "models": a.models, "rounds": a.rounds},
                "max_requests": a.max_requests, "max_retries": a.max_retries,
                "retry_delay": a.retry_delay, "timeout": a.timeout,
                "cost_assumptions": {"input_per_million": a.input_price, "output_per_million": a.output_price}}
    if not a.execute and not a.mock:
        print("Preview only: no credential read, no network call.")
        return
    rows, used = execute(plan, a.output, manifest, "MOCK_NOT_A_SECRET" if a.mock else load_config(a.env_file), a.max_requests,
                         a.max_retries, a.retry_delay, a.timeout, MockSession() if a.mock else None)
    if a.mock:
        print(f"OFFLINE FIXTURES: {len(rows)}/{len(plan)} records, {used} simulated requests; real HTTP=0; cost USD=0.")
    else:
        report(rows, used, len(plan), a.input_price, a.output_price)
    if len(rows) != len(plan) or any(r["error"] for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        # Our validation errors contain no secret-bearing request bodies.
        print(f"Stopped: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
