# Deploying hermiq-exec

hermiq-exec is an AppAPI **docker-install** ExApp with one hard constraint
driving everything below: **AppAPI has no per-app egress control**. The only
network knob AppAPI exposes is the deploy daemon's `net` field — whatever
Docker network the daemon is registered with, every ExApp container it
deploys gets attached to. There is no allowlist, no proxy setting, nothing
app-specific. Egress control has to be built at the Docker network layer,
*before* the app is ever registered.

That's what `docker-compose.jail.yml` does (see the comments in that file
for the full topology + the specific gap it closes vs. Specter's original
`concurrentie-analyse/docker-compose.intelligence.yml` firewall-sidecar
pattern). This README covers the steps around it: creating the locked-down
network, registering the daemon against it, and registering + enabling the
app itself.

**Everything below is written from the plan + AppAPI's documented `occ`
commands (cross-checked against the sibling n8n-nextcloud ExApp's README,
which documents the same `app_api:app:register` call for a working,
shipped ExApp). None of it has been run against a live Nextcloud instance
from this scaffold — treat the exact flag names as a strong draft, not a
guarantee, until it's been exercised once for real.**

## 1. Pre-create the locked-down network

This has to happen *before* daemon registration — AppAPI does not create or
manage the network's isolation properties itself, it only attaches
containers to whatever network you tell it about.

```bash
docker network create \
  --internal \
  --driver bridge \
  hermiq-exec-nc
```

`--internal` is what actually matters here: Docker refuses a default route
to the internet for any container on this network, full stop, independent
of any iptables rule inside a container. Containers on it can still reach
each other (including the `nextcloud` container) by name — which is exactly
the NC↔ExApp control channel this network exists for.

## 2. Register (or reuse) the deploy daemon against that network

```bash
docker exec -u www-data nextcloud php occ app_api:daemon:register \
    hermiq-exec-daemon \
    "Hermiq Execution Daemon" \
    docker-install \
    http \
    dockersocketproxy:2375 \
    http://nextcloud \
    --net hermiq-exec-nc
```

(Swap `docker-install` + a socket-proxy for HaRP if that's the daemon
already used for other ExApps on this instance — HaRP means no host port
gets published for the app, which is the preferred mode per plan §6.5;
the `--net` flag is the part that matters for this design either way.)

## 3. Register the app

```bash
docker exec -u www-data nextcloud php occ app_api:app:register \
    hermiq_exec \
    hermiq-exec-daemon \
    --info-xml https://github.com/ConductionNL/hermiq-exec/raw/main/appinfo/info.xml \
    --force-scopes
```

This is where AppAPI generates `APP_SECRET` and starts the container from
`appinfo/info.xml`'s `<docker-install>` image reference
(`ghcr.io/conductionnl/hermiq-exec:latest`) — attached to `hermiq-exec-nc`
per step 2, so it inherits the egress restriction automatically. The image
itself does not need to be built locally first if it's already published to
`ghcr.io`; for local/dev testing, build it via `docker-compose.jail.yml`
instead (see below) and point the daemon at a local registry/tag.

## 4. Enable the app

```bash
docker exec -u www-data nextcloud php occ app_api:app:enable hermiq_exec
```

This triggers the `/enabled` callback (`ex_app/lib/main.py::enabled_callback`),
which registers the two TaskProcessing providers
(`hermiq:doc-analyze`, `hermiq:agent-exec`) and starts the poll worker.
Disabling the app unregisters both providers and stops polling — Hermiq's
enqueue calls will simply have no provider to route to while disabled,
which is the correct fail-closed behavior.

## Local/dev testing with the jail compose file

`docker-compose.jail.yml` reproduces the same `--internal` network +
iptables-sidecar shape for local testing, without needing a full AppAPI
daemon registration round-trip:

```bash
cd deploy
export HERMIQ_EXEC_APP_SECRET=dev-secret
export NEXTCLOUD_URL=http://host.docker.internal:8080
export HERMIQ_EXEC_NC_NETWORK=hermiq-exec-nc   # from step 1 above
export CLAUDE_CREDENTIALS_PATH=~/.claude/.credentials.json
docker compose -f docker-compose.jail.yml up --build
```

Verified in this scaffold (no live NC/AppAPI, no network egress in this
environment — see the repo README's honesty note): `docker compose -f
docker-compose.jail.yml config` resolves cleanly, including the
`network_mode: service:egress-gate` + external-network wiring. **Not**
verified: an actual `docker build` of the Dockerfile (this sandbox has no
route to any container registry — including, fittingly, the exact kind of
egress this design exists to restrict), or a live run against a real
Nextcloud + AppAPI + Anthropic endpoint. Build and smoke-test both before
first real registration.

## Environment variables

See `appinfo/info.xml`'s `<environment-variables>` block for the
admin-facing list (`HERMIQ_EXEC_MAX_JOBS`, `HERMIQ_EXEC_POLL_INTERVAL`,
`HERMIQ_EXEC_POLL_BATCH`, `COMPUTE_DEVICE`). `APP_ID` / `APP_SECRET` /
`NEXTCLOUD_URL` are set by AppAPI itself at registration time in a real
deploy; they're surfaced explicitly in `docker-compose.jail.yml` only for
local testing.
