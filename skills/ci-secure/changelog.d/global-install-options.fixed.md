- **2026-11-25** — Recognize project dependency installs with global package-manager options before the install command (#500).
  An option the scanner does not know still reports the install when the
  word after it is an install command, or when reading that word as the
  option's value leaves one (`npm --workspaces ci`). npm's `-C` selects the
  install directory for the packageManager pin like `--prefix`, and npm's
  `-w <name>` is read as taking a value.
