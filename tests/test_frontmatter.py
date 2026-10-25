import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("frontmatter", Path(__file__).parents[1] / "scripts/frontmatter.py")
fm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fm)


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "site"
        self.workspace.mkdir()
        self.config = {"schemaVersion": 1, "hugo": {"version": "0.147.2", "edition": "extended"},
                       "baseURL": "https://example.com/blog/"}
        self.config_path = self.workspace / "build.json"
        self.save_config()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config))

    def load(self):
        self.save_config()
        return fm.load_config(self.workspace, "build.json")


class ConfigurationTests(WorkspaceTest):
    def test_project_base_path_and_timezone_are_preserved(self):
        self.config.update(timeZone="America/New_York", siteDirectory="nested")
        (self.workspace / "nested").mkdir()
        config, site = self.load()
        self.assertEqual(config["baseURL"], "https://example.com/blog/")
        self.assertEqual(config["timeZone"], "America/New_York")
        self.assertEqual(site, self.workspace / "nested")
        self.assertEqual(config["build"], {"minify": True, "garbageCollect": True})

    def test_config_cannot_escape_checkout_by_path_or_symlink(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.config))
        (self.workspace / "linked.json").symlink_to(outside)
        for path in ("../outside.json", "linked.json", str(outside)):
            with self.subTest(path=path), self.assertRaises(fm.BuildError):
                fm.load_config(self.workspace, path)

    def test_site_directory_cannot_escape_checkout(self):
        (self.workspace / "escape").symlink_to(self.root, target_is_directory=True)
        for directory in ("../", "escape", "/", "missing", 42):
            self.config["siteDirectory"] = directory
            with self.subTest(directory=directory), self.assertRaises(fm.BuildError):
                self.load()

    def test_stable_versions_only(self):
        for version in ("latest", "v0.147.2", "0.147", "0.147.2-rc1", "0.147.2\n", "$(env)", True):
            self.config["hugo"]["version"] = version
            with self.subTest(version=version), self.assertRaises(fm.BuildError):
                self.load()

    def test_schema_changes_fail_with_actionable_error(self):
        for value in (2, True, "1", None):
            self.config["schemaVersion"] = value
            with self.subTest(value=value), self.assertRaisesRegex(fm.BuildError, "schemaVersion"):
                self.load()

    def test_unknown_and_duplicate_fields_are_rejected(self):
        self.config["buildCommand"] = "arbitrary shell"
        with self.assertRaisesRegex(fm.BuildError, "Unknown"):
            self.load()
        self.config_path.write_text('{"schemaVersion":1,"schemaVersion":2}')
        with self.assertRaisesRegex(fm.BuildError, "Duplicate"):
            fm.load_config(self.workspace, "build.json")

    def test_malformed_options_and_timezone_are_rejected(self):
        for key, value in (("timeZone", "Not/AZone"), ("timeZone", "../UTC"), ("build", {"minify": "false"}),
                           ("build", {"garbageCollect": 1}), ("build", [])):
            config = dict(self.config)
            self.config[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(fm.BuildError):
                self.load()
            self.config = config

    def test_urls_cannot_embed_credentials_or_parameters(self):
        for url in ("javascript:alert(1)", "https://user:password@example.com", "https://example.com/?x=1",
                    "https://example.com/#fragment", "https://example.com/\nnext", "//example.com", "https://"):
            self.config["baseURL"] = url
            with self.subTest(url=url), self.assertRaises(fm.BuildError):
                self.load()

    def test_configuration_size_is_bounded(self):
        self.config_path.write_text(" " * (64 * 1024 + 1))
        with self.assertRaisesRegex(fm.BuildError, "64 KiB"):
            fm.load_config(self.workspace, "build.json")


class ReleaseTests(WorkspaceTest):
    def test_checksum_required_and_must_match_exact_asset(self):
        archive, checksums = self.root / "release.tgz", self.root / "checksums.txt"
        archive.write_bytes(b"binary")
        digest = hashlib.sha256(b"binary").hexdigest()
        checksums.write_text(f"{digest}  release.tgz\n")
        fm.verify_checksum(archive, checksums, "release.tgz")
        for text in (f"{digest}  another.tgz\n", f"{'0' * 64}  release.tgz\n",
                     f"{digest}  release.tgz\n{digest}  release.tgz\n", "invalid  release.tgz\n"):
            checksums.write_text(text)
            with self.subTest(text=text), self.assertRaises(fm.BuildError):
                fm.verify_checksum(archive, checksums, "release.tgz")

    def test_archive_extracts_only_regular_root_binary(self):
        archive = self.root / "archive.tgz"
        with tarfile.open(archive, "w:gz") as output:
            for name in ("../escape", "hugo"):
                member = tarfile.TarInfo(name)
                member.size = 6
                output.addfile(member, io.BytesIO(b"binary"))
        fm.extract_hugo(archive, self.root / "hugo")
        self.assertEqual((self.root / "hugo").read_bytes(), b"binary")
        self.assertFalse((self.root.parent / "escape").exists())
        with tarfile.open(archive, "w:gz") as output:
            member = tarfile.TarInfo("hugo")
            member.type, member.linkname = tarfile.SYMTYPE, "../escape"
            output.addfile(member)
        with self.assertRaisesRegex(fm.BuildError, "regular"):
            fm.extract_hugo(archive, self.root / "hugo")

    def test_installed_version_and_edition_are_checked(self):
        for actual in ("hugo v0.147.3+extended linux/amd64", "hugo v0.147.2 linux/amd64", "unrecognized"):
            with self.subTest(actual=actual), mock.patch.object(fm.subprocess, "check_output", return_value=actual):
                with self.assertRaises(fm.BuildError):
                    fm.checked_version(Path("hugo"), self.config)
        with mock.patch.object(fm.subprocess, "check_output", return_value="hugo v0.147.2+extended linux/amd64"):
            self.assertIn("0.147.2", fm.checked_version(Path("hugo"), self.config))


class BuildTests(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.args = argparse.Namespace(workspace=str(self.workspace), config="build.json",
                                       output=str(self.root / "public"), report=str(self.root / "report.json"),
                                       tools=str(self.root / "tools"))
        self.patches = [mock.patch.object(fm.subprocess, "check_output", return_value="a" * 40),
                        mock.patch.object(fm, "install_hugo", return_value=self.root / "tools/hugo"),
                        mock.patch.object(fm, "checked_version", return_value="hugo v0.147.2+extended linux/amd64")]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def execute(self, result=0):
        def run(command, site, log, environment):
            self.command, self.environment = command, environment
            Path(self.args.output, "index.html").write_text("<h1>Site</h1>")
            log.write_text("WARN example\n")
            return result, ["WARN example"], ["ERROR template function is unavailable"] if result else []
        with mock.patch.object(fm, "run_build", side_effect=run), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = fm.build(self.args)
        return status, json.loads(Path(self.args.report).read_text())

    def test_success_reports_exact_revision_and_actual_binary(self):
        with mock.patch.dict(fm.os.environ, {"FRONTMATTER_HEAD_SHA": "b" * 40, "FRONTMATTER_IMPLEMENTATION_SHA": "c" * 40}):
            status, report = self.execute()
        self.assertEqual(status, 0)
        self.assertEqual(report["source"]["commitSHA"], "a" * 40)
        self.assertEqual(report["source"]["headSHA"], "b" * 40)
        self.assertEqual(report["source"]["implementationSHA"], "c" * 40)
        self.assertEqual(report["hugo"]["requestedVersion"], "0.147.2")
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(self.environment["HUGO_ENVIRONMENT"], "production")
        self.assertIn("--baseURL", self.command)
        self.assertIn("https://example.com/blog/", self.command)

    def test_failed_build_retains_log_warnings_and_failure_phase(self):
        status, report = self.execute(result=1)
        self.assertEqual(status, 1)
        self.assertEqual(report["phase"], "build")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["warnings"], ["WARN example"])
        self.assertIn("template function is unavailable", report["error"])
        self.assertTrue(Path(self.args.report).with_suffix(".log").exists())

    def test_invalid_configuration_still_produces_failure_report(self):
        self.config["hugo"]["version"] = "latest"
        self.save_config()
        status, report = self.execute()
        self.assertEqual(status, 1)
        self.assertEqual(report["phase"], "configuration")
        self.assertIn("stable release", report["error"])

    def test_checksum_failure_does_not_start_build(self):
        with mock.patch.object(fm, "install_hugo", side_effect=fm.BuildError("checksum mismatch")):
            status, report = self.execute()
        self.assertEqual(status, 1)
        self.assertEqual(report["phase"], "installation")
        self.assertFalse(hasattr(self, "command"))

    def test_source_and_existing_output_are_never_overwritten(self):
        self.args.output = str(self.workspace / "public")
        status, report = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("outside", report["error"])
        self.args.output = str(self.root / "public")
        Path(self.args.output).mkdir()
        keep = Path(self.args.output) / "keep.txt"
        keep.write_text("retain me")
        status, report = self.execute()
        self.assertEqual(status, 1)
        self.assertEqual(keep.read_text(), "retain me")

    def test_empty_or_symlink_output_cannot_be_published(self):
        output = self.root / "output"
        output.mkdir()
        with self.assertRaisesRegex(fm.BuildError, "no HTML"):
            fm.validate_output(output)
        (output / "index.html").symlink_to(self.config_path)
        with self.assertRaisesRegex(fm.BuildError, "symbolic link"):
            fm.validate_output(output)

    def test_disabled_build_options_are_not_passed_as_strings(self):
        self.config["build"] = {"minify": False, "garbageCollect": False}
        self.save_config()
        status, _ = self.execute()
        self.assertEqual(status, 0)
        self.assertNotIn("--minify", self.command)
        self.assertNotIn("--gc", self.command)


if __name__ == "__main__":
    unittest.main()
