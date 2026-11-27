# mwpipe

mwpipe installs a Nexus Mods collection for OpenMW. It then runs the usual post-processing tools.
Everything runs in Docker. The host needs Docker only.

> [!NOTE]
> **Claude wrote this project almost entirely.** Claude is an AI model from Anthropic. Claude wrote
> almost all of the code, the tests, the Docker files and the documentation in Claude Code
> sessions. A human gave the requirements, examined the results and merged the changes.
>
> - Model: Claude Opus 5.5 (`claude-opus-5-5`), through Claude Code
> - Development time: 2026-11-20 to 2026-11-27

```
collection URL / nxm link
  -> fetch        get collection.json from Nexus (GraphQL v2) on every run
  -> download     get the mod archives through the Nexus API v3 (Premium is necessary)
  -> install      extract to ModData/<collection-name-slug>/ and apply the FOMOD choices
  -> configure    write openmw.cfg: data=, fallback-archive=, content=, groundcover=, fallback=
  -> clean        run tes3cmd clean (optional: --clean)
  -> sort         keep the load order of the collection (use plox only when necessary)
  -> delta        run delta_plugin merge  -> Generated/delta-merged.omwaddon
  -> groundcover  run GOCoverify          -> Generated/
  -> lightfix     run Lumenaire           -> Generated/Lumenaire.omwaddon
  -> textures     report textures that meshes use and nothing provides (optional: --check-textures)
```

You can safely run every step again:

- mwpipe verifies the archives by MD5 and keeps them in a cache.
- mwpipe extracts a mod again only when its archive or its install choices change.
- mwpipe removes the generated plugins from openmw.cfg before the tools run again.

## Setup

```sh
cp .env.example .env              # set OPENMW_DATA_DIR and OPENMW_CONFIG_DIR
mkdir -p secrets && (umask 077; printf %s 'YOUR_NEXUS_API_KEY' > secrets/nexus_api_key)
```

Put the base game in `$OPENMW_DATA_DIR/Morrowind/Data Files`. Alternatively, set
`MORROWIND_DATA_FILES`. Put `Morrowind.ini` next to the `Data Files` folder.

## Usage

```sh
./mwpipe.sh run https://www.nexusmods.com/games/morrowind/collections/8euesh
./mwpipe.sh run                       # run again with the last fetched collection
./mwpipe.sh run --steps configure,sort,delta
./mwpipe.sh postprocess               # sort, delta, groundcover, lightfix only
./mwpipe.sh --dry-run run             # show the openmw.cfg that mwpipe would write
```

`mwpipe.sh` runs `docker compose run --rm --build mwpipe` with your UID and GID.
To pull a prebuilt image, set `MWPIPE_IMAGE=ghcr.io/<owner>/mwpipe:latest` in `.env`.
For all options, see `./mwpipe.sh --help` and `./mwpipe.sh run --help`.

Nexus Premium is necessary. Some mods are not hosted on Nexus. mwpipe lists these mods at the
end of the download step. To install them, do these steps:

1. Put the files in the downloads cache. The file names can be different. mwpipe matches the files by MD5.
2. Run mwpipe again.

In Docker, you can set `DOWNLOADS_DIR` in `.env` to a host directory below `$OPENMW_DATA_DIR`.
Alternatively, copy the files into the volume with `docker cp`.

### Build a collection from a mod list

The `build-collection` command changes a list of mod URLs into a draft collection on your Nexus account.

```sh
./mwpipe.sh build-collection - --name "My Morrowind list" < modlist.txt
./mwpipe.sh build-collection "$OPENMW_DATA_DIR/modlist.txt" --no-upload \
    --output-dir "$OPENMW_DATA_DIR/collection-out"                 # build and examine only
./mwpipe.sh build-collection modlist.txt --collection-id 12345    # new revision
```

`modlist.txt` has one URL on each line. A `#` starts a comment. Put the file below
`$OPENMW_DATA_DIR`, or send it on stdin.

| Line | Meaning |
| --- | --- |
| Nexus mod page or `nxm://` link | Use the primary file of the mod. To select a file, add `?file_id=N`. If you want more than one file of a mod, add one such line for each file. Put the lines in load order. |
| other `https://` URL | Direct download (version `unknown`). |
| `+ <mod url> \| 00 Core` | Install only this archive folder. Add one line for each folder. The folder name is not case-sensitive. If you add no such line, mwpipe installs all candidate folders. |
| `+ <mod url> \| Step > Group > Option` | Select an option of the FOMOD installer of the mod. The names must be the same as in `fomod/ModuleConfig.xml`. mwpipe stores the option as the `choices` of the mod. |
| `- plugin.esp` | Remove the plugin from the load order. Use this for either/or plugins. plox reports these plugins as `[CONFLICT]`. |
| `! first.esm < second.esm` | plox order rule: first.esm loads before second.esm. |

