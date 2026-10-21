"""Paired real-model context-size ablation over frozen synthetic shop observations."""
import argparse
import asyncio
import hashlib
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from crashcustomer.config import DATA_DIR, OLLAMA_MODEL
from crashcustomer.policy import OllamaPolicy, available_actions
from crashcustomer.store import Store


def fixtures():
    store = Store(DATA_DIR)
    baseline = json.loads(Path("docs/evaluation-workflow-20261021.json").read_text())
    row = next(r for r in baseline["results"] if r["variant"] == "fixed")
    campaign = store.get_campaign(row["campaign_id"])
    run = store.get_run(campaign["run_ids"][0])
    assert "127.0.0.1:8720/fixed" in run["target_url"]
    choices = [s for s in run["observations"] if s["step"] < len(run["actions"])]
    indexes = list(dict.fromkeys([0, len(choices) // 2, len(choices) - 1]))
    return [{"source_run_id": run["id"], "goal": run["goal"], "snapshot": choices[i],
             "actions": run["actions"][:choices[i]["step"]]} for i in indexes]


def summarize(rows):
    result = {}
    for mode in ("full", "compact"):
        group = [r for r in rows if r["mode"] == mode]
        good = [r for r in group if r["valid"]]
        result[mode] = {"calls": len(group), "valid_decisions": len(good), "errors": len(group)-len(good),
                        "mean_input_tokens": round(statistics.mean(r["usage"]["input_tokens"] for r in good), 2) if good else None,
                        "mean_output_tokens": round(statistics.mean(r["usage"]["output_tokens"] for r in good), 2) if good else None,
                        "mean_ms": round(statistics.mean(r["ms"] for r in group), 2),
                        "median_ms": round(statistics.median(r["ms"] for r in group), 2),
                        "min_ms": min(r["ms"] for r in group), "max_ms": max(r["ms"] for r in group)}
    full, compact = result["full"], result["compact"]
    result["input_token_reduction_percent"] = round(100 * (1 - compact["mean_input_tokens"] / full["mean_input_tokens"]), 2) if full["mean_input_tokens"] and compact["mean_input_tokens"] else None
    pairs = {(r["repeat"], r["fixture"]): {} for r in rows}
    for r in rows: pairs[r["repeat"], r["fixture"]][r["mode"]] = r
    result["same_kind_and_target_pairs"] = sum(p["full"].get("action", {}).get("kind") == p["compact"].get("action", {}).get("kind")
                                              and p["full"].get("action", {}).get("target") == p["compact"].get("action", {}).get("target")
                                              and p["full"]["valid"] and p["compact"]["valid"] for p in pairs.values())
    result["pairs"] = len(pairs)
    return result


async def benchmark(repeats, output):
    frozen = fixtures()
    fixture_json = json.dumps(frozen, ensure_ascii=False, sort_keys=True)
    fixture_path = output.with_name("context-fixtures-20261021.json")
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(fixture_json + "\n")
    # Same schema, action guard, model, temperature, goal and observations in both conditions.
    policies = {"full": OllamaPolicy(compact_context=False), "compact": OllamaPolicy(compact_context=True)}
    await policies["compact"].decide(frozen[0]["goal"], frozen[0]["snapshot"], frozen[0]["actions"])
    rows = []
    for repeat in range(repeats):
        for index, fixture in enumerate(frozen):
            order = ("full", "compact") if (repeat + index) % 2 == 0 else ("compact", "full")
            for mode in order:
                started = time.perf_counter()
                row = {"repeat": repeat+1, "fixture": index, "step": fixture["snapshot"]["step"], "mode": mode, "valid": False}
                try:
                    action, usage = await policies[mode].decide(fixture["goal"], fixture["snapshot"], fixture["actions"])
                    row.update(action=action.model_dump(), usage=usage,
                               valid=any(c["kind"] == action.kind and c["target"] == action.target for c in available_actions(fixture["snapshot"])))
                except Exception as error:
                    row["error_type"] = type(error).__name__
                row["ms"] = round((time.perf_counter() - started) * 1000, 2)
                rows.append(row)
                output.write_text(json.dumps({"complete": False, "results": rows}, ensure_ascii=False, indent=2))
                print(json.dumps({k: row[k] for k in ("repeat", "fixture", "mode", "valid", "ms")}), flush=True)
    result = {"complete": True, "model": OLLAMA_MODEL, "measured_at": datetime.now(timezone.utc).isoformat(),
              "repeats": repeats, "fixtures": len(frozen), "fixture_file": fixture_path.name,
              "fixture_sha256": hashlib.sha256(fixture_json.encode()).hexdigest(),
              "control": "Full observed DOM text+ARIA+history vs text<=1500/no ARIA/last5 actions. All other action guards identical.",
              "order": "alternating paired order; one excluded compact warmup call", "summary": summarize(rows), "results": rows,
              "scope": "Decision-level evaluation on three frozen states of a synthetic shop. Not a browser completion or real-world recall benchmark. Local inference: tokens/time measured, monetary cost not estimated."}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("docs/benchmark-context-20261021.json"))
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10: parser.error("repeats must be 1..10")
    asyncio.run(benchmark(args.repeats, args.output))
