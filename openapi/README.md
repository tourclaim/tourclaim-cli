# Schema snapshot

`connectors-v1.json` is a copy of the full TourClaim connector schema the CLI uses, served at `/api/connectors/v1/openapi-cli.json`. It includes the drafts list (`list_claim_drafts`) and the key endpoints (`get_connection`, `disconnect`).

`/api/connectors/v1/openapi.json` serves the same schema without those three operations: the ten operations assistants load as tools.

Both editions' types, field tables and test mocks are checked against this file. [RELEASING.md](../RELEASING.md) says when and how to refresh it.
