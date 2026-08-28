# Loremaster gateway

`loremaster-gateway.py` is deployed to `~/.hermes/scripts/` on the host
running Hermes and started as a plain background process there - not as
part of this repo's Docker deployment.

It exists because the backend runs in a Linux container, while the
`hermes` CLI is a macOS-native binary tied to the host's Hermes
installation (persona, config, its own secrets). A container cannot exec
it directly, so this shim runs on the host and exposes it over a small
authenticated HTTP endpoint the backend can call across the LAN.

Stdlib-only by design, so it has no dependency surface beyond what the
hermes-agent venv already provides.
