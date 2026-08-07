
# --- fixture cloak materialization (panel finding: 43 intentionally-vulnerable
# workflow files shipped under .github/workflows/ paths, which registry
# scanners attribute to THIS repo as live automation — the class that once
# rated ci-score CRITICAL). The TRACKED tree stores them cloaked
# (dot-github/ + .fixture suffix, byte-identical); this hook inverts the
# rename into the original (gitignored, never-tracked) paths before
# collection so every existing test path keeps working. ci-score's
# _fixture_checkouts.py is the precedent.
import hashlib as _hl, json as _json, pathlib as _pl

def _materialize_cloaked_fixtures():
    tests = _pl.Path(__file__).resolve().parent
    jobs = [(tests / "fixtures/dot-github", tests / "fixtures/.github",
             tests / "fixtures/cloak-manifest.json")]
    ev = tests.parent / "evals/files"
    if ev.is_dir():
        for d in sorted(ev.iterdir()):
            if (d / "dot-github").is_dir():
                jobs.append((d / "dot-github", d / ".github", None))
    for src, dst, man in jobs:
        if not src.is_dir():
            continue
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(src)
            assert rel.name.endswith(".fixture"), f"uncloaked file in {src}: {rel}"
            out = dst / rel.parent / rel.name[: -len(".fixture")]
            out.parent.mkdir(parents=True, exist_ok=True)
            data = f.read_bytes()
            if not out.exists() or out.read_bytes() != data:
                out.write_bytes(data)
            if man is not None:
                expect = _json.loads(man.read_text()).get(str(out.relative_to(dst)))
                assert expect == _hl.sha256(data).hexdigest(), (
                    f"cloak round-trip drift: {rel}")

_materialize_cloaked_fixtures()
