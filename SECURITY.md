# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.6.x   | Yes       |
| 0.5.x   | No (fix forward) |
| < 0.5   | No        |

## Reporting a vulnerability

Email **leon2theuniverse@gmail.com** with the subject `SECURITY: <short summary>`.
Do **not** open a public issue for a suspected vulnerability.

We aim to acknowledge within 7 days. Once a fix is verified, we will publish a
note in the changelog. Please give us a reasonable window before public
disclosure.

## What this project handles, and what it must never handle

- API keys are read from the environment only and are never logged, printed,
  or written to disk. See `.gitignore` (`.env`) and the secret-scan job in CI.
- Client briefs and generated copy may only exist under `.data/`, which is
  gitignored. No real client data belongs in this repository.
- The legal audit is a first-pass filter, not legal advice. A human approves
  every piece of published copy; the deterministic blocklist cannot be
  bypassed by the model or by an operator prompt.
- The audit ledger and the quota state file (`.data/`) are local artifacts.
  Copying them outside the machine that produced them is your decision, not
  ours — treat them as client data.

## Safe harbor

Good-faith security research on your own checkout is welcome. Do not probe
systems you do not own, do not use real client data in tests, and do not
disable the blocklist or the human gate to "see what happens" with real data.
