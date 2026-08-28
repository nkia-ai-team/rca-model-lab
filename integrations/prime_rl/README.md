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

## Split KT sessions

`configs/prime_rl/rca-online-smoke/` contains standalone configs for the
three-process deployment:

- `trainer.toml` runs only the FSDP2 LoRA trainer on the training H200;
- `inference.toml` runs only vLLM on the inference H200;
- `orchestrator.toml` runs the RCA environment and scorer locally.

Each training group contains eight rollouts and
`env.max_concurrent_agents = 8`, so those eight model investigations execute in
parallel. Raising only `group_size` is insufficient because Prime otherwise
defaults the environment worker pool to one agent and serializes the group.

The orchestrator binds the ZMQ batch transport on local ports 5555 and 5556.
Expose those ports to the trainer with SSH reverse forwards. Expose the vLLM
router and admin engine to the orchestrator with local forwards for ports 8000
and 8100.

Prime's LoRA filesystem broadcast normally requires a shared mount. These KT
containers have isolated filesystems, so run `scripts/relay_prime_lora.py` with
a local copy of `relay.example.yaml`. The relay preserves Prime's
`.sender_ready -> .receiver_ready -> .started -> .finished` protocol and only
publishes `.finished` locally after the complete adapter exists on the vLLM
host. `local_broadcast_dir` and the inference host's
`remote_broadcast_dir` must be the identical absolute path because Prime sends
that path to vLLM's adapter-load endpoint.
