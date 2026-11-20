"""Command-line interface. Run `mwpipe --help` (or `docker compose run --rm mwpipe --help`)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from mwpipe import __version__
from mwpipe.nexus import NexusClient, parse_collection_ref
from mwpipe.pipeline import ALL_STEPS, DEFAULT_STEPS, POSTPROCESS_STEPS, Pipeline
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
               "OPENMW_CONFIG, MORROWIND_DATA_FILES, MODS_DIR, DOWNLOADS_DIR, GENERATED_DIR. "
               "API key: Docker secret nexus_api_key, NEXUS_API_KEY_FILE or NEXUS_API_KEY.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="show what would change without writing")

    paths = p.add_argument_group("paths (override environment variables)")
    for flag in ("data-dir", "openmw-cfg", "morrowind-data", "mods-dir", "downloads-dir", "generated-dir"):
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
    run.add_argument("--include-optional", action="store_true", help="install mods marked optional")
    run.add_argument("--allow-missing", action="store_true", help="continue when some archives are missing")
    run.add_argument("--lowercase-dds", action="store_true", help="rename *.DDS textures to lower case")
    run.add_argument("--sort", choices=["auto", "collection", "plox"], default="auto",
                     help="auto: keep the collection's load order, use plox only if it has none or "
                          "overrides add plugins; collection: never sort; plox: always sort")
    run.add_argument("--enlightened-flames", action="store_true",
                     help="Lumenaire candle conversion (needs the Enlightened Flames textures)")
    run.add_argument("--delta-skip", default="Cell,Dialogue,DialogueInfo",
                     help="record types delta_plugin must not merge (default: %(default)s)")

    post = sub.add_parser("postprocess", help=f"only {', '.join(POSTPROCESS_STEPS)} on the existing openmw.cfg")
    post.add_argument("--sort", choices=["auto", "collection", "plox"], default="auto")
    post.add_argument("--enlightened-flames", action="store_true")
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

        steps = args.steps or DEFAULT_STEPS + (["clean"] if args.clean else [])
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
