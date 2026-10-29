"""2026-10-26 bug fix (fast18/fast19): AdamW.load_state_dict restores the saved param_group hyperparameters, so every
update ran at the learning rate saved by the first update (5e-5, measured in adapter.pt of u005..u062) and the configured
--learning-rate (1e-4 from u19, rollback halvings, the 10-24 restore) never took effect. Re-apply the configured
learning rate after loading the optimizer state and record it."""
from pathlib import Path

p = Path("general_update.py"); s = p.read_text()


def rep(text, old, new, count=1):
    assert text.count(old) == count, (old[:80], text.count(old))
    return text.replace(old, new)


s = rep(s, '''                self._optimizer.load_state_dict(self.start_optimizer["state"])
        return self._optimizer''', '''                self._optimizer.load_state_dict(self.start_optimizer["state"])
            for group in self._optimizer.param_groups:   # load_state_dict restores the saved lr; use the configured one
                group["lr"] = self.training["learning_rate"]
        return self._optimizer''')
s = rep(s, '''            optimizer.load_state_dict(self.start_optimizer["state"])
        optimizer.step()''', '''            optimizer.load_state_dict(self.start_optimizer["state"])
        for group in optimizer.param_groups:   # 2026-10-26: load_state_dict restored the saved lr; use the configured one
            group["lr"] = self.training["learning_rate"]
        optimizer.step()''')
s = rep(s, '''        result = dict(status="passed", optimizer_steps=self.start_step + self.steps, steps_this_update=self.steps,''',
        '''        result = dict(status="passed", optimizer_steps=self.start_step + self.steps, steps_this_update=self.steps,
                      effective_learning_rate=self._optimizer.param_groups[0]["lr"],''')
p.write_text(s)
print("lr fix applied")
