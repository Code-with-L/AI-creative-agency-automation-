## What changed

<!-- One paragraph: what & why. Link issues. -->

## Safety checklist

- [ ] No real client data added or referenced anywhere in the diff
- [ ] `.env` / `.data/` untouched and still gitignored
- [ ] The deterministic blocklist is intact and tests still pass
- [ ] The human gate is intact: no auto-approve path reachable in live mode
- [ ] Legal audit remains a first-pass filter; `reviewed_by_human` stays False
- [ ] Heldout fixtures were not used for tuning
- [ ] Tests added/updated; `python -m pytest -q` passes
- [ ] `python -m ruff check .` clean
- [ ] Both eval splits still pass (tuned 20/20, heldout 10/10)

## Provider impact

- [ ] No live calls attempted, or quota spend was deliberate and noted
- [ ] Model names not hardcoded; `.env.example` updated if config changed
