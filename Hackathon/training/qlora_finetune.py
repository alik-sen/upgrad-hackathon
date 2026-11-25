"""QLoRA fine-tune of an SLM for the Triage Agent (SLM_Comprehensive_Guide §4), corrected.

NOT executed on the dev box (needs a CUDA GPU; see docs/GAPS_AND_ASSUMPTIONS.md). The production triage in this repo is the distilled
TF-IDF+LR classifier (src/unreliable/triage.py, ~3 ms); this script produces the SLM alternative whose adapters can be shadow-tested.

Defects fixed vs the guide's snippet:
  1. `Dataset.from_dict` mixed list and str columns ("output" was a bare string) -> ValueError.  All columns are lists here.
  2. The prompt template already ends with <|im_end|> and the script appended tokenizer.eos_token again -> duplicated terminator; removed.
  3. `dataset_text_field` / `max_seq_length` / `packing` kwargs moved into trl.SFTConfig in current TRL; TrainingArguments no longer accepted.
  4. Single toy example; replaced by the labelled triage set (data/triage_labeled.jsonl, train split) and a held-out eval with the guide's KPIs.
  5. lora_alpha=16 with r=64 gives scaling 0.25 (under-weights adapters); alpha=r is used by default (override with --alpha 16 to reproduce the guide).

Usage:
  python training/qlora_finetune.py --build-data                 # writes training/triage_sft.jsonl (CPU, no deps)
  python training/qlora_finetune.py --train --model unsloth/Qwen2.5-3B-Instruct
  python training/qlora_finetune.py --eval-file preds.jsonl      # schema-compliance / F1 / latency KPI check on model outputs
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYSTEM = "You are the triage agent of an enterprise assistant. Reply with ONLY compact JSON: {\"route\": DIRECT_ANSWER|HYBRID_RAG|TRANSACTION_EXEC|REJECT_OOS}."
PROMPT = "<|im_start|>system\n" + SYSTEM + "<|im_end|>\n<|im_start|>user\n{instruction}\n{input}<|im_end|>\n<|im_start|>assistant\n{output}<|im_end|>"
ROUTES = {"DIRECT_ANSWER", "HYBRID_RAG", "TRANSACTION_EXEC", "REJECT_OOS"}


def build_data(out: Path) -> int:
    rows = [json.loads(l) for l in (ROOT / "data" / "triage_labeled.jsonl").read_text().splitlines()]
    n = 0
    with out.open("w") as f:
        for r in rows:
            if r["split"] != "train":
                continue
            f.write(json.dumps({"instruction": "Classify the user message.", "input": r["text"], "output": json.dumps({"route": r["route"]})}) + "\n")
            n += 1
    return n


def train(model_name: str, rank: int, alpha: int, epochs: int, out_dir: str):
    import torch
    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastLanguageModel

    max_seq = 1024
    model, tok = FastLanguageModel.from_pretrained(model_name=model_name, max_seq_length=max_seq, dtype=None, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(model, r=rank, lora_alpha=alpha, lora_dropout=0, bias="none", use_gradient_checkpointing="unsloth", random_state=3407,
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"])
    rows = [json.loads(l) for l in (ROOT / "training" / "triage_sft.jsonl").read_text().splitlines()]
    ds = Dataset.from_dict({k: [r[k] for r in rows] for k in ("instruction", "input", "output")})
    ds = ds.map(lambda ex: {"text": [PROMPT.format(instruction=i, input=x, output=o) for i, x, o in zip(ex["instruction"], ex["input"], ex["output"])]}, batched=True)
    cfg = SFTConfig(output_dir=out_dir, dataset_text_field="text", max_seq_length=max_seq, packing=False, dataset_num_proc=2, per_device_train_batch_size=2,
                    gradient_accumulation_steps=4, warmup_ratio=0.05, num_train_epochs=epochs, learning_rate=2e-4, fp16=not torch.cuda.is_bf16_supported(),
                    bf16=torch.cuda.is_bf16_supported(), logging_steps=10, optim="adamw_8bit", weight_decay=0.01)
    SFTTrainer(model=model, tokenizer=tok, train_dataset=ds, args=cfg).train()
    model.save_pretrained_merged(f"{out_dir}/merged_16bit", tok, save_method="merged_16bit")


def evaluate(pred_file: Path) -> dict:
    """pred_file: jsonl {text, route(gold), output(model raw text), latency_ms}. KPIs from the guide §5: schema >= 99 %, F1 >= 92 %, p95 TTFT <= 45 ms."""
    rows = [json.loads(l) for l in pred_file.read_text().splitlines()]
    valid, correct, lats = 0, 0, []
    for r in rows:
        try:
            d = json.loads(r["output"]); ok = set(d) == {"route"} and d["route"] in ROUTES
        except Exception:  # noqa: BLE001
            ok, d = False, {}
        valid += ok; correct += ok and d["route"] == r["route"]; lats.append(r.get("latency_ms", 0))
    lats.sort()
    res = {"schema_compliance": valid / len(rows), "accuracy": correct / len(rows), "p95_latency_ms": lats[int(len(lats) * .95)] if lats else None}
    res["pass"] = res["schema_compliance"] >= .99 and res["accuracy"] >= .92 and (res["p95_latency_ms"] or 0) <= 45
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-data", action="store_true"); ap.add_argument("--train", action="store_true")
    ap.add_argument("--model", default="unsloth/Qwen2.5-3B-Instruct"); ap.add_argument("--rank", type=int, default=64); ap.add_argument("--alpha", type=int, default=64)
    ap.add_argument("--epochs", type=int, default=3); ap.add_argument("--out", default="outputs"); ap.add_argument("--eval-file")
    a = ap.parse_args()
    if a.build_data:
        print("wrote", build_data(ROOT / "training" / "triage_sft.jsonl"), "examples")
    if a.train:
        train(a.model, a.rank, a.alpha, a.epochs, a.out)
    if a.eval_file:
        print(evaluate(Path(a.eval_file)))
