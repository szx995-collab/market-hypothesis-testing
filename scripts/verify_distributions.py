"""Install wheel and sdist into separate offline virtual environments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import venv


def _python_path(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _console_path(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "market-validator.exe"
    return environment / "bin" / "market-validator"


def _offline_environment() -> dict[str, str]:
    allowed_names = (
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    )
    environment = {
        name: os.environ[name]
        for name in allowed_names
        if name in os.environ
    }
    environment.update(
        {
            "DEEPSEEK_API_KEY": "",
            "FRED_API_KEY": "",
            "NO_PROXY": "*",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _run(
    command: list[str],
    *,
    environment: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=180,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_one(
    *,
    label: str,
    artifact: Path,
    environment_path: Path,
    wheelhouse: Path,
    proposal: Path,
    root: Path,
) -> dict[str, object]:
    if environment_path.exists():
        raise ValueError(f"verification environment already exists: {label}")
    venv.EnvBuilder(with_pip=True, clear=False).create(environment_path)
    python = _python_path(environment_path)
    console = _console_path(environment_path)
    offline = _offline_environment()
    find_links = f"--find-links={wheelhouse}"
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            find_links,
            "pydantic>=2.12,<3",
            "tzdata>=2025.2,<2027; platform_system == 'Windows'",
            "setuptools>=77,<83",
            "wheel>=0.45,<1",
        ],
        environment=offline,
        cwd=root,
    )
    install = [
        str(python),
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
    ]
    if artifact.name.endswith(".tar.gz"):
        install.append("--no-build-isolation")
    install.append(str(artifact))
    _run(install, environment=offline, cwd=root)

    module = _run(
        [str(python), "-m", "market_validator", "doctor"],
        environment=offline,
        cwd=root,
    )
    installed = _run(
        [str(console), "doctor"],
        environment=offline,
        cwd=root,
    )
    if module.stdout != installed.stdout or module.stderr != installed.stderr:
        raise ValueError(f"module and console entry points differ for {label}")
    if json.loads(module.stdout) != {"stage": "scaffold", "status": "ok"}:
        raise ValueError(f"doctor output is invalid for {label}")

    backends_environment = dict(offline)
    backends_environment["PATH"] = ""
    backend_status = _run(
        [str(python), "-m", "market_validator", "backends"],
        environment=backends_environment,
        cwd=root,
    )
    backend_payload = json.loads(backend_status.stdout)
    if backend_payload["backends"]["deepseek_api"]["configured"] is not False:
        raise ValueError(f"AI provider unexpectedly configured for {label}")

    proposal_result = _run(
        [
            str(python),
            "-m",
            "market_validator",
            "validate-proposal",
            str(proposal),
        ],
        environment=offline,
        cwd=root,
    )
    if json.loads(proposal_result.stdout).get("ok") is not True:
        raise ValueError(f"offline proposal validation failed for {label}")
    return {
        "artifact": artifact.name,
        "artifact_sha256": _sha256(artifact),
        "doctor_entrypoints_equal": True,
        "offline_backends_command": True,
        "offline_proposal_validation": True,
    }


def _distribution_hashes(dist: Path) -> dict[str, str]:
    artifacts = sorted(
        (*dist.glob("market_validator-*.whl"), *dist.glob("market_validator-*.tar.gz")),
        key=lambda item: item.name,
    )
    if len(artifacts) != 2:
        raise ValueError("distribution directory must contain one wheel and one sdist")
    return {artifact.name: _sha256(artifact) for artifact in artifacts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--reproducible-against", type=Path)
    args = parser.parse_args(argv)
    try:
        root = Path.cwd().resolve(strict=True)
        dist = args.dist.resolve(strict=True)
        wheelhouse = args.wheelhouse.resolve(strict=True)
        proposal = args.proposal.resolve(strict=True)
        reproducible_against = (
            args.reproducible_against.resolve(strict=True)
            if args.reproducible_against is not None
            else None
        )
        work_root = args.work_root.resolve(strict=False)
        if work_root.exists():
            raise ValueError("verification work root must not already exist")
        work_root.mkdir(parents=True)
        wheels = sorted(dist.glob("market_validator-*.whl"))
        sdists = sorted(dist.glob("market_validator-*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("dist must contain exactly one wheel and one sdist")
        reproducible = None
        if reproducible_against is not None:
            if _distribution_hashes(dist) != _distribution_hashes(reproducible_against):
                raise ValueError("independent distribution builds are not byte-identical")
            reproducible = True
        results = [
            _verify_one(
                label="wheel",
                artifact=wheels[0],
                environment_path=work_root / "wheel-env",
                wheelhouse=wheelhouse,
                proposal=proposal,
                root=root,
            ),
            _verify_one(
                label="sdist",
                artifact=sdists[0],
                environment_path=work_root / "sdist-env",
                wheelhouse=wheelhouse,
                proposal=proposal,
                root=root,
            ),
        ]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {"ok": True, "reproducible_builds": reproducible, "results": results},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
