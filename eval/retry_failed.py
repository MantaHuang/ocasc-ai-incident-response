"""Targeted retry of gateway-failed cells, and merge for grading.

run.py treats an errored row as complete, so a resume never retries it, and
grade.py rejects duplicate keys, so a retry cannot be appended to the same
file. This script retries only rows whose error is a transient gateway fault,
writes them to a separate file with its own ledger, lock and budget, and a
--merge step builds a single file for grade.py.

Safety properties copied from run.py: preview by default; a durable ledger
entry is written before every request; an unreconciled attempt stops the run
rather than replaying a call the server may have charged; a hard request cap;
prompt hashes are checked against the frozen task file before sending. It
refuses to start while run.py holds its lock.

    python eval/retry_failed.py                 # preview
    python eval/retry_failed.py --execute       # send
    python eval/retry_failed.py --merge         # build results.merged.jsonl, no network
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run  # noqa: E402  (reuse the frozen runner's call path verbatim)

RETRYABLE = ("timeout", "network_error", "http_408", "http_429", "http_500",
             "http_502", "http_503", "http_504", "http_524")


def load_tasks(path):
    return {t["id"]: t for t in json.loads(path.read_text(encoding="utf-8"))["tasks"]}


def failed_rows(results, tasks):
    out = {}
    for row in run.read_jsonl(results):
        k = run.key(row)
        if k in out:
            raise ValueError("Duplicate key in primary results; reconcile first")
        if row["prompt_sha256"] != run.digest(tasks[row["task_id"]][f"prompt_{row['variant']}"]):
            raise ValueError("Primary result does not match frozen tasks")
        if row.get("error") in RETRYABLE:
            out[k] = row
    return out


def acquire(lock):
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError(f"Lock exists: {lock}; check for an active/interrupted process")
    os.close(fd)


def execute(args, tasks):
    primary_lock = args.results.with_suffix(".lock")
    if primary_lock.exists():
        raise ValueError("run.py lock present; wait for the primary run to finish")
    ledger = args.output.with_suffix(".attempts.jsonl")
    lock = args.output.with_suffix(".lock")

    failed = failed_rows(args.results, tasks)
    retried = {}
    for row in run.read_jsonl(args.output):
        k = run.key(row)
        if k in retried:
            raise ValueError("Duplicate key in retry output")
        if k not in failed:
            raise ValueError("Retry output holds a cell that is no longer a failure in primary")
        retried[k] = row

    attempts = run.read_jsonl(ledger)
    starts = [a for a in attempts if a["event"] == "start"]
    finishes = {a["attempt_id"] for a in attempts if a["event"] == "finish"}
    if any(a["attempt_id"] not in finishes or run.key(a) not in retried for a in starts):
        raise ValueError("Unreconciled retry attempt: server may have charged it; no automatic replay")

    pending = [k for k in sorted(failed) if k not in retried]
    used = len(starts)
    print(f"Primary failures={len(failed)}; already retried={len(retried)}; pending={len(pending)}; "
          f"ledger requests so far={used}; cap={args.max_requests}")
    print("By model:", dict(Counter(k[2] for k in pending)))
    if not args.execute:
        print("Preview only: no credential read, no network call.")
        return
    if used + len(pending) > args.max_requests:
        raise ValueError("Pending retries exceed request cap")

    api_key = run.load_config(args.env_file)
    acquire(lock)
    try:
        session = requests.Session()
        for k in pending:
            task_id, variant, model, rnd = k
            task = tasks[task_id]
            prompt = task[f"prompt_{variant}"]
            row = {"task_id": task_id, "variant": variant, "model": model, "round": rnd,
                   "ts": run.stamp(), "prompt_sha256": run.digest(prompt),
                   "response_text": "", "usage": {"input_tokens": 0, "output_tokens": 0},
                   "retries": 0, "http_status": None, "error": None,
                   "gateway": failed[k]["gateway"], "parameters": run.PARAMS.copy(),
                   "data_source": "live_gateway", "retry_of_ts": failed[k]["ts"]}
            for retry in range(args.max_retries + 1):
                if used >= args.max_requests:
                    break
                if retry:
                    time.sleep(args.retry_delay)
                used += 1
                attempt = {"task_id": task_id, "variant": variant, "model": model, "round": rnd,
                           "attempt_id": used, "event": "start", "ts": run.stamp()}
                run.append(ledger, attempt)  # reserve budget durably before sending
                status, error, text, usage, reason = run.call(
                    session, row["gateway"], api_key, dict(run.MODELS)[model],
                    run.payload(model, prompt), args.timeout)
                run.append(ledger, {**attempt, "event": "finish", "ts": run.stamp(),
                                    "http_status": status, "error": error, "usage": usage})
                row.update(response_text=text, usage=usage or {"input_tokens": 0, "output_tokens": 0},
                           retries=retry, http_status=status, error=error, stop_reason=reason,
                           truncated=reason in ("length", "max_tokens"))
                if error not in RETRYABLE:
                    break
            if used >= args.max_requests and row["http_status"] is None:
                break  # cap reached before this cell was ever sent; leave it pending
            run.append(args.output, row)
            print(f"{task_id} {variant} {model} round={rnd}: {row['error'] or 'ok'}", flush=True)
    finally:
        lock.unlink(missing_ok=True)

    rows = run.read_jsonl(args.output)
    ok = sum(r["error"] is None for r in rows)
    print(f"Retry output rows={len(rows)}; succeeded={ok}; still failed={len(rows) - ok}; "
          f"ledger requests={used}")


def merge(args, tasks):
    primary = run.read_jsonl(args.results)
    retries = {run.key(r): r for r in run.read_jsonl(args.output)}
    failed = failed_rows(args.results, tasks)
    if set(retries) - set(failed):
        raise ValueError("Retry output holds cells that are not primary failures")
    merged, replaced, kept_failed = [], 0, 0
    for row in primary:
        k = run.key(row)
        r = retries.get(k)
        if r is not None and r.get("error") is None:
            merged.append({**r, "replaced_failed_ts": row["ts"]})
            replaced += 1
        else:
            merged.append(row)
            kept_failed += row.get("error") is not None
    out = args.results.with_name("results.merged.jsonl")
    out.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in merged), encoding="utf-8")
    print(f"Wrote {out.name}: rows={len(merged)}; failures replaced by successful retry={replaced}; "
          f"rows still failed={kept_failed}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", type=Path, default=run.ROOT / "eval/results.jsonl")
    p.add_argument("--output", type=Path, default=run.ROOT / "eval/results.retry.jsonl")
    p.add_argument("--tasks-file", type=Path, default=run.ROOT / "eval/tasks.json")
    p.add_argument("--env-file", type=Path, default=run.ROOT / "tools/opendev.env")
    p.add_argument("--max-requests", type=int, default=150)
    p.add_argument("--max-retries", type=int, choices=[0, 1, 2], default=2)
    p.add_argument("--retry-delay", type=float, default=5)
    p.add_argument("--timeout", type=float, default=125)
    p.add_argument("--execute", action="store_true", help="Actually send requests")
    p.add_argument("--merge", action="store_true", help="Build results.merged.jsonl; no network")
    a = p.parse_args()
    tasks = load_tasks(a.tasks_file)
    try:
        merge(a, tasks) if a.merge else execute(a, tasks)
    except ValueError as e:
        print(f"Stopped: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
