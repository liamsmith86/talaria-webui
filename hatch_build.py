"""Precompress wheel assets once; serving needs neither an encoder nor a memory cache."""

import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import brotli
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return
        self.verify_frontend()
        self.compressed = TemporaryDirectory(prefix="talaria-assets-")
        static = Path(self.root) / "src/talaria/static"
        for source in static.rglob("*"):
            if source.suffix not in {".js", ".css", ".json", ".svg"}:
                continue
            data = source.read_bytes()
            if len(data) < 1024:
                continue
            variants = {
                ".gz": gzip.compress(data, compresslevel=9, mtime=0),
                ".br": brotli.compress(data, quality=11, mode=brotli.MODE_TEXT),
            }
            for suffix, encoded in variants.items():
                if len(encoded) >= len(data):
                    continue
                relative = source.relative_to(static).as_posix() + suffix
                destination = Path(self.compressed.name) / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(encoded)
                build_data["force_include"][str(destination)] = "talaria/static/" + relative

    def verify_frontend(self):
        root = Path(self.root)
        static = root / "src/talaria/static"
        built = static / "built"
        manifest_path = built / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("Missing production assets. Run npm ci && npm run build.")
        manifest = json.loads(manifest_path.read_text())
        if {path.name for path in built.iterdir()} != {*manifest["outputs"], "manifest.json"}:
            raise RuntimeError("Production outputs changed. Run npm run build.")
        inputs = {
            path.relative_to(root).as_posix()
            for path in static.rglob("*")
            if path.is_file() and built not in path.parents
        }
        if inputs != manifest["inputs"].keys():
            raise RuntimeError("Production inputs changed. Run npm run build.")
        for name, digest in manifest["inputs"].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"Production assets are stale ({name}). Run npm run build.")
        for name, digest in manifest["outputs"].items():
            if hashlib.sha256((built / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"Production asset is modified: {name}. Run npm run build.")

    def finalize(self, version, build_data, artifact_path):
        if version != "editable":
            self.compressed.cleanup()
