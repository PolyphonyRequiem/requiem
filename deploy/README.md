# Deploying the requiem delivery fleet

Container scaffolding for the **hermetic, requiem-managed fleet** (ADR-0017 §2).
This is honest, operator-facing scaffolding: it shows *how* an operator brings
the fleet up, but it has **not** been validated against a live Hermes gateway,
real ADO credentials, or a live worker run — that is the operator's `--live`
action in their own environment.

## The two halves (do not conflate them)

- **Reproducibility comes from immutable inputs.** `Dockerfile` bakes the three
  `fleet/requiem-*` profile distributions in as a *read-only template* under
  `/opt/requiem/fleet`. Nothing writes there at run time.
- **Clean execution comes from fresh runtime homes.** `entrypoint.sh` provisions
  a brand-new `HERMES_HOME` per run under `/var/lib/requiem/runs/<run-id>/`,
  installs the template profiles into it, and only then hands off to the base
  image's supervised `gateway run`. A persisted profile home is never reused, so
  kanban task-state cannot leak across runs.

## Bring-up

```bash
export ADO_PAT=...                # never commit this
export ANTHROPIC_API_KEY=...      # matches each profile's config.yaml model
docker compose -f deploy/docker-compose.yml up --build
```

The entrypoint fails the container (rather than running degraded) if a profile
distribution is missing or any profile is not in **Manual** orchestration
(`kanban.auto_decompose: false`) — requiem is the only decomposition authority.

## Where requiem's own gate fits

The container's entrypoint check is belt-and-braces. The authoritative,
fail-closed gate is `requiem.fleet_preflight.evaluate_fleet` (pure logic, unit
tested in `tests/test_fleet_preflight.py`): given the baseline delivery roles
resolved through `process.yaml`, a committed expected fleet lock, and a
`FleetInventory` gathered from the running container, it refuses the run on a
missing profile, non-Manual orchestration, a disabled dispatcher, a home that
escapes the run root, unauthorized writable memory, or any pinned-but-
unverifiable version/hash.

The operator-side adapter that turns a live container into a `FleetInventory`
(parsing `hermes profile list` / `hermes config get` / container labels) is the
one piece deferred until it can be built against a real gateway. Everything it
feeds is already judged by the tested pure logic above.
