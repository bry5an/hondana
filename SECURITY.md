# Security

## The model

Hondana is meant to run on a home network. **It has no login by default**: anyone who can reach
its port can browse, download, edit and delete books, and run the indexing pipeline. An optional
**admin password** (Settings) protects changes to the library and the server; browsing, downloads,
each reader's own state and the iPhone reader API stay open by design. Cross-site requests that
change anything are refused whether or not a password is set. Keep it on your LAN or a private VPN
(Tailscale, WireGuard); never forward its port to the internet. See
[docs/running-a-server.md](docs/running-a-server.md#security).

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/bry5an/hondana/security/advisories/new),
not in a public issue. Include what you found, how to reproduce it, and what it lets an attacker
do. You'll get a reply as soon as possible.

Fixes go into the latest release; older versions aren't patched.
