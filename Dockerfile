# unseenet-gateway in a container: the gateway, its adapters and Tor, on the
# node image (core, chiavdf, Tor, tini). Config, keys and state in /data.
#
#   docker run --rm -v gateway:/data ghcr.io/unseenet/gateway      # writes an example config
#   docker run --rm -v gateway:/data ghcr.io/unseenet/gateway keys
#   docker run -d --name gateway -v gateway:/data ghcr.io/unseenet/gateway
#
# See docs/docker.md and compose.yaml.
ARG NODE_IMAGE=ghcr.io/unseenet/node:latest

FROM ${NODE_IMAGE} AS build
USER root
RUN apk add --no-cache gcc musl-dev python3-dev libffi-dev
COPY pyproject.toml README.md LICENSE NOTICE gateway.example.toml /src/
COPY src /src/src
# not [fast]: coincurve has no musl wheels for this Python, and Nostr signs without it
RUN /opt/unseenet/bin/pip install --no-cache-dir /src \
 && find /opt/unseenet -name __pycache__ -type d -prune -exec rm -rf {} +

FROM ${NODE_IMAGE}
USER root
COPY --from=build /opt/unseenet /opt/unseenet
COPY gateway.example.toml /opt/unseenet-gateway/gateway.example.toml
COPY docker/entrypoint.sh /usr/local/bin/unseenet-gateway-entrypoint
RUN chmod 755 /usr/local/bin/unseenet-gateway-entrypoint
ENV UNSEENET_DATA=/data
VOLUME /data
HEALTHCHECK NONE
LABEL org.opencontainers.image.title="Unseenet gateway" \
      org.opencontainers.image.description="A gateway between Unseenet and other networks: Usenet, Nostr, Matrix" \
      org.opencontainers.image.source="https://github.com/unseenet/unseenet-gateway" \
      org.opencontainers.image.url="https://unseenet.org/gateways/" \
      org.opencontainers.image.licenses="Apache-2.0"
ENTRYPOINT ["/sbin/tini", "--", "unseenet-gateway-entrypoint"]
CMD []
