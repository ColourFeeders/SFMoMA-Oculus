#!/usr/bin/env python3
import argparse
import ipaddress
import json
from pathlib import Path
import re
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = {"mac_ip": "192.0.2.135", "windows_ip": "192.0.2.136", "pikvm_ip": "192.0.2.141"}
TEXT_SUFFIXES = {".py", ".ps1", ".cs", ".command", ".sh", ".cmd", ".json", ".txt", ".md", ".html", ".css", ".js"}


def render(config, destination, allow_examples=False):
    if set(config) != set(EXAMPLES):
        raise ValueError("Configuration must contain only mac_ip, windows_ip and pikvm_ip.")
    replacements = {}
    for key, example in EXAMPLES.items():
        address = ipaddress.IPv4Address(config[key])
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            raise ValueError(f"{key} must be a unicast site address.")
        if address in ipaddress.IPv4Network("192.0.2.0/24") and not allow_examples:
            raise ValueError("Replace documentation addresses with site addresses before rendering.")
        replacements[example.encode()] = str(address).encode()
    if len(set(replacements.values())) != 3:
        raise ValueError("Mac, Windows and PiKVM addresses must be different.")
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing directory: {destination}")
    if destination == ROOT or destination in ROOT.parents:
        raise ValueError("Output cannot be the repository or an ancestor.")
    if destination.is_relative_to(ROOT) and destination != ROOT / "deploy":
        raise ValueError("Inside the repository, output must be the ignored deploy/ directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".oculus-deploy-", dir=destination.parent))
    try:
        manifest = json.loads((ROOT / "docs/source-manifest.json").read_text(encoding="utf-8"))
        diagnostics = Path("tools/diagnostics")
        addresses = b"|".join(re.escape(address) for address in replacements)
        pattern = re.compile(rb"(?<![\d.])(?:" + addresses + rb")(?!\d|\.\d)")
        for row in manifest["files"]:
            relative = Path(row["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Invalid deployment source: {relative}")
            if relative.is_relative_to(diagnostics):
                target = stage / "diagnostics" / relative.relative_to(diagnostics)
            elif relative.parts and relative.parts[0] in {"packages", "touchdesigner"}:
                target = stage / relative
            else:
                continue
            source = ROOT / relative
            if any((ROOT / part).is_symlink() for part in (relative, *relative.parents)):
                raise ValueError(f"Linked deployment source: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix.lower() in TEXT_SUFFIXES:
                target.write_bytes(pattern.sub(lambda match: replacements[match.group()], source.read_bytes()))
                shutil.copystat(source, target)
            else:
                shutil.copy2(source, target)
        stage.rename(destination)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description="Render a local deployment copy with site IPv4 addresses.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "deploy")
    parser.add_argument("--allow-example-addresses", action="store_true", help="Only for packaging tests; output remains non-operational.")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    print(render(config, args.output, args.allow_example_addresses))


if __name__ == "__main__":
    main()
