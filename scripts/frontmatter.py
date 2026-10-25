#!/usr/bin/env python3
"""Build a Hugo site from a versioned contract. Python standard library only."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class BuildError(Exception):
    """An actionable configuration, installation, or build failure."""


def object_fields(value, allowed, required, name):
    if not isinstance(value, dict):
        raise BuildError(f"{name} must be an object.")
    unknown = value.keys() - allowed
    missing = required - value.keys()
    if unknown:
        raise BuildError(f"Unknown {name} fields: {', '.join(sorted(unknown))}.")
    if missing:
        raise BuildError(f"Missing {name} fields: {', '.join(sorted(missing))}.")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BuildError(f"Duplicate configuration field: {key}.")
        result[key] = value
    return result


def confined_path(root, relative, name):
    root = root.resolve(strict=True)
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise BuildError(f"{name} must be a nonempty repository-relative path.")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise BuildError(f"{name} must stay inside the checked-out repository.")
    return path


def load_config(workspace, relative):
    path = confined_path(workspace, relative, "Configuration path")
    if path.stat().st_size > 64 * 1024:
        raise BuildError("Build configuration must be at most 64 KiB.")
    config = json.loads(path.read_text(), object_pairs_hook=unique_object)
    object_fields(config, {"schemaVersion", "hugo", "baseURL", "siteDirectory", "timeZone", "build"},
                  {"schemaVersion", "hugo", "baseURL"}, "configuration")
    if type(config["schemaVersion"]) is not int or config["schemaVersion"] != 1:
        raise BuildError("Unsupported schemaVersion; this workflow supports version 1.")
    hugo = config["hugo"]
    object_fields(hugo, {"version", "edition"}, {"version", "edition"}, "hugo")
    if not isinstance(hugo["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", hugo["version"]):
        raise BuildError("hugo.version must pin a stable release such as 0.168.0; latest and ranges are unsupported.")
    if hugo["edition"] not in ("standard", "extended"):
        raise BuildError("hugo.edition must be standard or extended.")
    url = config["baseURL"]
    if not isinstance(url, str) or any(ord(c) < 33 for c in url):
        raise BuildError("baseURL must be an absolute HTTP(S) URL without whitespace.")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BuildError("baseURL must be an absolute HTTP(S) URL without credentials, query, or fragment.")
    config["baseURL"] = url.rstrip("/") + "/"
    site = confined_path(workspace, config.get("siteDirectory", "."), "siteDirectory")
    if not site.is_dir():
        raise BuildError("siteDirectory does not exist in this revision.")
    zone = config.get("timeZone", "UTC")
    if not isinstance(zone, str):
        raise BuildError("timeZone must be an IANA time zone name.")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise BuildError(f"Unknown IANA time zone: {zone}.") from error
    options = config.get("build", {})
    object_fields(options, {"minify", "garbageCollect"}, set(), "build")
    for name, value in options.items():
        if type(value) is not bool:
            raise BuildError(f"build.{name} must be a boolean.")
    config["build"] = {"minify": options.get("minify", True), "garbageCollect": options.get("garbageCollect", True)}
    config["timeZone"] = zone
    return config, site


def download(url, destination, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "frontmatter-actions"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        total = 0
        while data := response.read(1024 * 1024):
            total += len(data)
            if total > limit:
                raise BuildError("Release download exceeds the supported size.")
            output.write(data)


def verify_checksum(archive, checksums, name):
    matches = []
    for line in checksums.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            matches.append(parts[0])
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", matches[0]):
        raise BuildError(f"Release checksum missing or ambiguous for {name}.")
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual.lower() != matches[0].lower():
        raise BuildError(f"Release checksum mismatch for {name}.")


def extract_hugo(archive, target):
    with tarfile.open(archive, "r:gz") as source:
        members = [member for member in source.getmembers() if member.name == "hugo"]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 128 * 1024 * 1024:
            raise BuildError("Release archive must contain one regular hugo binary.")
        with source.extractfile(members[0]) as binary, target.open("wb") as output:
            shutil.copyfileobj(binary, output)
    target.chmod(0o755)


def install_hugo(config, tools):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise BuildError("This release supports Linux x86_64 GitHub-hosted runners.")
    version = config["hugo"]["version"]
    prefix = "hugo_extended" if config["hugo"]["edition"] == "extended" else "hugo"
    name = f"{prefix}_{version}_linux-amd64.tar.gz"
    release = f"https://github.com/gohugoio/hugo/releases/download/v{version}"
    tools.mkdir(parents=True, exist_ok=True)
    # Always verify a fresh release; runner state is not trusted as an install cache.
    with tempfile.TemporaryDirectory(prefix="download-", dir=tools) as temporary:
        temporary = Path(temporary)
        archive = temporary / name
        checksums = temporary / "checksums.txt"
        download(f"{release}/{name}", archive, 128 * 1024 * 1024)
        download(f"{release}/hugo_{version}_checksums.txt", checksums, 1024 * 1024)
        verify_checksum(archive, checksums, name)
        binary = tools / "hugo"
        extract_hugo(archive, binary)
    return binary


def checked_version(binary, config):
    actual = subprocess.check_output([str(binary), "version"], text=True, stderr=subprocess.STDOUT).strip()
    match = re.search(r"\bhugo v([0-9]+\.[0-9]+\.[0-9]+)([^\s]*)", actual)
    if not match or match[1] != config["hugo"]["version"]:
        raise BuildError("Installed Hugo version does not match the requested release.")
    extended = "+extended" in match[2]
    if extended != (config["hugo"]["edition"] == "extended"):
        raise BuildError("Installed Hugo edition does not match the requested edition.")
    return actual


def build_command(binary, config, site, output, cache):
    command = [str(binary), "--source", str(site), "--destination", str(output),
               "--baseURL", config["baseURL"], "--cacheDir", str(cache), "--environment", "production"]
    if config["build"]["minify"]:
        command.append("--minify")
    if config["build"]["garbageCollect"]:
        command.append("--gc")
    return command


def run_build(command, site, log, environment):
    warnings = []
    diagnostics = []
    with log.open("w") as output:
        process = subprocess.Popen(command, cwd=site, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace")
        for line in process.stdout:
            # Log Hugo output as text, never as executable GitHub workflow commands.
            output.write(line)
            print("[hugo] " + line.rstrip("\n").replace("\r", "\\r"), flush=True)
            if re.match(r"\s*WARN\b", line) and len(warnings) < 100:
                warnings.append(line.strip()[:2000])
            if re.match(r"\s*(ERROR\b|Error:)", line) and len(diagnostics) < 3:
                diagnostics.append(line.strip()[:1000])
        result = process.wait()
    return result, warnings, diagnostics


def validate_output(output):
    html_found = False
    for path in output.rglob("*"):
        if path.is_symlink():
            raise BuildError("Generated site contains a symbolic link, which GitHub Pages artifacts do not support.")
        if path.is_file() and path.suffix.lower() == ".html":
            html_found = True
    if not html_found:
        raise BuildError("Hugo produced no HTML files; check the site's layouts and configuration.")


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(path)


def build(args):
    report = {
        "schemaVersion": 1, "status": "failed", "phase": "configuration",
        "source": {"repository": os.environ.get("GITHUB_REPOSITORY", ""), "commitSHA": "",
                   "headSHA": os.environ.get("FRONTMATTER_HEAD_SHA", ""), "ref": os.environ.get("GITHUB_REF", ""),
                   "implementationSHA": os.environ.get("FRONTMATTER_IMPLEMENTATION_SHA", "")},
        "hugo": {"requestedVersion": None, "edition": None, "actualVersion": None},
        "startedAt": timestamp(), "finishedAt": "", "warnings": [], "error": None,
    }
    try:
        workspace = Path(args.workspace).resolve(strict=True)
        report["source"]["commitSHA"] = subprocess.check_output(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT).strip()
        config, site = load_config(workspace, args.config)
        report["hugo"].update(requestedVersion=config["hugo"]["version"], edition=config["hugo"]["edition"])
        output = Path(args.output).resolve()
        tools = Path(args.tools).resolve()
        report_path = Path(args.report).resolve()
        # Build artifacts/tools must not overwrite any checked-out source or the report.
        for name, path in (("Output", output), ("Tools", tools)):
            if path.is_relative_to(workspace) or workspace.is_relative_to(path):
                raise BuildError(f"{name} directory must be outside the checked-out repository.")
        if output == tools or output.is_relative_to(tools) or tools.is_relative_to(output):
            raise BuildError("Output and tools directories must be separate.")
        if report_path.is_relative_to(output) or report_path.is_relative_to(workspace):
            raise BuildError("Build report must be outside the site and checked-out repository.")
        if output.exists() and any(output.iterdir()):
            raise BuildError("Output directory must be empty; refusing to mix generated files with existing content.")
        output.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report["phase"] = "installation"
        binary = install_hugo(config, tools)
        report["hugo"]["actualVersion"] = checked_version(binary, config)
        print(report["hugo"]["actualVersion"], flush=True)
        report["phase"] = "build"
        environment = dict(os.environ, TZ=config["timeZone"], HUGO_ENVIRONMENT="production")
        command = build_command(binary, config, site, output, tools / "hugo-cache")
        result, report["warnings"], diagnostics = run_build(command, site, report_path.with_suffix(".log"), environment)
        if result:
            detail = " ".join(diagnostics) or "See the build log for details."
            raise BuildError(f"Hugo exited with status {result}. {detail}")
        report["phase"] = "output"
        validate_output(output)
        report.update(status="succeeded", phase="complete")
    except (BuildError, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        report["error"] = str(error)[:4000]
        print("Frontmatter build failed: " + report["error"].replace("\n", " ").replace("\r", " "), file=sys.stderr)
    finally:
        report["finishedAt"] = timestamp()
        write_report(Path(args.report), report)
    return 0 if report["status"] == "succeeded" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build")
    command.add_argument("--workspace", required=True)
    command.add_argument("--config", default=".frontmatter/build.json")
    command.add_argument("--output", required=True)
    command.add_argument("--report", required=True)
    command.add_argument("--tools", required=True)
    args = parser.parse_args()
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