You can put `+` and `-` lines anywhere in the list.

`build-collection` writes to the log what each FOMOD and each multi-folder archive offers.
It also writes the `+` line that you can use. A FOMOD step can be hidden by an earlier choice.
Such a step appears only after you make that choice. Then you must run the command again.

The command does these steps:

1. It downloads every archive.
2. It installs the archives into a scratch setup. It does not change your `openmw.cfg`.
3. It sorts the plugins with plox.

The collection keeps the order of your list for the data directories. The load order of the
collection is the plox result. mwpipe uploads the bundle as a **draft** revision. The bundle is
`collection.json` and `loadOrder` in a `.7z` file. Publish the draft revision on the website.

mwpipe writes the inspection files to `--output-dir`. These files are `collection.json`,
`collection-data.json`, the bundle and `load-order.txt`. The default directory is in the state
volume. The host cannot browse this volume. Use a directory below `$OPENMW_DATA_DIR` instead.

Limits:

- Premium is necessary.
- The bundle must be smaller than 100 MiB.
- FOMOD installers use their defaults, unless you select options with `+` lines.
- mwpipe stores the folder selections as an `mwpipe` key in `collection.json`.
  `mwpipe run` uses these selections again. `mods.<key>.data` in `overrides.json` has priority.
  Vortex ignores the selections.
- `--collection-id N` uses the name of the collection again. If the API does not report the name,
  use `--name` one time. mwpipe then remembers the name.

mwpipe indexes the archives in the cache by mod ID and file ID. The index is `archives.json`.
If the index is lost, mwpipe adopts the files in `downloads/` by name and size.
`mwpipe dedupe-downloads [--dry-run]` removes byte-identical duplicates. It keeps the shortest name.

### Nexus APIs

| Call | API |
| --- | --- |
| collection revision and archive link | GraphQL v2 (v3 cannot read collections) |
| download links | REST v3 `mod-file-versions/{id}/download-link` (Experimental) |
| mod name, version, primary file | REST v3 `mods/{id}`, `mod-file-versions/{id}` |
| upload and create a collection | REST v3 `uploads`, `collections` |
| Premium check, download fallback | REST v1 (fallback on v3 404, 410 or 5xx) |

mwpipe verifies downloads by MD5 only. Manifests can use camelCase keys (`modId`) or snake_case
keys (`mod_id`).

### Load order

By default (`--sort auto`), mwpipe uses the load order of the collection without changes.
mwpipe runs plox only in these cases:

- The collection has no load order.
- `extra_content` adds plugins that have no position.

`--sort plox` always sorts with the mlox rules. `--sort collection` never sorts.
The generated plugins are always last.

To put a plugin in a specified position, add mlox rules as strings under `plox_rules` in
`config/overrides.json`. The rules are `[Order]`, `[NearStart]` and `[NearEnd]`. The rules apply
with `--sort plox`. They always apply in `build-collection`. `--sort auto` ignores the rules and
shows a warning. The base masters stay first, unless the load order of the collection says
differently.

## Input and secrets

| What | How (the first match is used) |
| --- | --- |
| Nexus API key | `--api-key-file`, `NEXUS_API_KEY_FILE`, Docker secret `/run/secrets/nexus_api_key`, `NEXUS_API_KEY`, `--api-key-stdin`, prompt |
| Collection | positional argument, `MWPIPE_COLLECTION`, last fetched collection |
| Paths | flags (`--data-dir`, `--openmw-cfg`, ...) have priority over environment variables (`OPENMW_DATA_DIR`, `OPENMW_CONFIG_DIR`, `OPENMW_CONFIG`, `MORROWIND_DATA_FILES`, `MODS_DIR`, `GENERATED_DIR`, `DOWNLOADS_DIR`, `MWPIPE_CACHE_DIR`, `MWPIPE_STATE_DIR`, `MWPIPE_CONFIG_DIR`) |

No flag accepts the key itself. This keeps the key out of the shell history. mwpipe sends the key
only to `api.nexusmods.com`.

The container has these security limits:

- It runs as your user. With rootful Docker, `mwpipe.sh` uses your UID and GID. With rootless
  Docker or rootless Podman, it uses container UID 0, because that UID is your host user (all
  other container UIDs are subordinate IDs from `/etc/subuid`). To use different IDs, set `PUID`
  and `PGID` in the environment.
- Its root filesystem is read-only.
- It has no capabilities.
- Only the mounted folders are writable.

