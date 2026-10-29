"""Fused INT4 (AutoRound GPTQ, group 128, packed zeros, +1 zero offset) -> BF16 dequantization for stacked experts.

Same arithmetic as flash_train.PackedLoRA.dequant / grouped_experts._dequant_stacked: w and zero are exact small
integers, the scale is rounded to the target dtype first, and (w - zero) * scale is rounded once to the target dtype.
One kernel reads the packed weights once and writes the output once (the torch version materializes several int32
and bf16 temporaries)."""
import torch
import triton
import triton.language as tl


@triton.jit
def _dequant_kernel(q_ptr, z_ptr, s_ptr, out_ptr, R, N, G, NZ, OUT_BF16: tl.constexpr,
                    BLOCK_R: tl.constexpr, BLOCK_N: tl.constexpr):
    e = tl.program_id(0)
    rows = tl.program_id(1) * BLOCK_R + tl.arange(0, BLOCK_R)
    cols = tl.program_id(2) * BLOCK_N + tl.arange(0, BLOCK_N)
    mask = (rows[:, None] < R) & (cols[None, :] < N)
    q = tl.load(q_ptr + e * R * N + rows[:, None] * N + cols[None, :], mask=mask, other=0)
    g = rows // 16                                   # 8 unpacked rows per packed row, 128 rows per group
    zq = tl.load(z_ptr + e * G * NZ + g[:, None] * NZ + cols[None, :] // 8, mask=mask, other=0)
    zero = ((zq >> ((cols[None, :] % 8) * 4)) & 15) + 1
    s = tl.load(s_ptr + e * G * N + g[:, None] * N + cols[None, :], mask=mask, other=0.)
    if OUT_BF16:
        s = s.to(tl.bfloat16).to(tl.float32)
    else:
        s = s.to(tl.float16).to(tl.float32)
    out_base = out_ptr + e * R * 8 * N
    for k in tl.static_range(8):
        w = ((q >> (4 * k)) & 15) - zero
        val = w.to(tl.float32) * s
        if OUT_BF16:
            val = val.to(tl.bfloat16)
        else:
            val = val.to(tl.float16)
        tl.store(out_base + (rows[:, None] * 8 + k) * N + cols[None, :], val, mask=mask)


def dequant_stacked(q, z, s, dtype=torch.bfloat16):
    """q [E, R, N] int32, z [E, G, N/8] int32, s [E, G, N] -> [E, 8R, N] dtype."""
    assert q.is_cuda and q.dtype == torch.int32 and z.dtype == torch.int32 and dtype in (torch.bfloat16, torch.float16)
    q, z, s = q.contiguous(), z.contiguous(), s.contiguous()
    E, R, N = q.shape
    G, NZ = s.shape[1], z.shape[2]
    assert R * 8 == G * 128 and NZ * 8 == N
    out = torch.empty((E, R * 8, N), device=q.device, dtype=dtype)
    BLOCK_R, BLOCK_N = 16, 128
    grid = (E, triton.cdiv(R, BLOCK_R), triton.cdiv(N, BLOCK_N))
    with torch.cuda.device(q.device):
        _dequant_kernel[grid](q, z, s, out, R, N, G, NZ, OUT_BF16=(dtype == torch.bfloat16), BLOCK_R=BLOCK_R, BLOCK_N=BLOCK_N)
    return out
