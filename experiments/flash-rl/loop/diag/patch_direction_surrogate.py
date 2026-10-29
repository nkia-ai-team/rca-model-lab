"""fast19 direction test: also evaluate the IMPLEMENTED corrected-GRPO surrogate (importance correction, clipping, masks,
turn weights) on the scored turns before and after the step (2026-10-26, Codex review). Proximal = pre-step full-forward."""
from pathlib import Path

r = Path("general_update_runner.py"); t = r.read_text()
old = '''                    total_tokens = sum(r_["tokens"] for r_ in rows_out)'''
new = '''                    from rca_lab.train import grpo_correction
                    def implemented_loss(cur_map):
                        total = 0.
                        for (i, j) in picks:
                            turn = episodes[i]["turns"][j]
                            cur = cur_map[(i, j)].reshape(1, -1).float()
                            prox = before1[(i, j)].reshape(1, -1).float()
                            old = torch.tensor(turn["old_logprobs"], dtype=torch.float32)[None]
                            res = grpo_correction.corrected_grpo_loss(
                                cur, prox, old, prox, torch.ones_like(cur, dtype=torch.bool),
                                update.groups[g]["advantages"][i:i + 1].float(), importance_cap=update.training["importance_cap"],
                                clip_epsilon=update.training["clip_epsilon"], kl_beta=0.)
                            total += float(res.loss) * cur.numel() / episodes[i]["generated_tokens"] / len(update.active)
                        return total
                    surrogate_loss = dict(before=implemented_loss(before1), repeat=implemented_loss(before2), after=implemented_loss(after))
                    total_tokens = sum(r_["tokens"] for r_ in rows_out)'''
assert t.count(old) == 1
t = t.replace(old, new)
old2 = '''"surrogate_change": surrogate,'''
assert t.count(old2) == 1
t = t.replace(old2, '''"surrogate_change": surrogate, "implemented_surrogate_loss": surrogate_loss,''')
r.write_text(t)
print("patched implemented surrogate")
