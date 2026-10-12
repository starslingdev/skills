"""OPT84 scan facts: the plugins an ESLint config loads in every config shape
(flat JS, legacy JS, `.eslintrc*` JSON / YAML, `package.json#eslintConfig`),
the Prettier plugins each package declares, and the pnpm catalog read's
unreadable marker. The engine (`collect_runs._opt84_eslint_fit`,
`_opt84_version`) reads these; the tests pin the shapes it consumes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
import collect_runs as cr  # noqa: E402
import scan  # noqa: E402

_LEGACY_EXTENDS = ["eslint:recommended", "plugin:react/recommended",
                   "plugin:react-hooks/recommended",
                   "plugin:@typescript-eslint/recommended"]


def _repo(tmp_path, pkg, files=None):
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    for name, text in (files or {}).items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def _facts(tmp_path, files, pkg=None, path=None):
    block = scan._read_type_aware_lint(_repo(tmp_path, pkg or {}, files))
    key = path or next(iter(files))
    return block["eslint_config_facts"][key]


def _assert_legacy(facts):
    assert {"react", "react-hooks", "@typescript-eslint"} <= set(facts["plugins"]), facts
    assert facts["exhaustive_deps"] is True, facts


# ---- 1a: plugins named only through `plugin:*` extends ------------------------

def test_eslintrc_json_extends_plugin_presets_name_their_plugins(tmp_path):
    cfg = json.dumps({"root": True, "extends": _LEGACY_EXTENDS})
    _assert_legacy(_facts(tmp_path, {".eslintrc.json": cfg}))


def test_eslintrc_js_extends_plugin_presets_name_their_plugins(tmp_path):
    cfg = "module.exports = {\n  extends: " + json.dumps(_LEGACY_EXTENDS) + ",\n};\n"
    _assert_legacy(_facts(tmp_path, {".eslintrc.js": cfg}))


def test_scoped_plugin_extends_shapes():
    assert scan._tal_plugin_from_extends("plugin:@scope/name/cfg") == "@scope/name"
    assert scan._tal_plugin_from_extends("plugin:@scope/cfg") == "@scope"
    assert scan._tal_plugin_from_extends("plugin:react/jsx-runtime") == "react"
    assert scan._tal_plugin_from_extends("plugin:eslint-plugin-vue/base") == "vue"
    assert scan._tal_plugin_from_extends("eslint:recommended") is None


def test_react_hooks_preset_sets_exhaustive_deps_alone(tmp_path):
    cfg = json.dumps({"extends": ["plugin:react-hooks/recommended"]})
    facts = _facts(tmp_path, {".eslintrc.json": cfg})
    assert facts["plugins"] == ["react-hooks"]
    assert facts["exhaustive_deps"] is True


# ---- 1b: YAML / unquoted lists read from the parsed object --------------------

def test_eslintrc_yaml_block_list_plugins(tmp_path):
    cfg = "root: true\nplugins:\n  - react\n  - vue\n  - eslint-plugin-unicorn\n"
    facts = _facts(tmp_path, {".eslintrc.yml": cfg})
    assert {"react", "vue", "unicorn"} <= set(facts["plugins"]), facts


def test_eslintrc_yaml_unquoted_flow_list_plugins(tmp_path):
    cfg = ("plugins: [react, '@typescript-eslint']\n"
           "extends:\n  - plugin:react-hooks/recommended\n")
    facts = _facts(tmp_path, {".eslintrc.yaml": cfg})
    assert {"react", "@typescript-eslint", "react-hooks"} <= set(facts["plugins"]), facts
    assert facts["exhaustive_deps"] is True


def test_eslintrc_overrides_plugins_count(tmp_path):
    cfg = json.dumps({"overrides": [{"files": ["*.vue"], "plugins": ["vue"]}]})
    facts = _facts(tmp_path, {".eslintrc": cfg})
    assert "vue" in facts["plugins"], facts


# ---- 1c: package.json eslintConfig -------------------------------------------

def test_package_json_eslint_config_gets_fit_facts(tmp_path):
    pkg = {"devDependencies": {"eslint": "^8.57.0"},
           "eslintConfig": {"extends": _LEGACY_EXTENDS, "plugins": ["jsx-a11y"]}}
    block = scan._read_type_aware_lint(_repo(tmp_path, pkg))
    facts = block["eslint_config_facts"]["package.json"]
    _assert_legacy(facts)
    assert "jsx-a11y" in facts["plugins"]
    # The engine reads these facts: a framework plugin makes it a dual-run.
    configs = [c for c in block["configs"] if c["path"] == "package.json"]
    reasons = cr._opt84_eslint_fit(block, configs)
    assert any("framework plugin react " in r for r in reasons), reasons


def test_flat_config_names_stay_in_the_engine_shape(tmp_path):
    cfg = ("import react from 'eslint-plugin-react';\n"
           "import ts from '@typescript-eslint/eslint-plugin';\n"
           "export default [{ plugins: { react, '@typescript-eslint': ts } }];\n")
    facts = _facts(tmp_path, {"eslint.config.js": cfg})
    assert {"react", "@typescript-eslint"} <= set(facts["plugins"]), facts


# ---- 2: prettier plugins -------------------------------------------------------

def test_scan_records_prettier_plugins_per_package_dir(tmp_path):
    root = _repo(tmp_path, {
        "devDependencies": {"prettier": "^3.3.0",
                            "prettier-plugin-tailwindcss": "^0.6.0",
                            "@ianvs/prettier-plugin-sort-imports": "^4.0.0",
                            "@acme/prettier-plugin": "1.0.0",
                            "eslint-plugin-prettier": "^5.0.0"}},
        {"packages/web/package.json": json.dumps(
            {"dependencies": {"prettier-plugin-svelte": "^3.0.0"}}),
         "packages/api/package.json": json.dumps({"devDependencies": {"prettier": "^3"}})})
    block = scan._read_type_aware_lint(root)
    assert block["prettier_plugins"] == {
        "": ["@acme/prettier-plugin", "@ianvs/prettier-plugin-sort-imports",
             "prettier-plugin-tailwindcss"],
        "packages/web": ["prettier-plugin-svelte"]}


def test_scan_block_defaults_carry_prettier_plugins():
    assert scan._tal_block()["prettier_plugins"] == {}


# ---- 2b: Prettier plugins named in Prettier's own config files ----------------

def test_scan_reads_prettier_plugins_from_every_data_config_shape(tmp_path):
    root = _repo(tmp_path, {"prettier": {"plugins": ["prettier-plugin-pkgjson"]}}, {
        ".prettierrc": "plugins:\n  - prettier-plugin-rc-yaml\n",
        "a/package.json": "{}",
        "a/.prettierrc.json": json.dumps({"plugins": ["prettier-plugin-astro"]}),
        "b/package.json": "{}",
        "b/.prettierrc.yml": "plugins: [prettier-plugin-svelte]\n",
        "c/package.json": "{}",
        "c/.prettierrc.json5": '{"plugins": ["./local-plugin.js"]}',
        "d/package.json": "{}",
        "d/.prettierrc.yaml": "semi: false\n"})
    block = scan._read_type_aware_lint(root)
    assert block["prettier_plugins"] == {
        "": ["prettier-plugin-pkgjson", "prettier-plugin-rc-yaml"],
        "a": ["prettier-plugin-astro"], "b": ["prettier-plugin-svelte"],
        "c": ["./local-plugin.js"]}
    assert block["prettier_plugin_files"] == {
        "": {"prettier-plugin-pkgjson": "package.json",
             "prettier-plugin-rc-yaml": ".prettierrc"},
        "a": {"prettier-plugin-astro": "a/.prettierrc.json"},
        "b": {"prettier-plugin-svelte": "b/.prettierrc.yml"},
        "c": {"./local-plugin.js": "c/.prettierrc.json5"}}
    assert block["prettier_config_unreadable"] == {}
    # `config_files` stays the presence list it was
    assert block["config_files"]["a"] == [".prettierrc.json"]


def test_a_dependency_plugin_keeps_naming_the_package_json(tmp_path):
    root = _repo(tmp_path, {"devDependencies": {"prettier-plugin-x": "1"}},
                 {".prettierrc.json": json.dumps({"plugins": ["prettier-plugin-x"]})})
    block = scan._read_type_aware_lint(root)
    assert block["prettier_plugins"] == {"": ["prettier-plugin-x"]}
    assert block["prettier_plugin_files"] == {}


def test_scan_marks_prettier_configs_it_does_not_open(tmp_path):
    root = _repo(tmp_path, {"prettier": "@acme/prettier-config"}, {
        "a/package.json": "{}", "a/prettier.config.mjs": "export default {};\n",
        "b/package.json": "{}", "b/.prettierrc.cjs": "module.exports = {};\n",
        "c/package.json": "{}", "c/.prettierrc.json": "{not json",
        "d/package.json": "{}", "d/.prettierrc.ts": "export default {};\n",
        "e/package.json": "{}", "e/.prettierrc.toml": "semi = false\n"})
    block = scan._read_type_aware_lint(root)
    assert block["prettier_config_unreadable"] == {
        "": ["package.json"], "a": ["a/prettier.config.mjs"],
        "b": ["b/.prettierrc.cjs"], "c": ["c/.prettierrc.json"],
        "d": ["d/.prettierrc.ts"], "e": ["e/.prettierrc.toml"]}


def test_scan_block_defaults_carry_the_prettier_config_keys():
    b = scan._tal_block()
    assert b["prettier_plugin_files"] == {} and b["prettier_config_unreadable"] == {}


def test_the_block_shape_refuses_an_unknown_key():
    import pytest
    with pytest.raises(KeyError):
        scan._tal_block(prettier_plugin=["typo"])


def test_the_reader_crash_message_names_both_patterns(tmp_path, monkeypatch, capsys):
    def _boom(_root):
        raise RuntimeError("x")
    monkeypatch.setattr(scan, "_read_type_aware_lint_unguarded", _boom)
    block = scan._read_type_aware_lint(tmp_path)
    assert block["error"] == "RuntimeError"
    err = capsys.readouterr().err
    assert "OPT82" in err and "OPT84" in err, err


# ---- 3: pnpm catalogs ----------------------------------------------------------

def test_pnpm_workspace_malformed_is_marked_unreadable(tmp_path):
    root = _repo(tmp_path, {}, {"pnpm-workspace.yaml": "catalog: [unclosed\n  - : :\n"})
    block = scan._read_type_aware_lint(root)
    assert block["pnpm_catalogs"] == {}
    assert block["pnpm_catalogs_unreadable"] is True


def test_pnpm_workspace_absent_or_readable_is_not_marked(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert scan._read_type_aware_lint(_repo(a, {}))["pnpm_catalogs_unreadable"] is False
    blk = scan._read_type_aware_lint(_repo(b, {}, {
        "pnpm-workspace.yaml": "packages: ['p/*']\ncatalog:\n  typescript: ^5.6.2\n"}))
    assert blk["pnpm_catalogs_unreadable"] is False
    assert blk["pnpm_catalogs"] == {"default": {"typescript": "^5.6.2"}}
    assert blk["unreadable"] == []          # the ESLint-config list is untouched
    assert scan._tal_block()["pnpm_catalogs_unreadable"] is False


def test_malformed_catalogs_default_lets_the_catalog_key_win(tmp_path):
    one = tmp_path / "one"
    one.mkdir()
    _repo(one, {"devDependencies": {"typescript": "catalog:",
                                    "prettier": "catalog:default"}},
          {"pnpm-workspace.yaml": (
              "catalogs:\n  default: not-a-map\n  ts6:\n    typescript: ^6.0.3\n"
              "catalog:\n  typescript: ^5.6.2\n  prettier: ^3.3.0\n")})
    block = scan._read_type_aware_lint(one)
    assert cr._opt84_version(block, "", "typescript") == "^5.6.2"
    assert cr._opt84_version(block, "", "prettier") == "^3.3.0"
    assert block["pnpm_catalogs"]["ts6"] == {"typescript": "^6.0.3"}
    # Both maps present: the top-level `catalog:` wins a conflict.
    two = tmp_path / "two"
    two.mkdir()
    _repo(two, {"devDependencies": {"typescript": "catalog:"}},
          {"pnpm-workspace.yaml": ("catalogs:\n  default:\n    typescript: ^4.9.0\n"
                                   "catalog:\n  typescript: ^5.6.2\n")})
    assert cr._opt84_version(scan._read_type_aware_lint(two), "", "typescript") == "^5.6.2"


def test_a_tab_indented_json_prettierrc_names_its_plugin(tmp_path):
    # A `.prettierrc` written as JSON indented with tabs: YAML refuses tab
    # indentation, so the read must try JSON first.
    rc = ('{\n\t"useTabs": true,\n\t"singleQuote": true,\n\t"trailingComma": "none",\n'
          '\t"printWidth": 100,\n\t"plugins": ["prettier-plugin-svelte"],\n'
          '\t"overrides": [{ "files": "*.svelte", "options": { "parser": "svelte" } }]\n}\n')
    block = scan._read_type_aware_lint(_repo(tmp_path, {}, {".prettierrc": rc}))
    assert block["prettier_plugins"][""] == ["prettier-plugin-svelte"]
    assert block["prettier_plugin_files"][""]["prettier-plugin-svelte"] == ".prettierrc"
    assert block["prettier_config_unreadable"] == {}


# ---- E2: a Prettier config with no package.json beside it ----------------------

def _tree(tmp_path, files):
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def _prettier_run(block, run="npx prettier --check .", wd=None):
    sys.path.insert(0, str(_SKILL_DIR / "tests"))
    import test_opt84_tool_swap as t84
    step = {"run": run, **({"working-directory": wd} if wd else {})}
    out, withheld, cands = t84._run(step, block=block)
    assert len(out) == 1, (withheld, cands)
    return t84._tool(out[0], "prettier")


def test_a_root_prettierrc_with_no_root_package_json_is_read(tmp_path):
    root = _tree(tmp_path, {
        ".prettierrc": json.dumps({"plugins": ["prettier-plugin-tailwindcss"]}),
        "web/package.json": json.dumps({"devDependencies": {"prettier": "^3.3.0"}})})
    block = scan._read_type_aware_lint(root)
    assert block["prettier_plugins"].get("") == ["prettier-plugin-tailwindcss"], block
    assert block["prettier_plugin_files"].get("") == {
        "prettier-plugin-tailwindcss": ".prettierrc"}
    t = _prettier_run(block, wd="web")
    assert t["fit"] == "dual-run", t
    assert any("`.prettierrc`" in r and "prettier-plugin-tailwindcss" in r
               for r in t["fit_reasons"]), t["fit_reasons"]


def test_a_config_only_subfolder_below_a_root_run_is_read(tmp_path):
    root = _repo(tmp_path, {"devDependencies": {"prettier": "^3.3.0"}}, {
        "docs/.prettierrc.json": json.dumps({"plugins": ["prettier-plugin-astro"]})})
    block = scan._read_type_aware_lint(root)
    assert block["prettier_plugin_files"].get("docs") == {
        "prettier-plugin-astro": "docs/.prettierrc.json"}, block
    t = _prettier_run(block)
    assert t["fit"] == "dual-run", t
    assert any("`docs/.prettierrc.json`" in r for r in t["fit_reasons"]), t["fit_reasons"]


def test_a_config_only_folder_with_a_js_config_is_marked_unopened(tmp_path):
    root = _repo(tmp_path, {}, {"docs/prettier.config.js": "module.exports = {};\n"})
    block = scan._read_type_aware_lint(root)
    assert block["prettier_config_unreadable"] == {"docs": ["docs/prettier.config.js"]}


def test_a_config_only_folder_the_walk_skips_is_not_read(tmp_path):
    root = _repo(tmp_path, {}, {
        "node_modules/x/.prettierrc.json": json.dumps({"plugins": ["p"]})})
    block = scan._read_type_aware_lint(root)
    assert block["prettier_plugins"] == {} and block["prettier_config_unreadable"] == {}


def test_a_package_folder_that_cannot_be_listed_is_marked_unreadable(tmp_path,
                                                                     monkeypatch):
    import os
    root = _repo(tmp_path, {}, {"web/package.json": "{}"})
    real = os.listdir

    def _listdir(p="."):
        if str(p).endswith("/web"):
            raise PermissionError(13, "denied")
        return real(p)
    monkeypatch.setattr(scan.os, "listdir", _listdir)
    block = scan._read_type_aware_lint(root)
    assert list(block["prettier_config_unreadable"]) == ["web"], block
    assert block["prettier_config_unreadable"]["web"], block


def test_a_folder_the_walk_itself_cannot_list_is_marked_unreadable(tmp_path, monkeypatch):
    """Independent of how pathlib lists directories on this Python: the walk's own
    listing of `web` fails, so `web` is named as a place whose Prettier configs
    could not be read (Python 3.9's pathlib ignores a patched `os.listdir`; newer
    versions honour it, which is why the sibling test was green here and red in
    CI before the walk recorded the folder)."""
    from pathlib import Path
    root = _repo(tmp_path, {}, {"web/package.json": "{}"})
    real = Path.iterdir

    def _iterdir(self):
        if self.name == "web":
            raise PermissionError(13, "denied")
        return real(self)
    monkeypatch.setattr(Path, "iterdir", _iterdir)
    block = scan._read_type_aware_lint(root)
    assert list(block["prettier_config_unreadable"]) == ["web"], block
    assert block["prettier_config_unreadable"]["web"] == [
        scan._tal_unlisted_folder("web")], block
    assert block["truncated"] is True
