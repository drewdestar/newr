"""Init and manifest tests (SPEC.md section 11, M6 Phase 2b).

`newr init` scaffolds a project (main.newr + newr.json + locales/ +
.env.example + .gitignore) and `newr build` without --target reads
the manifest for the default target. A missing manifest changes
nothing (the default stays py); a malformed manifest, or a target
value that is not one of the seven targets, is an error naming the
file.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compiler.fmt import format_source  # noqa: E402
from compiler.init import (  # noqa: E402
    InitError,
    Manifest,
    ManifestError,
    VALID_TARGETS,
    default_target_for,
    find_manifest,
    http_allowlist_for,
    load_manifest,
    main_source,
    manifest_source,
    scaffold,
)

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"


def _run_cli(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *argv],
        capture_output=True, text=True, cwd=cwd)


class TestManifest(unittest.TestCase):

    def _write(self, directory, text, name="newr.json"):
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_load_full_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({
                "name": "demo", "version": "0.1.0", "target": "js",
                "default_locale": "id", "db_path": "data/app.db",
            }))
            manifest = load_manifest(path)
            self.assertEqual(manifest, Manifest(
                name="demo", version="0.1.0", target="js",
                default_locale="id", db_path="data/app.db"))

    def test_load_minimal_manifest(self):
        # Only name and version are required (SPEC section 11).
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({
                "name": "demo", "version": "0.1.0",
            }))
            manifest = load_manifest(path)
            self.assertIsNone(manifest.target)
            self.assertIsNone(manifest.default_locale)
            self.assertIsNone(manifest.db_path)

    def test_load_ignores_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({
                "name": "demo", "version": "0.1.0",
                "future_field": True,
            }))
            self.assertEqual(load_manifest(path).name, "demo")

    def test_load_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "{not json")
            with self.assertRaises(ManifestError) as ctx:
                load_manifest(path)
            self.assertIn("not valid JSON", str(ctx.exception))
            self.assertIn(str(path), str(ctx.exception))

    def test_load_top_level_must_be_an_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "[1, 2]")
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_load_missing_name_or_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            for data in ({}, {"name": "demo"},
                         {"version": "0.1.0"},
                         {"name": "", "version": "0.1.0"},
                         {"name": "demo", "version": 1}):
                path = self._write(tmp, json.dumps(data))
                with self.assertRaises(ManifestError, msg=str(data)):
                    load_manifest(path)

    def test_load_unknown_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({
                "name": "demo", "version": "0.1.0", "target": "go",
            }))
            with self.assertRaises(ManifestError) as ctx:
                load_manifest(path)
            self.assertIn("go", str(ctx.exception))

    def test_load_wrong_option_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            for data in (
                    {"name": "demo", "version": "0.1.0",
                     "target": 3},
                    {"name": "demo", "version": "0.1.0",
                     "default_locale": ["en"]},
                    {"name": "demo", "version": "0.1.0",
                     "db_path": 7}):
                path = self._write(tmp, json.dumps(data))
                with self.assertRaises(ManifestError, msg=str(data)):
                    load_manifest(path)

    def test_load_http_allowlist(self):
        # M8b: newr.json gains an optional http_allowlist of bare
        # hostnames; it loads as a tuple, and round-trips through
        # to_dict as a list.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({
                "name": "demo", "version": "0.1.0",
                "http_allowlist": ["localhost", "127.0.0.1",
                                   "api.example.com"],
            }))
            manifest = load_manifest(path)
            self.assertEqual(manifest.http_allowlist,
                             ("localhost", "127.0.0.1", "api.example.com"))
            self.assertEqual(manifest.to_dict()["http_allowlist"],
                             ["localhost", "127.0.0.1", "api.example.com"])

    def test_load_http_allowlist_absent_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({
                "name": "demo", "version": "0.1.0",
            }))
            self.assertIsNone(load_manifest(path).http_allowlist)

    def test_load_http_allowlist_malformed(self):
        # Not a list, a non-string entry, or a bare-hostname
        # violation (scheme, port, userinfo, path) is a manifest
        # error naming the file.
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("localhost",
                        ["localhost", 8080],
                        ["https://api.example.com"],
                        ["api.example.com:8080"],
                        ["user@api.example.com"],
                        ["api.example.com/todos"],
                        [""]):
                path = self._write(tmp, json.dumps({
                    "name": "demo", "version": "0.1.0",
                    "http_allowlist": bad,
                }))
                with self.assertRaises(ManifestError, msg=str(bad)) as ctx:
                    load_manifest(path)
                self.assertIn(str(path), str(ctx.exception))

    def test_http_allowlist_for_no_manifest_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            prog = Path(tmp) / "main.newr"
            prog.write_text('print "hi"\n', encoding="utf-8")
            self.assertIsNone(http_allowlist_for(prog))

    def test_http_allowlist_for_reads_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, json.dumps({
                "name": "demo", "version": "0.1.0",
                "http_allowlist": ["api.example.com"],
            }))
            prog = Path(tmp) / "main.newr"
            prog.write_text('print "hi"\n', encoding="utf-8")
            self.assertEqual(http_allowlist_for(prog),
                             ("api.example.com",))

    def test_load_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ManifestError):
                load_manifest(Path(tmp) / "newr.json")

    def test_manifest_to_dict_omits_absent_optionals(self):
        manifest = Manifest(name="demo", version="0.1.0")
        self.assertEqual(manifest.to_dict(),
                         {"name": "demo", "version": "0.1.0"})


class TestFindManifest(unittest.TestCase):

    def test_finds_manifest_in_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "newr.json").write_text(
                '{"name": "demo", "version": "0.1.0"}',
                encoding="utf-8")
            deep = root / "a" / "b"
            deep.mkdir(parents=True)
            self.assertEqual(find_manifest(deep), root / "newr.json")
            # A file start uses its parent directory.
            self.assertEqual(find_manifest(deep / "main.newr"),
                             root / "newr.json")

    def test_finds_manifest_in_the_start_dir_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "newr.json").write_text(
                '{"name": "demo", "version": "0.1.0"}',
                encoding="utf-8")
            self.assertEqual(find_manifest(root), root / "newr.json")

    def test_returns_none_without_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_manifest(Path(tmp)))


class TestDefaultTarget(unittest.TestCase):

    def _write_manifest(self, directory, data):
        path = Path(directory) / "newr.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_no_manifest_defaults_to_py(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main.newr"
            main.write_text('print "hi"\n', encoding="utf-8")
            self.assertEqual(default_target_for(main), "py")

    def test_manifest_target_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_manifest(tmp, {
                "name": "demo", "version": "0.1.0", "target": "dart",
            })
            main = Path(tmp) / "main.newr"
            main.write_text('print "hi"\n', encoding="utf-8")
            self.assertEqual(default_target_for(main), "dart")

    def test_manifest_without_target_defaults_to_py(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_manifest(tmp, {
                "name": "demo", "version": "0.1.0",
            })
            main = Path(tmp) / "main.newr"
            main.write_text('print "hi"\n', encoding="utf-8")
            self.assertEqual(default_target_for(main), "py")

    def test_malformed_manifest_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "newr.json"
            path.write_text("{bad", encoding="utf-8")
            main = Path(tmp) / "main.newr"
            main.write_text('print "hi"\n', encoding="utf-8")
            with self.assertRaises(ManifestError):
                default_target_for(main)


class TestScaffold(unittest.TestCase):

    def test_creates_all_five_scaffold_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            created = scaffold(Path(tmp) / "demo")
            names = sorted(str(p.relative_to(Path(tmp) / "demo"))
                           for p in created)
            self.assertEqual(names, [".env.example", ".gitignore",
                                     "locales/.gitkeep", "main.newr",
                                     "newr.json"])
            for path in created:
                self.assertTrue(path.is_file(), path)

    def test_manifest_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            scaffold(Path(tmp) / "demo", target="js")
            data = json.loads(
                (Path(tmp) / "demo" / "newr.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(data, {
                "name": "demo", "version": "0.2.0",
                "target": "js", "default_locale": "en",
            })

    def test_name_defaults_to_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            scaffold(Path(tmp) / "my-app")
            data = json.loads(
                (Path(tmp) / "my-app" / "newr.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(data["name"], "my-app")

    def test_main_source_is_fmt_clean(self):
        for target in VALID_TARGETS:
            source = main_source("demo", target)
            self.assertEqual(format_source(source, "<test>"), source,
                             target)

    def test_main_source_names_the_run_command(self):
        self.assertIn("python main.py", main_source("demo", "py"))
        self.assertIn("node main.js", main_source("demo", "js"))
        self.assertIn("dart main.dart", main_source("demo", "dart"))

    def test_manifest_source_round_trips(self):
        data = json.loads(manifest_source("demo", "rs"))
        self.assertEqual(data["name"], "demo")
        self.assertEqual(data["target"], "rs")

    def test_second_scaffold_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "demo"
            scaffold(directory)
            with self.assertRaises(InitError) as ctx:
                scaffold(directory)
            self.assertIn("newr.json", str(ctx.exception))

    def test_existing_main_newr_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "demo"
            directory.mkdir()
            (directory / "main.newr").write_text(
                'print "mine"\n', encoding="utf-8")
            with self.assertRaises(InitError) as ctx:
                scaffold(directory)
            self.assertIn("main.newr", str(ctx.exception))

    def test_unknown_target_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InitError):
                scaffold(Path(tmp) / "demo", target="go")

    def test_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            created = scaffold(Path(tmp) / "a" / "b" / "demo")
            self.assertTrue((Path(tmp) / "a" / "b" / "demo"
                             / "main.newr").is_file())
            self.assertEqual(len(created), 5)

    def test_env_example_and_gitignore_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            scaffold(Path(tmp) / "demo")
            env = (Path(tmp) / "demo" / ".env.example").read_text(
                encoding="utf-8")
            self.assertIn("DISCORD_TOKEN=", env)
            self.assertIn("Never commit .env", env)
            ignore = (Path(tmp) / "demo" / ".gitignore").read_text(
                encoding="utf-8")
            self.assertIn(".env\n", ignore)
            self.assertIn("__pycache__/", ignore)


class TestCLI(unittest.TestCase):

    def test_init_into_a_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_cli("init", str(Path(tmp) / "demo"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("created", result.stdout)
            self.assertIn("main.newr", result.stdout)
            demo = Path(tmp) / "demo"
            self.assertTrue((demo / "main.newr").is_file())
            self.assertTrue((demo / "newr.json").is_file())
            self.assertTrue((demo / "locales" / ".gitkeep").is_file())
            self.assertTrue((demo / ".env.example").is_file())
            self.assertTrue((demo / ".gitignore").is_file())

    def test_init_defaults_to_current_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_cli("init", cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(tmp) / "main.newr").is_file())
            data = json.loads((Path(tmp) / "newr.json").read_text(
                encoding="utf-8"))
            self.assertEqual(data["name"], Path(tmp).name)

    def test_init_name_and_target_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_cli("init", str(Path(tmp) / "demo"),
                              "--name", "cool-app", "--target", "kt")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(
                (Path(tmp) / "demo" / "newr.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(data["name"], "cool-app")
            self.assertEqual(data["target"], "kt")
            main = (Path(tmp) / "demo" / "main.newr").read_text(
                encoding="utf-8")
            self.assertIn("kotlinc", main)

    def test_init_refuses_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = str(Path(tmp) / "demo")
            self.assertEqual(_run_cli("init", directory).returncode, 0)
            result = _run_cli("init", directory)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("already exists", result.stderr)

    def test_init_unknown_target_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_cli("init", str(Path(tmp) / "demo"),
                              "--target", "go")
            self.assertEqual(result.returncode, 1)
            self.assertIn("unknown target", result.stderr)

    def test_init_unknown_flag_exits_1(self):
        result = _run_cli("init", "--bogus")
        self.assertEqual(result.returncode, 1)

    def test_init_two_directories_exits_1(self):
        result = _run_cli("init", "a", "b")
        self.assertEqual(result.returncode, 1)

    def test_init_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_cli("--json", "init",
                              str(Path(tmp) / "demo"))
            self.assertEqual(result.returncode, 0, result.stdout)
            envelope = json.loads(result.stdout)
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["command"], "init")
            self.assertEqual(envelope["data"]["name"], "demo")
            self.assertEqual(len(envelope["data"]["files"]), 5)

    def test_help_init(self):
        result = _run_cli("help", "init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("newr.json", result.stdout)

    def test_scaffolded_project_checks_and_builds_all_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "demo"
            self.assertEqual(
                _run_cli("init", str(demo)).returncode, 0)
            result = _run_cli("check", str(demo / "main.newr"))
            self.assertEqual(result.returncode, 0, result.stdout)
            expected = {"py": "main.py", "js": "main.js",
                        "java": "main.java", "cs": "main.cs",
                        "rs": "main.rs", "kt": "main.kt",
                        "dart": "main.dart"}
            for target, outfile in expected.items():
                result = _run_cli("build", str(demo / "main.newr"),
                                  "--target", target)
                self.assertEqual(result.returncode, 0,
                                 (target, result.stdout, result.stderr))
                self.assertTrue((demo / outfile).is_file(),
                                (target, outfile))

    def test_build_uses_manifest_target_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "demo"
            self.assertEqual(
                _run_cli("init", str(demo), "--target", "js")
                .returncode, 0)
            result = _run_cli("build", str(demo / "main.newr"))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue((demo / "main.js").is_file())
            self.assertFalse((demo / "main.py").exists())

    def test_build_explicit_target_overrides_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "demo"
            self.assertEqual(
                _run_cli("init", str(demo), "--target", "js")
                .returncode, 0)
            result = _run_cli("build", str(demo / "main.newr"),
                              "--target", "py")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue((demo / "main.py").is_file())
            self.assertFalse((demo / "main.js").exists())

    def test_build_without_manifest_still_defaults_to_py(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main.newr"
            main.write_text('print "Hello, world!"\n',
                            encoding="utf-8")
            result = _run_cli("build", str(main))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue((Path(tmp) / "main.py").is_file())

    def test_build_malformed_manifest_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "demo"
            self.assertEqual(
                _run_cli("init", str(demo)).returncode, 0)
            (demo / "newr.json").write_text("{bad", encoding="utf-8")
            result = _run_cli("build", str(demo / "main.newr"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("not valid JSON", result.stderr)

    def test_build_unknown_manifest_target_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "demo"
            self.assertEqual(
                _run_cli("init", str(demo)).returncode, 0)
            (demo / "newr.json").write_text(json.dumps({
                "name": "demo", "version": "0.1.0", "target": "go",
            }), encoding="utf-8")
            result = _run_cli("build", str(demo / "main.newr"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("go", result.stderr)

    def test_build_check_ignores_manifest(self):
        # `newr build --check` takes no target, so a broken manifest
        # must not fail the check.
        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "demo"
            self.assertEqual(
                _run_cli("init", str(demo)).returncode, 0)
            (demo / "newr.json").write_text("{bad", encoding="utf-8")
            result = _run_cli("build", str(demo / "main.newr"),
                              "--check")
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
