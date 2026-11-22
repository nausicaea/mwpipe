# mwpipe

Installs a Nexus Mods collection for OpenMW and runs the usual post-processing, all inside Docker.
The host needs Docker and nothing else.

```
collection URL / nxm link
  -> fetch        collection.json from Nexus (GraphQL v2), on every run
  -> download     mod archives through the Nexus API v3 (requires Premium)
  -> install      extract to ModData/<collection-name-slug>/, FOMOD choices honoured
  -> configure    openmw.cfg: data=, fallback-archive=, content=, groundcover=, fallback=
  -> clean        tes3cmd clean (opt-in: --clean)
  -> sort         keep the collection's load order (plox only when needed)
  -> delta        delta_plugin merge  -> Generated/delta-merged.omwaddon
  -> groundcover  GOCoverify          -> Generated/
  -> lightfix     Lumenaire           -> Generated/Lumenaire.omwaddon
  -> textures     report textures that meshes use but nothing provides (opt-in: --check-textures)
```

Every step can be re-run safely: archives are verified by MD5 and cached, mods are only
re-extracted when their archive or install choices change, and generated plugins are removed
from openmw.cfg before the tools run again.

## Setup

```sh
cp .env.example .env              # set OPENMW_DATA_DIR and OPENMW_CONFIG_DIR
mkdir -p secrets && (umask 077; printf %s 'YOUR_NEXUS_API_KEY' > secrets/nexus_api_key)
```

The base game must be in `$OPENMW_DATA_DIR/Morrowind/Data Files` (or set
`MORROWIND_DATA_FILES`), with `Morrowind.ini` next to that folder.

## Usage

```sh
./mwpipe.sh run https://www.nexusmods.com/games/morrowind/collections/8euesh
./mwpipe.sh run                       # re-run with the last fetched collection
./mwpipe.sh run --steps configure,sort,delta
./mwpipe.sh postprocess               # sort, delta, groundcover, lightfix only
./mwpipe.sh --dry-run run             # show the openmw.cfg that would be written
```

`mwpipe.sh` wraps `docker compose run --rm --build mwpipe` with your UID/GID. Set
`MWPIPE_IMAGE=ghcr.io/<owner>/mwpipe:latest` in `.env` to pull a prebuilt image instead.
See `./mwpipe.sh --help` and `./mwpipe.sh run --help` for all options.

Nexus Premium is required. Mods hosted outside Nexus are listed at the end of the download
step; put those files in the downloads cache (any file name, matched by MD5) and run again.
In Docker, set `DOWNLOADS_DIR` in `.env` to a host directory below `$OPENMW_DATA_DIR`, or copy
files into the volume with `docker cp`.

### Building a collection from a mod list

Turns a list of mod URLs into a draft collection on your Nexus account.

```sh
./mwpipe.sh build-collection - --name "My Morrowind list" < modlist.txt
./mwpipe.sh build-collection "$OPENMW_DATA_DIR/modlist.txt" --no-upload \
    --output-dir "$OPENMW_DATA_DIR/collection-out"                 # build and inspect only
./mwpipe.sh build-collection modlist.txt --collection-id 12345    # new revision
```

`modlist.txt`: one URL per line, `#` starts a comment. The file must be below `$OPENMW_DATA_DIR`
or piped on stdin.

| Line | Meaning |
| --- | --- |
| Nexus mod page or `nxm://` link | Resolves to the mod's primary file. Add `?file_id=N` to pin a file; a mod with several wanted files needs one such line per file, in load order. |
| other `https://` URL | Direct download (version `unknown`). |
| `+ <mod url> \| 00 Core` | Install only this archive folder (one line per folder, case-insensitive). Without any, all candidate folders are installed. |
| `+ <mod url> \| Step > Group > Option` | Pick an option of the mod's FOMOD installer (names must match `fomod/ModuleConfig.xml`). Stored as the mod's `choices`. |
| `- plugin.esp` | Leave the plugin out of the load order (for either/or plugins; plox reports these as `[CONFLICT]`). |
| `! first.esm < second.esm` | plox order rule: first loads before second. |

