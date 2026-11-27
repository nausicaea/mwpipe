## v1.3.1 (2026-11-27)

### Fix

- **docker**: use container root with a rootless runtime (#31)

## v1.3.0 (2026-11-27)

### Feat

- **docker**: sign release images with cosign (#30)

## v1.2.0 (2026-11-27)

### Feat

- **docker**: add OCI image labels to Docker builds (#28)
- **modlist**: rework the disposition and persuasion system (#24)
- **cli**: make the texture presence check opt-in via --check-textures (#21)
- **build-collection**: pick data folders and FOMOD options with '+' lines (#6)
- report textures that meshes use but no mod or BSA provides (#9)

### Fix

- **docker**: OCI version label from git describe; v prefix for release tags (#29)
- **security**: contain untrusted paths, limit extraction size, pin tool builds (#25)
- **overrides**: empty the overrides file again (#17)
- lowercase only the .dds extension and never overwrite a file (#8)
- give build-collection its own ModData instead of the real one (#13)
- download the collection archive on every run (#11)
- normalise leading slashes, trailing dots and absolute paths in the texture scan (#10)
- **overrides**: specify a load order rule for distant sea floor
- **overrides**: select the correct mod variants
- skip plugins with missing masters and Lua plugins in tes3cmd clean (#3)

### Refactor

- name the ModData subdirectory after the collection name instead of its Nexus slug (#2)

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
