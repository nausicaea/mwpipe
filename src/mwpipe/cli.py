"""Command-line interface. Run `mwpipe --help` (or `docker compose run --rm mwpipe --help`)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from mwpipe import __version__
from mwpipe.nexus import NexusClient, parse_collection_ref
from mwpipe.pipeline import ALL_STEPS, DEFAULT_STEPS, POSTPROCESS_STEPS, Pipeline
from mwpipe.publish import build_collection
from mwpipe.settings import Settings, read_api_key
from mwpipe.util import PipelineError, log


def _steps(text: str) -> list[str]:
    steps = [s.strip() for s in text.split(",") if s.strip()]
    unknown = set(steps) - set(ALL_STEPS)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown step(s): {', '.join(sorted(unknown))}")
    return steps


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mwpipe",
        description="Install a Nexus Mods collection for OpenMW and run the post-processing tools.",
        epilog="Environment variables mirror the path flags: OPENMW_DATA_DIR, OPENMW_CONFIG_DIR, "
               "OPENMW_CONFIG, MORROWIND_DATA_FILES, MODS_DIR, DOWNLOADS_DIR, GENERATED_DIR, "
               "MWPIPE_CACHE_DIR, MWPIPE_STATE_DIR, MWPIPE_CONFIG_DIR. "
               "API key: Docker secret nexus_api_key, NEXUS_API_KEY_FILE or NEXUS_API_KEY.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="show what would change without writing")

    paths = p.add_argument_group("paths (override environment variables)")
    for flag in ("data-dir", "openmw-cfg", "morrowind-data", "mods-dir", "generated-dir",
                 "cache-dir", "state-dir", "config-dir", "downloads-dir"):
        paths.add_argument(f"--{flag}", type=Path, metavar="PATH")

    auth = p.add_argument_group("Nexus authentication (never pass the key itself as an argument)")
    auth.add_argument("--api-key-file", type=Path, metavar="PATH")
    auth.add_argument("--api-key-stdin", action="store_true", help="read the key from the first stdin line")

    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="full pipeline: fetch, download, install, configure, post-process")
    run.add_argument("collection", nargs="?", default=os.environ.get("MWPIPE_COLLECTION"),
                     help="collection URL, nxm://.../collections/... link, or slug[@revision] "
                          "(default: $MWPIPE_COLLECTION, or the last fetched collection)")
    run.add_argument("--steps", type=_steps, default=None, help=f"comma list from: {','.join(ALL_STEPS)}")
    run.add_argument("--skip", type=_steps, default=[], help="steps to leave out")
    run.add_argument("--clean", action="store_true", help="also run tes3cmd clean on collection plugins")
    run.add_argument("--check-textures", action="store_true",
                     help="also report textures that meshes use but nothing provides (the textures step)")
    run.add_argument("--include-optional", action="store_true", help="install mods marked optional")
    run.add_argument("--allow-missing", action="store_true", help="continue when some archives are missing")
    run.add_argument("--lowercase-dds", action="store_true", help="rename *.DDS textures to lower case")
    run.add_argument("--sort", choices=["auto", "collection", "plox"], default="auto",
                     help="auto: keep the collection's load order, use plox only if it has none or "
                          "overrides add plugins; collection: never sort; plox: always sort "
                          "(the only mode that applies plox_rules)")
    run.add_argument("--enlightened-flames", action="store_true",
                     help="Lumenaire candle conversion (needs the Enlightened Flames textures)")
    run.add_argument("--delta-skip", default="Cell,Dialogue,DialogueInfo",
                     help="record types delta_plugin must not merge (default: %(default)s)")

    post = sub.add_parser("postprocess", help=f"only {', '.join(POSTPROCESS_STEPS)} on the existing openmw.cfg")
    post.add_argument("--sort", choices=["auto", "collection", "plox"], default="auto")
    post.add_argument("--enlightened-flames", action="store_true")

    sub.add_parser("dedupe-downloads", help="remove byte-identical archives from the downloads directory "
                                            "(honors --dry-run)")

    build = sub.add_parser("build-collection", help="turn a list of mod URLs into a Nexus collection and upload it",
                           description="Downloads the mods, sorts their plugins with plox and uploads the "
                                       "result as a draft collection on the account of the API key.")
    build.add_argument("modlist", help="file with one mod URL per line ('#' comments), or - for stdin")
    build.add_argument("--name", help="collection name, 3-36 characters (default: the mod list's file name)")
    build.add_argument("--summary", default="", help="short summary, up to 255 characters")
    build.add_argument("--description", default="")
    build.add_argument("--game", help="Nexus game domain (default: taken from the URLs)")
    build.add_argument("--collection-id", help="add a new revision to this collection (numeric id) instead of creating one; "
                            "the name is taken from that collection unless --name is given")
    build.add_argument("--no-upload", action="store_true", help="build and write the files, but do not upload")
    build.add_argument("--output-dir", type=Path, metavar="PATH",
                       help="where collection.json, the bundle and load-order.txt go (default: <state>/builds/<name>/out)")
    build.add_argument("--allow-missing", action="store_true", help="leave out mods that cannot be resolved")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname).1s %(message)s", stream=sys.stderr)
    try:
        settings = Settings.from_args(args)
        key_holder: dict[str, str] = {}
        if args.api_key_stdin:
            key_holder["key"] = read_api_key(None, from_stdin=True)

        def client() -> NexusClient:
            nexus = NexusClient(key_holder.get("key") or read_api_key(args.api_key_file, False))
            user = nexus.user()
            log.info("Nexus account: %s", user.get("name"))
            return nexus

        if args.command == "postprocess":
            Pipeline(settings, sort_mode=args.sort, enlightened_flames=args.enlightened_flames
                     ).run(POSTPROCESS_STEPS, None)
            return 0

        if args.command == "dedupe-downloads":
            merged = Pipeline(settings).dedupe_downloads()
            log.info("%d duplicate(s) %s", len(merged), "found" if settings.dry_run else "removed")
            return 0

        if args.command == "build-collection":
            if args.modlist == "-" and args.api_key_stdin:
                raise PipelineError("--api-key-stdin and a mod list on stdin both read stdin; use a key file.")
            text = sys.stdin.read() if args.modlist == "-" else Path(args.modlist).read_text(encoding="utf-8")
            name = args.name
            if not name and not args.collection_id:
                name = "collection" if args.modlist == "-" else Path(args.modlist).stem
            result = build_collection(
                settings, client(), text, name=name, summary=args.summary, description=args.description,
                game=args.game, collection_id=args.collection_id, upload=not args.no_upload,
                output_dir=args.output_dir, allow_missing=args.allow_missing)
            log.info("%s", json.dumps(result, indent=2))
            return 0

        steps = args.steps or (
            DEFAULT_STEPS + (["clean"] if args.clean else []) + (["textures"] if args.check_textures else []))
        steps = [s for s in steps if s not in args.skip]
        ref = parse_collection_ref(args.collection) if args.collection else None
        if ref is None and "fetch" in steps:
            steps.remove("fetch")  # reuse the collection fetched last time
        pipeline = Pipeline(
            settings, client_factory=client, include_optional=args.include_optional,
            allow_missing=args.allow_missing, lowercase_dds=args.lowercase_dds, sort_mode=args.sort,
            delta_skip=[s for s in args.delta_skip.split(",") if s],
            enlightened_flames=args.enlightened_flames,
        )
        pipeline.run(steps, ref)
        log.info("done")
        return 0
    except PipelineError as err:
        log.error("%s", err)
        return 1
    except KeyboardInterrupt:
        return 130
