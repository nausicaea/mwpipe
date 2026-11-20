# mwpipe

Installs a Nexus Mods collection for OpenMW and runs the usual post-processing, all inside Docker.
The host needs Docker and nothing else.

```
collection URL / nxm link
  -> fetch        collection.json from Nexus (GraphQL v2, see "Nexus APIs")
  -> download     mod archives through the Nexus API v3 (requires Premium)
  -> install      extract to ModData/<slug>/, FOMOD choices and "replicate" mode honoured
  -> configure    openmw.cfg: data=, fallback-archive=, content=, groundcover=, fallback=
  -> clean        tes3cmd clean (opt-in: --clean)
  -> sort         keep the collection's load order (plox only when needed, see below)
  -> delta        delta_plugin merge  -> Generated/delta-merged.omwaddon
  -> groundcover  GOCoverify          -> Generated/deleted_groundcover.omwaddon, groundcover.omwaddon, meshes/
  -> lightfix     Lumenaire           -> Generated/Lumenaire.omwaddon
```

Every step can be re-run safely: archives are verified by MD5 and cached, mods are only
re-extracted when their archive or install choices change, and generated plugins are removed
from openmw.cfg before the tools run again, so the tools never merge their own output.

## Setup

```sh
cp .env.example .env              # set OPENMW_DATA_DIR and OPENMW_CONFIG_DIR
mkdir -p secrets && (umask 077; printf %s 'YOUR_NEXUS_API_KEY' > secrets/nexus_api_key)
```

The base game must already be in `$OPENMW_DATA_DIR/Morrowind/Data Files` (or set
`MORROWIND_DATA_FILES`), with `Morrowind.ini` next to that folder.

## Usage

```sh
./mwpipe.sh run https://www.nexusmods.com/games/morrowind/collections/8euesh
./mwpipe.sh run nxm://morrowind/collections/8euesh/revisions/1
./mwpipe.sh run                       # re-run with the last fetched collection
./mwpipe.sh run --steps configure,sort,delta
./mwpipe.sh postprocess               # sort, delta, groundcover, lightfix only (the old entrypoint)
./mwpipe.sh --dry-run run             # show the openmw.cfg that would be written
```

`mwpipe.sh` is `docker compose run --rm --build mwpipe` with your UID/GID; you can call
compose directly. To skip the local build, set `MWPIPE_IMAGE=ghcr.io/<owner>/mwpipe:latest`
in `.env`: the script then pulls that image instead. Run `./mwpipe.sh --help` and `./mwpipe.sh run --help` for every option.

A Nexus Premium account is required: the API only issues download links to Premium accounts.
Mods hosted outside Nexus are listed at the end of the download step; put those files into
the downloads cache (see Layout; any file name works, they are matched by MD5) and run again.
In Docker the cache is a volume, so either set `DOWNLOADS_DIR` in `.env` to a host directory (below
`$OPENMW_DATA_DIR`) or copy the file in with `docker run`/`docker cp`.

### Building a collection from a mod list

The reverse direction: turn a list of mod URLs into a collection on your own Nexus account.

```sh
./mwpipe.sh build-collection - --name "My Morrowind list" < modlist.txt
./mwpipe.sh build-collection "$OPENMW_DATA_DIR/modlist.txt" --no-upload \
    --output-dir "$OPENMW_DATA_DIR/collection-out"                          # build and inspect only
./mwpipe.sh build-collection modlist.txt --collection-id 12345             # new revision of a collection
```

`modlist.txt` has one URL per line, `#` starts a comment. Nexus mod pages (`/morrowind/mods/43931`,
optionally `?file_id=N` to pin a file) and `nxm://.../mods/ID/files/ID` links resolve through the
v3 API to the mod's primary file. Any other `https://` URL becomes a "direct" download, which
the collection installer fetches from that URL.
A line starting with `-` names a plugin to leave out of the load order, for mods that ship either/or
plugins. plox prints a `[CONFLICT] use only one of these plugins` for them; add `- k_weather (louder
sounds).esp` to the list, run again, and the conflict is gone from the sort and from the collection
(the plugin is stored as disabled, which `mwpipe run` honors). The mod list file must be somewhere the container
can see (below `$OPENMW_DATA_DIR`), or be piped in on stdin as above.

What happens: every archive is downloaded (cached by MD5), installed into a scratch setup that never
touches your `openmw.cfg`, and sorted with plox. The collection keeps your list order for the data
directories (later entries override earlier ones) and the plox result as its load order. The bundle
(`collection.json` with `loadOrder`, packed as `.7z`) is uploaded and a collection is created from it
as a **draft** revision; publish it on the website. Files for inspection land in
`--output-dir` (default: `builds/<name>/out/` in the state volume, which the host cannot browse, so pass
`--output-dir` below `$OPENMW_DATA_DIR` to look at them): `collection.json`, `collection-data.json` (the API
request body), the bundle and `load-order.txt`.