Archive contents and `collection.json` are not trusted. mwpipe removes symlinks. The install stops
if one of these paths tries to leave its folder:

- an archive entry
- a FOMOD source or destination
- a replicate-mode path
- a bundled file name

The extracted size of one archive has a limit of 10 GiB. A FOMOD install has the same limit. To
change the limit, use `--extract-limit GIB` or set `MWPIPE_EXTRACT_LIMIT` in `.env`. For a moment,
an install needs this space twice: in the cache volume and in `ModData`.

The Dockerfile pins each tool: delta_plugin and plox by git commit, and GOCoverify and Lumenaire
by the SHA-256 of each zip. Renovate updates the tags and the commits. When Renovate changes the
version of GOCoverify or Lumenaire, the `renovate-hashes` workflow adds a commit with the new SHA-256
values to the Renovate branch and starts the Docker build. The workflow changes only the values of a
tool whose version changed. If the zip of an unchanged version changes, the build stops. To get the
values by hand, run `docker/fetch-gitlab-tool.sh --print-sha256 NAME VERSION`.

mwpipe follows a redirect only to HTTPS. It does not send the key again after a redirect to a
different host. mwpipe does not write a file name with a line break into `openmw.cfg`.

Outside Docker, mwpipe does not use p7zip, because p7zip can write through a symlink in an archive.
It uses bsdtar instead. The Docker image has 7-Zip, which does not have this problem.

mwpipe mounts the paths at the same location inside the container as on the host. Because of this,
the paths in `openmw.cfg` are correct for OpenMW and for the tools.

## Layout

```
$OPENMW_DATA_DIR/
  Morrowind/Data Files/        base game (you supply this)
  ModData/<collection-name-slug>/NNN Name/     one folder for each mod
  Generated/                   delta merge, groundcover, light fixes (last data= entry)
```

| | Where | Contents |
| --- | --- | --- |
| cache | volume `mwpipe_cache`, `/cache` | `downloads/`, `archives.json`, `md5-cache.json`, `plox/`, `scratch/`. You can safely delete it. |
| state | volume `mwpipe_state`, `/state` | `current.json`, `collections/`, `manifest.json`, `missing-textures.txt`, `backups/` (openmw.cfg), `builds/`. If you delete it, you must install the mods again. |
| config | `./config/overrides.json`, `./config/lumenaire.toml`, mounted read-only on `/config` | You edit these files. mwpipe never writes them. Without `lumenaire.toml`, the packaged defaults apply. |

The volumes stay after `run --rm`. `docker compose down -v` deletes them.

Outside Docker (`pip install .`), the directories are specific to the user:

- Linux: `~/.cache/mwpipe`, `~/.local/state/mwpipe`, `~/.config/mwpipe`
- macOS: `~/Library/Caches/mwpipe` and `~/Library/Application Support/mwpipe`

To change them, use `--cache-dir`, `--state-dir`, `--config-dir` or the `MWPIPE_*_DIR` variables.

### Upgrade from the first layout

The first layout used `Downloads/`, `.mwpipe/` and `mwpipe-overrides.json` in the data directory.
To upgrade, do these steps:

1. Set `DOWNLOADS_DIR` to the old `Downloads` folder.
2. Copy the overrides file to `config/`. If you edited `lumenaire.toml`, copy it also.
3. Delete `.mwpipe/`.

mwpipe extracts the mods again one time.

### openmw.cfg

mwpipe keeps all lines that it does not manage. It manages `data=`, `fallback-archive=`,
`content=` and `groundcover=`. It writes these keys as one block at the end of the file. Before it
writes the file, it makes a backup with a timestamp. If there are no `fallback=` lines, mwpipe
imports them from `Morrowind.ini`. This is the same as `openmw-iniimporter`.

## Troubleshooting

| Message or symptom | Cause | Action |
| --- | --- | --- |
| `Create .env first` | `mwpipe.sh` cannot find `.env`. | Run `cp .env.example .env` and set the two directories. |
| `No Nexus API key` | No key source has a key. | Put the key in `secrets/nexus_api_key`. Alternatively, use `--api-key-stdin`, or run with a terminal so that mwpipe can ask for the key. |
| `Nexus rejected the API key (401)` | The key is wrong or expired. | Make a new key on the Nexus website. Replace the old key. |
| `Nexus refused the request (403)` | The account is not Premium, or the file is quarantined. | Check the Premium status of the account. Check the mod page for the file. |
| `The Nexus rate limit is exceeded` | There are too many requests. | Wait. Then run the command again. The cache keeps the finished downloads. |
| `MD5 mismatch` | The download is corrupted, or the file changed upstream. | Run the command again. If the error stays, delete the file from the downloads cache. |
| `These mods are not hosted on Nexus` | The collection has mods from other sites. | Download the files by hand. Put them in the downloads cache. Run the command again. |
| `Archive entry leaves the extraction directory` | An archive tries to write outside its folder. | Do not use this archive. mwpipe stopped the install on purpose. |
| `The FOMOD installer produced no files` | The recorded choices select no files. | Examine the choices. For `build-collection`, add `+` lines. |
| `plox_rules ignored` | `--sort auto` keeps the load order of the collection. | Use `--sort plox` to apply the rules. |
| `delta_plugin` runs out of memory | The merge needs much memory. | Set `RAYON_NUM_THREADS=1`. |