`+` and `-` lines can go anywhere. `build-collection` logs what each FOMOD and multi-folder
archive offers, with the `+` line to use; FOMOD steps hidden by an earlier choice appear
only after that choice is made, so run again.

Every archive is downloaded, installed into a scratch setup (never your `openmw.cfg`) and sorted
with plox. The collection keeps your list order for data directories and the plox result as its
load order. The bundle (`collection.json` plus `loadOrder`, as `.7z`) is uploaded as a **draft**
revision; publish it on the website. Inspection files (`collection.json`, `collection-data.json`,
the bundle, `load-order.txt`) go to `--output-dir`; the default is inside the state volume, which
the host cannot browse, so pass a directory below `$OPENMW_DATA_DIR`.

Limits: Premium is needed; the bundle must be under 100 MiB; FOMOD installers use defaults unless
picked with `+` lines. Folder picks are stored as an `mwpipe` key in `collection.json`, so
`mwpipe run` repeats them (`mods.<key>.data` in `overrides.json` takes precedence), but Vortex
ignores them. `--collection-id N` reuses the collection's name; if the API will not report it,
pass `--name` once and mwpipe remembers it.

Archives are indexed by mod and file id in `archives.json` in the cache. If the index is lost,
files in `downloads/` are adopted by name and size. `mwpipe dedupe-downloads [--dry-run]` removes
byte-identical duplicates, keeping the shortest name.

### Nexus APIs

| Call | API |
| --- | --- |
| collection revision and archive link | GraphQL v2 (v3 cannot read collections) |
| download links | REST v3 `mod-file-versions/{id}/download-link` (Experimental) |
| mod name, version, primary file | REST v3 `mods/{id}`, `mod-file-versions/{id}` |
| upload and create a collection | REST v3 `uploads`, `collections` |
| Premium check, download fallback | REST v1 (fallback on v3 404, 410 or 5xx) |

Downloads are verified by MD5 only. Manifests accept camelCase (`modId`) or snake_case (`mod_id`) keys.

### Load order

The collection's load order is used as is (`--sort auto`). plox runs only when the collection has
no load order or `extra_content` adds plugins with no position. `--sort plox` always sorts with the
mlox rules; `--sort collection` never does. Generated plugins are always last.

To place a plugin yourself, add mlox rules (`[Order]`, `[NearStart]`, `[NearEnd]`) as strings under
`plox_rules` in `config/overrides.json`. They apply with `--sort plox` and always in
`build-collection`; `--sort auto` ignores them and warns. The base masters stay first unless the
collection's order says otherwise.

## Input and secrets

| What | How (first match wins) |
| --- | --- |
| Nexus API key | `--api-key-file`, `NEXUS_API_KEY_FILE`, Docker secret `/run/secrets/nexus_api_key`, `NEXUS_API_KEY`, `--api-key-stdin`, prompt |
| Collection | positional argument, `MWPIPE_COLLECTION`, last fetched collection |
| Paths | flags (`--data-dir`, `--openmw-cfg`, ...) over env vars (`OPENMW_DATA_DIR`, `OPENMW_CONFIG_DIR`, `OPENMW_CONFIG`, `MORROWIND_DATA_FILES`, `MODS_DIR`, `GENERATED_DIR`, `DOWNLOADS_DIR`, `MWPIPE_CACHE_DIR`, `MWPIPE_STATE_DIR`, `MWPIPE_CONFIG_DIR`) |

No flag takes the key itself, to keep it out of shell history. The key is only sent to
`api.nexusmods.com`. The container runs as your user with a read-only root filesystem, no
capabilities and only the mounted folders writable. Archive contents are untrusted: symlinks are
removed and entries escaping the extraction folder abort the install.

Paths are mounted at the same location inside the container as on the host, so the paths in
`openmw.cfg` work for both OpenMW and the tools.

