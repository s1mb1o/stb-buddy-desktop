# Security policy

## Trust model

STB Buddy Desktop is a development tool for a physically controlled device
and a trusted host. It does not implement authentication, authorization, or
TLS.

Anyone who can reach the HTTP server can:

- read the full retained serial-console history;
- send arbitrary bytes and commands to the attached board;
- send a serial BREAK;
- clear the current retained history;
- initiate and retrieve board file downloads.

The service binds to `127.0.0.1` by default. Keep that default unless remote
access is required. If you use `--host 0.0.0.0`, restrict access with host and
network firewalls, use only a trusted network, and never forward the port to
the public Internet.

The temporary `nc` listener used for large downloads accepts the first
connection that reaches its ephemeral port. A SHA-256 comparison rejects
incorrect content, but it does not authenticate the connecting peer.

## Reporting a vulnerability

Please report vulnerabilities through GitHub's private security advisory
feature for this repository. Include the affected version, reproduction steps,
impact, and any suggested mitigation. Do not open a public issue for an
unpatched vulnerability.

## Supported versions

Security fixes are applied to the latest release. Earlier releases are not
maintained as separate support branches.
