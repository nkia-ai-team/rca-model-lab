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
- adds CPU contract tests for logprob/gradient equivalence and adapter key loading.

Prepare a checkout with:

```bash
scripts/prepare_prime_rl.sh /absolute/path/to/prime-rl
```

The script refuses a dirty or differently pinned checkout. It is idempotent when
the patch is already applied.