## Layout

```
$OPENMW_DATA_DIR/
  Morrowind/Data Files/        base game (yours)
  ModData/<collection-name-slug>/NNN Name/     one folder per mod
  Generated/                   delta merge, groundcover, light fixes (last data= entry)
```

| | Where | Holds |
| --- | --- | --- |
| cache | volume `mwpipe_cache`, `/cache` | `downloads/`, `archives.json`, `md5-cache.json`, `plox/`, `scratch/`. Safe to delete. |
| state | volume `mwpipe_state`, `/state` | `current.json`, `collections/`, `manifest.json`, `missing-textures.txt`, `backups/` (openmw.cfg), `builds/`. Deleting it means reinstalling. |
| config | `./config/overrides.json`, `./config/lumenaire.toml`, mounted read-only on `/config` | Yours to edit; mwpipe never writes them. Without `lumenaire.toml` the packaged defaults apply. |

Volumes survive `run --rm`; `docker compose down -v` deletes them.

Outside Docker (`pip install .`) the directories are per-user: `~/.cache/mwpipe`,
`~/.local/state/mwpipe`, `~/.config/mwpipe` on Linux; `~/Library/Caches/mwpipe` and
`~/Library/Application Support/mwpipe` on macOS. Override with `--cache-dir`, `--state-dir`,
`--config-dir` or the `MWPIPE_*_DIR` variables.

Upgrading from the first layout (`Downloads/`, `.mwpipe/`, `mwpipe-overrides.json` in the data
directory): set `DOWNLOADS_DIR` to the old `Downloads` folder, copy the overrides file (and an
edited `lumenaire.toml`) to `config/`, then delete `.mwpipe/`. Mods are re-extracted once.

`openmw.cfg` keeps all lines mwpipe does not manage. `data=`, `fallback-archive=`, `content=` and
`groundcover=` are rewritten as one block at the end, after a timestamped backup. If there are no
`fallback=` lines, they are imported from `Morrowind.ini` like `openmw-iniimporter` does.

## Overrides

`config/overrides.json` covers what a collection can't express. Mods are keyed by
`<modId>-<fileId>` or by collection name.

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
| `GOCOVERIFY_ARGS`, `LUMENAIRE_ARGS` | extra flags, e.g. `LUMENAIRE_ARGS="--negative-lights preserve"` |
| `--enlightened-flames` | Lumenaire candle conversion (needs Enlightened Flames textures) |
| `RAYON_NUM_THREADS=1` | lower delta_plugin memory use |
| `--delta-skip` | record types delta_plugin skips (default `Cell,Dialogue,DialogueInfo`) |
| build args `*_VERSION`, `TES3CMD_COMMIT` | tool versions, maintained by Renovate |
| build args `GOCOVERIFY_SHA256`, `LUMENAIRE_SHA256` | pin Go tools to an exact build |

Light settings live in `config/lumenaire.toml`.

## Development

Python 3.11+ package, no runtime dependencies:

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
python3 -m unittest discover -s tests       # offline
```

Code map (`src/mwpipe/`): `cli.py` arguments, `settings.py` paths and secrets, `nexus.py` API,
`modlist.py`/`publish.py` mod list to collection, `archives.py` extraction, `install.py` data
folder detection, `fomod.py` FOMOD, `collection.py` collection.json and ordering,
`openmw_cfg.py` config file, `tools.py` external tools, `pipeline.py` steps.

## Dependency updates and releases

External versions live in annotated `ARG` lines in the `Dockerfile`, updated by Renovate
(`renovate.json5`, `config:best-practices`; `tes3cmd` is pinned to a `master` commit). Alpine
packages are deliberately unpinned; the base image digest moves them.

`.github/workflows/docker.yml` runs the tests, builds for linux/amd64 and linux/arm64, and on
pushes to `main` and `vX.Y.Z` tags publishes a multi-arch image to `ghcr.io/<owner>/<repo>` with
provenance attestation. Pull requests only build.
