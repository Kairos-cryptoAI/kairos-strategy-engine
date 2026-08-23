# Security policy

Please report suspected vulnerabilities through a private GitHub security
advisory for this repository. Do not include credentials, signing keys, account
identifiers, or production market data in a public issue.

This package performs no network or exchange operations and must never receive
API credentials. A change that introduces network access, environment-secret
reads, wall-clock input, or nondeterministic randomness is a security-boundary
change and requires explicit review.