To see more detail, add `-v` before the command, for example `./mwpipe.sh -v run`.

## Overrides

Use `config/overrides.json` for settings that a collection cannot express. The key of a mod is
`<modId>-<fileId>` or the name of the mod in the collection.

```json
{
  "mods": {
    "12345-67890": { "data": ["00 Core", "02 Patch for X"] },
    "Some Mod":    { "skip": true },
    "Optional Mod": { "include": true }
  },
  "fallback":      { "Fonts_Font_0": "pelagiad" },
  "groundcover":   ["Vurts Groundcover - Reeds.esp"],
  "disable":       ["Unwanted.esp"],
  "plox_rules":    ["[Order]\nDistant_Seafloor_2.00.esm\nBloodmoon.esm"],
  "extra_content": ["MyPatch.esp"],
  "extra_data":    ["/path/to/a/manually/installed/mod"]
}
```

## Tool configuration

| Setting | Effect |
| --- | --- |
| `GOCOVERIFY_ARGS`, `LUMENAIRE_ARGS` | Add flags to the tool, for example `LUMENAIRE_ARGS="--negative-lights preserve"`. |
| `--enlightened-flames` | Do the Lumenaire candle conversion. This needs the Enlightened Flames textures. |
| `RAYON_NUM_THREADS=1` | Decrease the memory use of delta_plugin. |
| `--delta-skip` | Set the record types that delta_plugin does not merge (default `Cell,Dialogue,DialogueInfo`). |
| build args `*_VERSION`, `TES3CMD_COMMIT` | Set the tool versions. Renovate maintains them. |
| build args `GOCOVERIFY_SHA256`, `LUMENAIRE_SHA256` | Pin the Go tools to an exact build. |

The light settings are in `config/lumenaire.toml`.

## Development

mwpipe is a Python 3.11+ package. It has no runtime dependencies.

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
python3 -m unittest discover -s tests       # offline
```

Code map (`src/mwpipe/`):

| File | Contents |
| --- | --- |
| `cli.py` | arguments |
| `settings.py` | paths and secrets |
| `nexus.py` | Nexus API |
| `modlist.py`, `publish.py` | change a mod list to a collection |
| `archives.py` | extraction |
| `install.py` | detection of data folders |
| `fomod.py` | FOMOD |
| `collection.py` | collection.json and mod order |
| `openmw_cfg.py` | config file |
| `tools.py` | external tools |
| `pipeline.py` | steps |

## Dependency updates and releases

The external versions are in annotated `ARG` lines in the `Dockerfile`. Renovate updates them
(`renovate.json5`, `config:best-practices`). `tes3cmd` is pinned to a `master` commit. The Alpine
packages are not pinned on purpose. The digest of the base image changes them.

`.github/workflows/docker.yml` does these tasks:

- It runs the tests.
- It builds for linux/amd64 and linux/arm64.
- On a push to `main` and on `vX.Y.Z` tags, it publishes a multi-arch image to
  `ghcr.io/<owner>/<repo>` with provenance attestation.
- On `vX.Y.Z` tags, it signs the image with [cosign](https://github.com/sigstore/cosign)
  (keyless, Sigstore). Images from `main` are not signed.

A pull request only builds the image.

To verify the signature of a release image, use this command:

```sh
cosign verify ghcr.io/<owner>/mwpipe:X.Y.Z \
  --certificate-identity https://github.com/<owner>/mwpipe/.github/workflows/docker.yml@refs/tags/vX.Y.Z \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

To verify the provenance attestation, use `gh attestation verify oci://ghcr.io/<owner>/mwpipe:X.Y.Z --repo <owner>/mwpipe`.

## License

This project is in the public domain. You can copy, change, publish, use, sell and distribute
it for all purposes, with no conditions. For the full text, see [`LICENSE`](LICENSE)
([The Unlicense](https://unlicense.org/)).
