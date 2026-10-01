#!/usr/bin/env python3
"""List, download or delete JAIS Voice models (JSON on stdout, one record per line).

    models.py list
    models.py download <id>     # progress records, then {"event": "done"} or {"event": "error"}
    models.py delete <id>

Downloads use the default Hugging Face login when one exists; published
repositories need none. Interrupting a download keeps it resumable.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
# Progress is reported as JSON records; the library's terminal bars are noise here.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
sys.path.insert(0, str(ROOT))
from k2_runtime import model_store


def emit(record: dict) -> None:
    print(json.dumps(record, ensure_ascii=False), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    for name in ("download", "delete"):
        commands.add_parser(name).add_argument("id")
    args = parser.parse_args(argv)
    # SIGTERM from the app cancels a download like Ctrl-C; the staging directory is kept.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    base = model_store.models_dir(args.runtime_dir)
    try:
        catalog = model_store.load_catalog()
        if args.command == "list":
            emit({"event": "catalog", "catalog": str(model_store.catalog_path()),
                  "models": [model_store.state(entry, base) for entry in catalog]})
        elif args.command == "download":
            model_store.download(model_store.find(catalog, args.id), base, emit)
        else:
            model_store.delete(model_store.find(catalog, args.id), base)
            emit({"event": "deleted", "id": args.id})
        return 0
    except KeyboardInterrupt:
        emit({"event": "cancelled"})
        return 130
    except Exception as error:
        message = str(error) or type(error).__name__
        if type(error).__name__ in {"RepositoryNotFoundError", "GatedRepoError"}:
            message = "Dépôt Hugging Face inaccessible. Vérifiez le lien du catalogue ou connectez-vous avec `hf auth login`."
        emit({"event": "error", "message": message})
        return 1


if __name__ == "__main__":
    sys.exit(main())
