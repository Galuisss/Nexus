"""Compare native context balances across local Codex rollouts."""

import argparse
import json
from pathlib import Path

import zstandard

from nexus.context_usage import analyze_records


def read(path):
    data = path.read_bytes()
    if path.suffix == ".zst":
        with zstandard.ZstdDecompressor().stream_reader(data) as reader:
            data = reader.read()
    return data


def history(path, paths, end=None):
    data = read(path)[:end]
    records = [json.loads(line) for line in data[: data.rfind(b"\n") + 1].splitlines()]
    if boundary := records[0]["payload"].get("history_base"):
        parent = paths[boundary["thread_id"]]
        records = history(parent, paths, boundary["end_byte_offset"]) + records
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--latest", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = {}
    for directory in ("sessions", "archived_sessions"):
        for path in (args.codex_home / directory).rglob("*.jsonl*"):
            if path.name.endswith((".jsonl", ".jsonl.zst")):
                thread_id = path.name.removesuffix(".zst").removesuffix(".jsonl")[-36:]
                paths[thread_id] = path
    reports = []
    print(
        f"{'会话':36} {'请求':>4} {'首个差额':>9} {'最新差额':>9} {'最小':>9} {'最大':>9} {'漂移':>9}"
    )
    for path in sorted(paths.values(), key=lambda entry: entry.stat().st_mtime, reverse=True)[
        : args.latest
    ]:
        thread_id = path.name.removesuffix(".zst").removesuffix(".jsonl")[-36:]
        result = analyze_records(history(path, paths), thread_id)
        samples = [
            sample for sample in result["samples"] if sample["threadId"] == result["threadId"]
        ]
        if not samples:
            continue
        values = [sample["inputDelta"] for sample in samples]
        report = {
            "threadId": result["threadId"],
            "file": str(path),
            "model": result["model"],
            "requests": len(samples),
            "first": values[0],
            "last": values[-1],
            "minimum": min(values),
            "maximum": max(values),
            "drift": values[-1] - values[0],
            "alignedOutputs": sum(sample["outputDelta"] == 0 for sample in samples),
            "discardedOutputTokens": sum(sample["discardedOutputTokens"] for sample in samples),
            "samples": samples,
            "categories": result["categories"],
        }
        reports.append(report)
        print(
            f"{result['threadId']:36} {len(samples):4} {values[0]:9,} {values[-1]:9,} {min(values):9,} {max(values):9,} {values[-1] - values[0]:+9,}"
        )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