Limits: Premium is needed (the mods are downloaded), the bundle must be under 100 MiB, FOMOD
installers run with their default choices, and mods with no plugins at all are included as they are.
An archive with several alternative data folders ("adding all" warnings in the log) is installed with all
of them, later ones overriding earlier ones; the v3 manifest has no field for picking one.
Direct downloads get the version `unknown`, since a bare URL carries none.

### Nexus APIs

| Call | API | Why |
| --- | --- | --- |
| collection revision and its archive link | GraphQL v2 | v3 has no endpoint that reads collections, only create and edit |
| mod file download links | REST v3 `mod-file-versions/{id}/download-link` | documented, but badged Experimental |
| mod name, version and primary file | REST v3 `mods/{id}`, `mod-file-versions/{id}` | used by `build-collection` |
| upload and create a collection | REST v3 `uploads`, `collections` | used by `build-collection` |
| Premium check, and download fallback | REST v1 | v1 is used when the v3 link call returns 404, 410 or 5xx |

Downloads are verified by MD5 only. Collection manifests are accepted with either camelCase keys
(`modId`, `fileId`) or the snake_case keys of the v3 upload spec (`mod_id`, `file_id`).

### Load order

The collection's load order is what its author tested, so it is used as is (`--sort auto`).
plox only runs when the collection has no load order or `extra_content` adds plugins with no
position of their own. `--sort plox` always sorts with the mlox rules; `--sort collection` never does.
Generated plugins are always appended last.

## Input and secrets

| What | How (first match wins) |
| --- | --- |
| Nexus API key | `--api-key-file`, `NEXUS_API_KEY_FILE`, Docker secret `/run/secrets/nexus_api_key`, `NEXUS_API_KEY`, `--api-key-stdin`, interactive prompt |
| Collection | positional argument, `MWPIPE_COLLECTION`, last fetched collection |
| Paths | flags (`--data-dir`, `--openmw-cfg`, ...) over env vars (`OPENMW_DATA_DIR`, `OPENMW_CONFIG_DIR`, `OPENMW_CONFIG`, `MORROWIND_DATA_FILES`, `MODS_DIR`, `GENERATED_DIR`, `MWPIPE_CACHE_DIR`, `MWPIPE_STATE_DIR`, `MWPIPE_CONFIG_DIR`, `DOWNLOADS_DIR`) |

