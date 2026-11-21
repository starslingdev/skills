- **2026-11-21** — Hold OPT89's optional native-runner sharding out of default reports, and state the added runner-minute cost of OPT89 and OPT97 job-adding recipes under `--bmf` (#465).
- **2026-11-21** — A default report no longer renders a held-back OPT97 card in the
  pre-start wait section; the held-back line already counts it (#465).
- **2026-11-21** — Under `--bmf`, the OPT89 and OPT97 added-cost lines name the
  job-adding option they cost, so the rest of each finding is not read as added
  cost (#465).
- **2026-11-21** — An OPT89 finding held back as a whole is counted once in the
  held-back line, by the report and by `verify_report.py` alike (#465).
