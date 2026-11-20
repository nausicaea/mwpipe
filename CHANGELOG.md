## 1.1.0 (2026-11-20)

### Feat

- **build-collection**: convert mod list to Nexus collection
- initial commit

### Fix

- **collection**: take the collection name from --collection-id; fix name clobbered by disabled plugins
- **download**: reuse downloaded archives across collections and merge duplicates
- **config**: restore file-based configs and let mod lists disable either/or plugins
- **docker-compose**: remove the unsupported mode parameter from the config entries
- **nexus**: percent-encode CDN download URLs and report download failures cleanly

### Refactor

- **config**: inline the config files for overrides and lumenaire into the docker compose file
- **state**: use named volumes for cache and state, compose configs for settings
- **state**: separate mwpipe's cache, state and config from OpenMW's data directory
