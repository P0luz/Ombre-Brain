# 2026-11 dependency security update

This fork updates MCP 1.29.0 to 1.30.0, PyJWT 2.13.0 to 2.15.1 and certifi
2026.7.22 to 2026.10.23. Other resolved package versions remain unchanged.
The production and development locks include package hashes. Their resolution
keeps the original August index snapshot, advancing only these three packages
to the fixed November 1 UTC snapshot recorded in CI.

This requires a dependency installation, not only a hot code update. For Docker
or Zeabur, rebuild the image from the updated repository and recreate the service
with its existing persistent volumes and environment. For an existing virtual
environment, stop the service, run `python -m pip install --require-hashes -r
requirements.lock.txt`, verify `python -m pip check`, then restart it. Preserve a
current data backup and the previous deployment configuration before switching.

The source archive now includes both requirements.txt and requirements.lock.txt.
Older updaters must detect the dependency change instead of treating this archive
as code-only. Automatic pip installation remains disabled by default; do not
enable it merely to bypass the rebuild requirement. Verify installed versions
inside the running service, then check authentication, MCP initialization and
dependent clients. A source-code version string alone does not prove this fix is
installed. Security auditing remains enabled without ignore rules.
