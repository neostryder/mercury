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

## Running it reliably

Run it under whatever your platform's supervised-background-process
mechanism is, not a bare `nohup ... &`, so it comes back on its own after a
crash or a reboot - a `systemd` unit on Linux, a `launchd` job on macOS, a
process manager like `pm2` or `forever` for a Node-based agent CLI. Two
things matter regardless of which one you use:

- **Restart on both crash and boot.** `systemd`'s `Restart=always` plus
  `WantedBy=multi-user.target`, or `launchd`'s `KeepAlive` plus
  `RunAtLoad`, are the equivalent knobs - the specific keys differ, the
  requirement doesn't.
- **Keep `AGENT_GATEWAY_SECRET` out of the unit file itself** if your
  supervisor can avoid it - fetch it at process start from wherever you
  already keep secrets (a secrets manager CLI, an env file read at launch)
  rather than embedding the live value in a file that outlives a rotation.
  A small wrapper script that exports the secret and then `exec`s the
  gateway process is enough for most supervisors.

The reference deployment here runs it as a `launchd` job on macOS this
way: a wrapper script pulls the secret from a secrets manager and execs
the gateway, and the `launchd` job (`RunAtLoad` + `KeepAlive`) points at
that wrapper rather than the Python script directly.