There is deliberately no flag that takes the key itself, since command lines end up in shell
history and process lists. The key is only sent to `api.nexusmods.com`, never to download mirrors.
The container runs as your user with a read-only root filesystem, no capabilities and only the
mounted folders (OpenMW's two, plus the cache and state volumes) writable, and the config files read-only. Archive contents are treated as untrusted: symlinks are removed
and entries that escape the extraction folder abort the install.

Paths are mounted at the same location inside the container as on the host, so the paths written
into `openmw.cfg` work for both OpenMW and the tools. That replaces the old hardcoded
`/Users/kitsune/...` symlink.

## Layout

OpenMW's files and mwpipe's own files are kept apart. OpenMW's, because `openmw.cfg` points at them:

```
$OPENMW_DATA_DIR/
  Morrowind/Data Files/        base game (yours)
  ModData/<slug>/NNN Name/     one folder per mod (several if the archive has several roots)
  Generated/                   delta merge, groundcover, light fixes (last data= entry)
```

mwpipe's own files, in Docker:

| | Where | Holds |
| --- | --- | --- |
| cache | named volume `mwpipe_cache`, `/cache` | `downloads/` (archive cache), `md5-cache.json`, `plox/` (mlox rules), `scratch/`. Safe to delete. |
| state | named volume `mwpipe_state`, `/state` | `current.json`, `collections/`, `manifest.json`, `backups/` (openmw.cfg), `builds/`. Deleting it means reinstalling. |
| config | `./config/overrides.json` and `./config/lumenaire.toml`, mounted read-only on `/config` by compose's `configs:` | Yours to edit; mwpipe never writes them. Without a `lumenaire.toml` the packaged defaults are used. |

The volumes survive `run --rm`; `docker compose down -v` deletes them, `docker volume ls` shows them.
`DOWNLOADS_DIR` in `.env` moves just the archives to a host directory.

Outside Docker (`pip install .`) the same three directories are per-user: XDG directories on Linux
(`~/.cache/mwpipe`, `~/.local/state/mwpipe`, `~/.config/mwpipe`), and `~/Library/Caches/mwpipe` and
`~/Library/Application Support/mwpipe` on macOS. Override with `--cache-dir`, `--state-dir`, `--config-dir`
or `MWPIPE_CACHE_DIR`, `MWPIPE_STATE_DIR`, `MWPIPE_CONFIG_DIR`.

Upgrading from the first layout (`Downloads/`, `.mwpipe/` and `mwpipe-overrides.json` in the data
directory): set `DOWNLOADS_DIR` in `.env` to the old `Downloads` folder, copy the overrides file to
`config/overrides.json` and your `lumenaire.toml`, if you edited it, to `config/`, then delete `.mwpipe/`.
The installed mods are re-extracted once.

`openmw.cfg` keeps all lines mwpipe does not manage (`encoding`, `fallback=`, comments, ...).
`data=`, `fallback-archive=`, `content=` and `groundcover=` are rewritten as one block at the end.
A timestamped backup goes to `backups/` in the state volume before every write. If the file has no
`fallback=` lines, they are imported from `Morrowind.ini` the same way `openmw-iniimporter` does.

## Overrides

`config/overrides.json` covers what a collection can't express. Mods are keyed by
`<modId>-<fileId>` or by their name in the collection.

```json
{
  "mods": {
    "12345-67890": { "data": ["00 Core", "02 Patch for X"] },
    "Some Mod":    { "skip": true },
    "Optional Mod": { "include": true }
  },
  "fallback":      { "Fonts_Font_0": "pelagiad", "Fonts_Font_2": "OMWAyembedt" },
  "groundcover":   ["Vurts Groundcover - Reeds.esp"],
  "disable":       ["Unwanted.esp"],
  "extra_content": ["MyPatch.esp"],
  "extra_data":    ["/path/to/a/manually/installed/mod"]
}
```

## Tool configuration

| Variable | Effect |
| --- | --- |
| `GOCOVERIFY_ARGS`, `LUMENAIRE_ARGS` | extra flags, e.g. `LUMENAIRE_ARGS="--negative-lights preserve"` |
| `--enlightened-flames` | Lumenaire candle conversion (needs the Enlightened Flames textures) |
| `RAYON_NUM_THREADS=1` | lower delta_plugin's memory use on large load orders |
| `--delta-skip` | record types delta_plugin skips (default `Cell,Dialogue,DialogueInfo`, as MOMW recommends) |
| build args `DELTA_PLUGIN_VERSION`, `PLOX_VERSION`, `GOCOVERIFY_VERSION`, `LUMENAIRE_VERSION`, `TES3CMD_COMMIT` | tool versions, maintained by Renovate |
| build args `GOCOVERIFY_SHA256`, `LUMENAIRE_SHA256` | pin the Go tools to an exact build (default: verify against the published checksum) |

Light settings come from `config/lumenaire.toml`, which starts as a copy of the
bundled defaults (your former waza values: flicker off, radius 1.2 / 1.1).

## Development

mwpipe is a regular Python package (`pyproject.toml`, `src/mwpipe/`, no runtime dependencies,
Python 3.11 or newer). The image installs it with `pip install`; outside Docker you can do the same
for development:

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
python3 -m unittest discover -s tests       # offline, needs neither Docker nor network
```

Code map (`src/mwpipe/`): `cli.py` arguments, `settings.py` paths and secrets, `nexus.py` API,
`modlist.py` and `publish.py` mod list to collection, `archives.py` extraction, `install.py` data folder detection, `fomod.py` FOMOD installer,
`collection.py` collection.json and mod ordering, `openmw_cfg.py` config file, `tools.py`
external tools, `pipeline.py` the steps.

## Dependency updates and releases

All external versions live in annotated `ARG` lines in the `Dockerfile`:

```dockerfile
# renovate: datasource=github-tags depName=rfuzzo/plox
ARG PLOX_VERSION=v0.5.0
```

`renovate.json5` extends `config:best-practices` (digest pinning for base images, SHA pinning for
GitHub Actions) and adds two custom managers: one for the annotated version `ARG`s, one for
`tes3cmd`, which has no releases and is pinned to a commit on `master`. The Python base image
and `.python-version` are grouped into one PR. Alpine packages are not pinned on purpose: Alpine
removes old package versions, so pins break builds; the base image digest moves them instead.
Image digests (`@sha256:...`) are added by Renovate's first pin PR.

`.github/workflows/docker.yml` runs the tests, builds natively for linux/amd64 and linux/arm64,
and on pushes to `main` and `vX.Y.Z` tags publishes a multi-arch image to
`ghcr.io/<owner>/<repo>` with a build provenance attestation. Pull requests only build.
The arm64 job uses GitHub's `ubuntu-24.04-arm` runner, which is free for public repositories only.
