
# --- fixture cloak materialization -------------------------------------------
# 47 intentionally-vulnerable workflow files ship under `.github/workflows/`
# paths, which registry scanners attribute to THIS repo as live automation —
# the class that once rated ci-score CRITICAL. The TRACKED tree stores them
# cloaked (dot-github/ + .fixture suffix, byte-identical); this hook inverts
# the rename into the original (gitignored, never-tracked) paths before
# collection so every existing test path keeps working. ci-score's
# _fixture_checkouts.py is the precedent.
#
# THE MANIFEST IS A CENSUS, NOT A LOOKUP. It used to be consulted only for
# files that happened to be discovered, which made it silent in both
# directions: deleting a cloaked fixture left the suite 100% green (11 of the
# negative controls are named in no test at all, so nothing else would notice),
# and an entry for a file that no longer exists was never read. Both are now
# hard errors, and the manifest spans the eval trees too — those materialize
# the same way and were previously uncovered.
#
# MATERIALIZATION ALSO PRUNES. Anything sitting at a destination path with no
# manifest entry behind it is deleted, so a renamed or removed fixture cannot
# accumulate as a stale uncloaked workflow file in a working tree — which is
# also what an install into an already-used checkout would leave behind.
import hashlib as _hl, json as _json, pathlib as _pl

_MANIFEST_NAME = "cloak-manifest.json"


def _materialize_cloaked_fixtures():
    tests = _pl.Path(__file__).resolve().parent
    skill = tests.parent
    manifest_path = tests / "fixtures" / _MANIFEST_NAME
    manifest = _json.loads(manifest_path.read_text())

    # (cloaked source dir, materialized destination dir)
    jobs = [(tests / "fixtures/dot-github", tests / "fixtures/.github")]
    ev = skill / "evals/files"
    if ev.is_dir():
        for d in sorted(ev.iterdir()):
            if (d / "dot-github").is_dir():
                jobs.append((d / "dot-github", d / ".github"))

    def key(p):
        return p.resolve().relative_to(skill).as_posix()

    discovered: dict[str, str] = {}
    for src, dst in jobs:
        if not src.is_dir():
            continue
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(src)
            if not rel.name.endswith(".fixture"):
                raise RuntimeError(
                    f"uncloaked file in {src}: {rel} — every cloaked fixture "
                    "must carry the .fixture suffix")
            out = dst / rel.parent / rel.name[: -len(".fixture")]
            out.parent.mkdir(parents=True, exist_ok=True)
            data = f.read_bytes()
            if not out.exists() or out.read_bytes() != data:
                out.write_bytes(data)
            discovered[key(out)] = _hl.sha256(data).hexdigest()

    # Prune: a materialized file with no manifest entry is stale output from a
    # fixture that was renamed or deleted. Left in place it is an uncloaked,
    # attack-shaped workflow file loitering in the tree.
    for _, dst in jobs:
        if not dst.is_dir():
            continue
        for out in sorted(dst.rglob("*")):
            if out.is_file() and key(out) not in manifest:
                out.unlink()

    # The census, both directions. `raise` and not `assert`, so `python -O`
    # cannot strip the one check that makes the manifest authoritative.
    missing = sorted(set(manifest) - set(discovered))
    extra = sorted(set(discovered) - set(manifest))
    if missing or extra:
        raise RuntimeError(
            "cloak manifest is out of sync with the cloaked fixture tree "
            f"({manifest_path.name} lists {len(manifest)}, the tree has "
            f"{len(discovered)}).\n"
            f"  in the manifest but NOT on disk (deleted fixture?): "
            f"{', '.join(missing) or 'none'}\n"
            f"  on disk but NOT in the manifest (new fixture?): "
            f"{', '.join(extra) or 'none'}\n"
            "Add or remove the entry in the same change as the fixture.")

    drifted = sorted(k for k, h in discovered.items() if manifest[k] != h)
    if drifted:
        raise RuntimeError(
            "cloak round-trip drift — the materialized bytes do not match the "
            f"manifest hash for: {', '.join(drifted)}")


_materialize_cloaked_fixtures()
