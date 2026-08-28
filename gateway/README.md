# Agent gateway

`agent_gateway.py` runs natively on whatever host your agent already runs
on, started as a plain background process there - not as part of this
repo's Docker deployment.

It exists because Mercury's backend commonly runs in a container (a
different OS, without your agent's own credentials or configuration), while
most agent CLIs are tied to the host they are installed on: they expect a
particular OS, a particular config directory, a particular auth setup. A
container cannot exec that CLI directly, so this shim runs on the host
where the CLI already works and exposes it over a small authenticated HTTP
endpoint the backend can call across the network.

Stdlib-only by design, so it has no dependency surface beyond whatever your
agent CLI already needs.

## Configuring it for your agent

Set `AGENT_CHAT_COMMAND` to whatever single-shot, non-interactive command
reads a prompt on stdin and prints the full reply on stdout. Whatever
credentials, config, or persona selection your agent needs should already
be set up in the environment this process runs in - the gateway does not
manage any of that itself.

Set `AGENT_GATEWAY_SECRET` to a random shared secret; the backend must be
configured with the same value. This endpoint is meant for a private
network - it has no other authentication.

Run it as whatever your platform's supervised-background-process mechanism
is (a systemd unit, a launchd job, a supervisor process) so it restarts if
it crashes or the host reboots. The reference deployment here just uses
`nohup ... &` while validating shadow mode; that is not durable and should
be replaced before relying on Mercury long-term.
