# Security policy

## Reporting a vulnerability

Email **info@getcopernican.com**, the contact published in [getcopernican.com/.well-known/security.txt](https://www.getcopernican.com/.well-known/security.txt). Please do not open a public issue or pull request for a security problem.

Include:

- the `tourclaim --version` output, your operating system and Node.js version;
- what you found and how to reproduce it;
- what an attacker could do with it.

Do not include real API keys, traveler data or claim documents in the report. Use review mode and fictional data when testing.

We will acknowledge the report, keep you informed while we fix it, and credit you if you wish.

## Scope

This policy covers this command-line client. Problems in the TourClaim API or web app (`app.getcopernican.com`) can be reported to the same address.

Examples of what we want to hear about:

- an API key printed, logged, written with loose permissions or sent anywhere other than the configured API;
- a way to make the tool share a file or email without the traveler's agreement;
- a way to make the tool sign, or appear to sign, on the traveler's behalf;
- unsafe handling of URLs, files or `.eml` input.

## Supported versions

Fixes are released for the latest published version.
