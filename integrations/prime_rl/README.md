# Prime-RL integration

The RCA online-RL stack is pinned to Prime-RL commit
`95734aa1dd3de26afee31e99b7b63b86ad8f4a2e` and its Verifiers submodule commit
`b2e4e8157783b2c0dffc7821044c87f29f1c3ccf`.

`patches/0001-muse-glimmer-sft-lora.patch` adds only the contracts that the upstream
trainer does not currently provide:

- registers Muse-Glimmer's vision and language module boundaries;
- allows the HF implementation to use SDPA;
- preserves Muse-Glimmer's pre-softcap `output_multiplier` in trainer logprobs;
- initializes Prime's sharded LoRA tensors from an existing PEFT SFT adapter;
- keeps the CPU orchestrator independent from trainer-only CUDA/TorchTitan imports;
- keeps the served LoRA name separate from the base tokenizer/config used by
  the renderer and batch packer;
- adapts Prime's vLLM hooks to the current launcher and structured-output API;
- uses W&B's current GraphQL transport instead of the removed `wandb_gql` import;
- adds CPU contract tests for logprob/gradient equivalence and adapter key loading.

Prepare a checkout with:

```bash
scripts/prepare_prime_rl.sh /absolute/path/to/prime-rl
```

The script refuses a dirty or differently pinned checkout. It is idempotent when
the patch is already applied.

Prime keeps PyTorch in its GPU extra even though the orchestrator uses CPU
tensors while packing batches. Therefore the local orchestrator environment
must include a CPU PyTorch wheel and `prometheus-client` in addition to the
patched Prime package. It does not need TorchTitan, vLLM, or the CUDA trainer
stack.

The trainer environment must apply `trainer-overrides.txt` after installing
Prime. Prime pins Transformers 5.6.2, which cannot load the `muse_glimmer`
architecture; the override uses the same Muse-capable Transformers 5.15.1 as
the SFT environment and also declares Prime's missing metrics dependency:

```bash
uv pip install --python /absolute/path/to/trainer/python \
  -r integrations/prime_rl/trainer-overrides.txt
```

All stages are bound to the pinned
`RedHatAI/Muse-Glimmer-30B-FP8-block` source revision. Transformers dequantizes
that compressed checkpoint to BF16 for SFT, while vLLM serves the original FP8
weights and a separate LoRA adapter. Prime's DCP loader cannot reconstruct the
checkpoint's block scales directly, so its trainer reads a disposable dense
BF16 loading cache derived from that same FP8 source:

```bash
CUDA_VISIBLE_DEVICES=0 scripts/materialize_prime_training_cache.py \
  configs/sft/muse-glimmer-30b-teacher-v4-high.yaml \
  --output /home/work/models/muse-glimmer-30b-fp8-block-bf16-train-cache
```

`training_cache_manifest.json` binds the cache to the source revision and file
hashes. The materializer also preserves the custom tokenizer and chat template
byte-for-byte. This cache is neither a new base model nor a merged adapter and
may be deleted and regenerated; do not upload it as the canonical checkpoint.

## Split KT sessions

`configs/prime_rl/rca-online-smoke/` contains standalone configs for the
four-process deployment:

- `trainer.toml` runs only the FSDP2 LoRA trainer on the training H200;
- `inference.toml` runs only vLLM on the inference H200;
- `env-server.toml` runs the production RCA harness, scenario lease and scorer
  locally;
- `orchestrator.toml` schedules rollout groups, packs trajectories and sends
  training batches locally.

Each training group contains eight rollouts and
`env.max_concurrent_agents = 8`, so those eight model investigations execute in
parallel. Raising only `group_size` is insufficient because Prime otherwise
defaults the environment worker pool to one agent and serializes the group.

The local orchestrator owns coordination, not GPU computation. It asks the
environment server for a training case, schedules eight Student investigations,
routes every model request to the inference session, collects the complete
episodes, asks the typed scorer for rewards, computes group-relative advantages,
and sends the packed token batch to the remote trainer. The inference session
only generates tokens; the training session only computes gradients and updates
the LoRA adapter.

Online training is progressive. `env-server.toml` is the short exploration
bootstrap stage. It rewards diagnosis quality plus bounded credit for distinct
environment evidence that is actually cited in the final answer. It does not
reward tool names, raw RPC success, or fewer turns. This avoids an all-zero GRPO
group before the SFT policy can reach a correct root while preventing repeated
tool calls from accumulating unbounded reward. After the bootstrap checkpoint,
restart the environment server with `env-server-diagnosis.toml`; that stage uses
only root identity, proof validity, status, and strict correctness. A model is
promoted only by the diagnosis-stage sealed evaluation, never by the bootstrap
score.

The transition is a checkpoint resume, not a new adapter initialization. Stop
the bootstrap processes only after both local and remote `step_3` checkpoints
exist. Then start the diagnosis environment with `env-server-diagnosis.toml`,
the local scheduler with `orchestrator-diagnosis.toml`, and the remote trainer
with `trainer-diagnosis.toml`. Both diagnosis configs declare `resume.step = 3`
and `max_steps = 6`, so Prime restores the same policy and optimizer state and
trains steps 4 through 6 under the stricter reward. Do not point the diagnosis
trainer directly at the original SFT adapter; that would discard the bootstrap
updates.

The orchestrator binds the ZMQ batch transport on local ports 5555 and 5556.
Expose those ports to the trainer with SSH reverse forwards. Expose the vLLM
router and admin engine to the orchestrator with local forwards for ports 8000
and 8100.

Prime's LoRA filesystem broadcast normally requires a shared mount. These KT
containers have isolated filesystems, so run `scripts/relay_prime_lora.py` with
a local copy of `relay.example.yaml`. The relay preserves Prime's
`.sender_ready -> .receiver_ready -> .started -> .finished` protocol and only
publishes `.finished` locally after the complete adapter exists on the vLLM
host. It uses legacy SCP because KT's SFTP subsystem corrupts the protocol on
large adapter transfers. `local_broadcast_dir` and the inference host's
`remote_broadcast_dir` must be the identical absolute path because Prime sends
that path to vLLM's adapter-load endpoint.

The current inference image has an immutable FlashInfer version mismatch, so
`inference.toml` selects FlashAttention and disables FlashInfer autotuning. It
does not reduce the KV-cache allocation or the eight-request concurrency target.
