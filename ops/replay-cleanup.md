# Replay cleanup contract

Plan: retain case-finally cleanup as the primary path; register a generation-specific
lease for each case, then reclaim only registered, unowned generations with a user
systemd timer. Cover live owner, PID reuse, retained cases, foreign containers,
partial failure and retry before enabling the timer. No global Docker pruning.

Normal cases remove their DBs immediately. The timer runs every two minutes with
a two-minute minimum lease age for orphaned active leases after owner death.
Finished cases are eligible on the next timer run.
Running owners and explicit `--keep-db`/`prepare` retention remain protected.
Each newly restored DB carries its lease generation label. Previously running
cases can be adopted only with verified PID identity and exact container IDs.

Install the units in `ops/systemd/replay-cleanup.*` into `~/.config/systemd/user/`,
then run `systemctl --user daemon-reload` and
`systemctl --user enable --now replay-cleanup.timer`.
Inspect `journalctl --user -u replay-cleanup.service` for actions and failures.
The reaper defaults to dry-run; the service explicitly uses `--apply`.

Deployed on 104 on 2026-10-14: user timer enabled and active, user linger enabled.
A real canary was protected while active and its container and anonymous volume
were removed after completion. The two existing evaluation projects were adopted
by exact container/network IDs and owner PID identity, without restarting them.
Evidence: `outputs/replay-cleanup-20261014/auto-cleanup-verification.json`.
Unregistered standalone restores and explicit retained leases are not swept.
