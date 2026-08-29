# Bridge runtime integrity

The bridge is a CPython 3.12 application with no third-party Python runtime
packages. `security/runtime-manifest.json` makes that claim executable:

- every runtime Python import must be from the standard library or `bridge`;
- every runtime source, launcher, service unit, and installed skill is SHA-256
  bound to the reviewed manifest;
- required and feature-gated host commands are explicit;
- `scripts/verify_deploy_snapshot.sh` refuses dirty, stale, or incomplete runtime
  snapshots before a deployment.

`uv.lock` hash-locks the test and audit toolchain. CI checks lock drift, verifies
the source manifest, audits the fully hashed development graph, and runs the
test suite on pull requests, main, and weekly. If a third-party runtime import
is intentionally added, declare the package in `pyproject.toml`, regenerate the
lock, and extend the manifest generator in the same reviewed change.

External host tools are not PyPI dependencies and this repository does not pretend to
hash-lock their package-manager artifacts; host patching and base-system provenance remain
separate operational controls.
