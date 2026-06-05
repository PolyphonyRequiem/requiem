# requiem delivery fleet — container image (ADR-0017 §2).
#
# Reproducibility comes from IMMUTABLE inputs: this image bakes the requiem-*
# profile distributions as a read-only template. Clean execution comes from a
# FRESH per-run HERMES_HOME provisioned at container start — never from a
# long-lived profile home baked into the image (which would leak kanban
# task-state across runs). The entrypoint reconciles the two: install the
# template profiles into a fresh home, then hand off to the base image's
# supervised `gateway run`.

FROM nousresearch/hermes-agent:latest

# Immutable, distribution-owned profile sources. NOT a runtime home — nothing
# here is ever written to at run time.
COPY fleet/ /opt/requiem/fleet/

# Provisioning entrypoint: builds a fresh per-run home from the template above,
# enforces Manual orchestration, then execs the base image's CMD.
COPY deploy/entrypoint.sh /usr/local/bin/requiem-fleet-entrypoint
RUN chmod +x /usr/local/bin/requiem-fleet-entrypoint

# Each run gets its own HERMES_HOME under here (see entrypoint). Declaring it a
# volume keeps run state out of the image layer.
ENV REQUIEM_FLEET_ROOT=/opt/requiem/fleet
VOLUME ["/var/lib/requiem/runs"]

ENTRYPOINT ["/usr/local/bin/requiem-fleet-entrypoint"]
