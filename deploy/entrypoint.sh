#!/usr/bin/env bash
# requiem fleet provisioning entrypoint (ADR-0017 §2).
#
# Reconciles the two halves of the ADR: an IMMUTABLE baked profile template and
# a FRESH per-run home. We never run the gateway against a persisted profile
# home — each invocation provisions a clean HERMES_HOME so kanban task-state
# from a prior run can never leak into this one.
set -euo pipefail

FLEET_ROOT="${REQUIEM_FLEET_ROOT:-/opt/requiem/fleet}"
RUN_ID="${REQUIEM_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
RUN_HOME_ROOT="${REQUIEM_RUN_HOME_ROOT:-/var/lib/requiem/runs}"
HERMES_HOME="${RUN_HOME_ROOT}/${RUN_ID}/hermes-home"

PROFILES=(requiem-implementer requiem-reviewer requiem-closer)

echo "[requiem-fleet] run_id=${RUN_ID}"
echo "[requiem-fleet] provisioning fresh HERMES_HOME=${HERMES_HOME}"
mkdir -p "${HERMES_HOME}"
export HERMES_HOME

# Install each immutable distribution into the fresh home. The base image's
# `hermes profile install` clones/copies the distribution and strips any VCS
# metadata; installing from the read-only template keeps the image reproducible
# while the home stays per-run.
for profile in "${PROFILES[@]}"; do
  src="${FLEET_ROOT}/${profile}"
  if [[ ! -f "${src}/distribution.yaml" ]]; then
    echo "[requiem-fleet] FATAL: missing distribution ${src}" >&2
    exit 1
  fi
  echo "[requiem-fleet] installing ${profile}"
  hermes profile install "${src}" --yes
done

# Belt-and-braces: requiem is the only decomposition authority, so the gateway
# dispatcher must never auto-decompose. The profile config.yaml already pins
# this; we also assert it here so a tampered/older config fails the container
# rather than silently fanning out behind requiem's back.
export HERMES_KANBAN_DISPATCH_IN_GATEWAY="${HERMES_KANBAN_DISPATCH_IN_GATEWAY:-1}"
for profile in "${PROFILES[@]}"; do
  if ! hermes -p "${profile}" config get kanban.auto_decompose | grep -qiE '^false$'; then
    echo "[requiem-fleet] FATAL: ${profile} is not in Manual orchestration" >&2
    exit 1
  fi
done

echo "[requiem-fleet] fleet provisioned; handing off to gateway"
# Hand off to the base image's supervised gateway CMD (s6 keeps it alive).
exec "$@"
