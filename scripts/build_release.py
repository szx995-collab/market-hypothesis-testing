"""Build reproducible wheel and sdist release candidates.

Setuptools currently preserves creation times for generated sdist members.  This
script still delegates both archive builds to the standard ``build`` frontend,
then canonicalizes only tar/gzip metadata in the generated sdist.  File contents,
names, modes, and package metadata are not changed.
"""

from __future__ import annotations

import argparse
from copy import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonicalize_sdist(path: Path, *, source_date_epoch: int) -> None:
    temporary_path = path.with_name(f".{path.name}.canonical.tmp")
    if temporary_path.exists():
        raise ValueError("canonical sdist temporary path already exists")

    try:
        with tarfile.open(path, mode="r:gz") as source:
            with temporary_path.open("xb") as raw_output:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw_output,
                    compresslevel=9,
                    mtime=source_date_epoch,
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed,
                        mode="w|",
                        format=tarfile.PAX_FORMAT,
                    ) as target:
                        for original in source.getmembers():
                            member = copy(original)
                            member.mtime = source_date_epoch
                            member.uid = 0
                            member.gid = 0
                            member.uname = ""
                            member.gname = ""
                            member.pax_headers = {}
                            content = source.extractfile(original) if original.isfile() else None
                            target.addfile(member, content)
                raw_output.flush()
                os.fsync(raw_output.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    args = parser.parse_args(argv)

    try:
        if args.source_date_epoch < 0:
            raise ValueError("source-date-epoch must not be negative")
        output_dir = args.output_dir.resolve(strict=False)
        if output_dir.exists() and any(output_dir.iterdir()):
            raise ValueError("output directory must not contain existing files")
        output_dir.mkdir(parents=True, exist_ok=True)

        environment = dict(os.environ)
        environment["SOURCE_DATE_EPOCH"] = str(args.source_date_epoch)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(output_dir),
            ],
            env=environment,
            check=True,
            timeout=300,
        )

        wheels = sorted(output_dir.glob("market_validator-*.whl"))
        sdists = sorted(output_dir.glob("market_validator-*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("build must produce exactly one wheel and one sdist")
        _canonicalize_sdist(sdists[0], source_date_epoch=args.source_date_epoch)

        artifacts = [
            {
                "bytes": path.stat().st_size,
                "filename": path.name,
                "sha256": _sha256(path),
            }
            for path in sorted((*wheels, *sdists), key=lambda item: item.name)
        ]
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps({"artifacts": artifacts, "ok": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
