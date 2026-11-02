# Tencent production deployment

Only `main` push or a manual workflow run on `main` can deploy. Pull requests and other branches never receive the deployment job. The deployment job depends on the existing complete test jobs and transfers the exact image built in that run; it does not rebuild on the server.

The client skips a queued commit if `main` has advanced. SSH checks the committed host public key. Each repository has a separate `NANZHI_DEPLOY_KEY` secret, forced to the matching service; it cannot run a shell, forward ports, or deploy another service. Application tokens and data remain on the server.

The root-owned receiver on the server verifies archive digest, size, platform, tag, and revision label. It serializes updates, makes isolated data copies with SQLite online backups, and verifies health, invalid-token rejection, authenticated MCP initialization and tool discovery with external network access disabled. Failure at this stage leaves the running production service unchanged.

After rehearsal succeeds, it briefly enables a maintenance gate, stops only the selected service, archives its persistent data and saves the previous Compose configuration, then starts the tested image and repeats authentication checks. Success removes the maintenance gate. If failure happens after production is stopped or its data may have been migrated, the maintenance gate stays in place and the GitHub job fails. A maintainer must inspect the receipt and backup: automatic database downgrade could lose or corrupt new data, so it is deliberately not attempted.

Server paths (root only):

- `/usr/local/sbin/nanzhi-deploy`: deployment receiver.
- `/opt/nanzhi-deploy/runs/`: per-attempt receipts, previous configuration and pre-update data archive.
- `/opt/nanzhi-production/compose.json`: production services and private environment.
- `/var/lib/nanzhi-deploy-gates/`: persistent maintenance flags; remove only after verifying recovery.

Other services are manual. Old Zeabur Git triggers are disabled. Do not restart old Zeabur applications; legacy domains temporarily relay to the new server. Never print or commit production configurations, deployment private keys or data backups.
