"""Export the bundled plugin for installation on the machine running Hermes."""

import argparse
import shutil
from pathlib import Path


def main(argv):
    parser = argparse.ArgumentParser(description="Install Talaria's optional Hermes plugin files")
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home() / ".hermes",
        help="Hermes home on this machine (default: ~/.hermes)",
    )
    args = parser.parse_args(argv)
    target = args.home.expanduser().resolve() / "plugins" / "talaria"
    source = Path(__file__).parent / "hermes_plugin"
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Only the packaged plugin files; no Talaria dependencies enter Hermes's environment.
    for file in source.iterdir():
        if file.is_file() and file.suffix in {".py", ".yaml"}:
            staged = target / f".{file.name}.new"
            shutil.copyfile(file, staged)
            staged.chmod(0o600)
            staged.replace(target / file.name)
    print(f"Plugin installed in {target}")
    print(
        "Enable it with `hermes plugins enable talaria` in this Hermes profile, "
        "then restart the gateway."
    )
    print(
        "For a multiplexed gateway, install and enable it in the gateway's primary profile as well."
    )
