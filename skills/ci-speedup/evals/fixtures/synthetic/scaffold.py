#!/usr/bin/env python3
"""Scaffold the `synthetic` eval fixture: the offline e2e test's repository and
its replay corpus, built by that test's own helpers so the two never drift.

    scaffold.py <empty sandbox dir> <aux dir>

Writes the git checkout (origin `synthetic/repo`) into the sandbox and the
replay corpus into the aux dir, then prints the session's environment as
KEY=VALUE lines for the eval runner. Maintainer harness only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
import test_offline_pipeline_e2e as e2e  # noqa: E402

# The e2e test brackets the time-dependent monthly-volume fixture over ~30
# seconds; an agent reaches the data pass minutes after scaffolding, so the
# same endpoints are written over a 25-minute band (the session's own budget).
_BAND_S = 1500


def main(sandbox: Path, aux: Path) -> None:
    e2e._init_repo(sandbox)
    replay = e2e._replay_dir(aux)
    wf_ids = [getattr(e2e, n) for n in dir(e2e) if re.fullmatch(r"_WF\d*_ID", n)]
    now = dt.datetime.now(dt.timezone.utc)
    body = json.dumps({"total_count": 42})
    for off in range(-1, _BAND_S):
        since = (now + dt.timedelta(seconds=off, days=-30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for wf in wf_ids:
            for event in ("", "&event=pull_request"):
                ep = f"repos/{e2e._REPO}/actions/workflows/{wf}/runs?per_page=1{event}&created=>={since}"
                (replay / e2e.cr._fixture_name(ep, "json")).write_text(body, encoding="utf-8")
    print(f"CI_SPEEDUP_GH_FIXTURES={replay}")
    print(f"EVAL_GH_SHIM_REPO={e2e._REPO}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
