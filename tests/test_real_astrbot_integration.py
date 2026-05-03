import json
import os
import pathlib
import subprocess
import sys
import textwrap


def test_plugin_initializes_under_real_astrbot(tmp_path):
    script = textwrap.dedent(
        """
        import asyncio
        import importlib.util
        import pathlib
        import sys
        import types

        root = pathlib.Path.cwd()

        package = types.ModuleType("astrbot_plugin_tmemory")
        package.__path__ = [str(root)]
        sys.modules["astrbot_plugin_tmemory"] = package

        def load_module(module_name, file_name):
            full_name = f"astrbot_plugin_tmemory.{module_name}"
            spec = importlib.util.spec_from_file_location(full_name, root / file_name)
            module = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = module
            spec.loader.exec_module(module)
            return module

        load_module("hybrid_search", "hybrid_search.py")
        main = load_module("main", "main.py")

        async def run():
            plugin = main.TMemoryPlugin(context=None, config={"webui_enabled": False})
            await plugin.initialize()
            with plugin._db() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                    ).fetchall()
                }
            assert "memories" in tables
            assert plugin._worker_running is True
            await plugin.terminate()
            assert plugin._worker_running is False
            assert plugin._db_mgr._conn is None

        asyncio.run(run())
        """
    )

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd="/Users/tango/Documents/paperclip/astrbot_plugin_tmemory",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_plugin_is_discoverable_from_real_astrbot_plugin_directory(tmp_path):
    astrbot_root = tmp_path / "astrbot-root"
    script = textwrap.dedent(
        f"""
        import importlib
        import os
        import pathlib
        import shutil
        import sys

        from astrbot.core.star.star_manager import PluginManager
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

        repo_root = pathlib.Path(r"/Users/tango/Documents/paperclip/astrbot_plugin_tmemory")
        astrbot_root = pathlib.Path(r"{astrbot_root}")
        os.environ["ASTRBOT_ROOT"] = str(astrbot_root)

        plugin_root = pathlib.Path(get_astrbot_plugin_path())
        plugin_root.mkdir(parents=True, exist_ok=True)
        installed_plugin = plugin_root / "astrbot_plugin_tmemory"
        shutil.copytree(repo_root, installed_plugin, dirs_exist_ok=True)

        sys.path.insert(0, str(astrbot_root))

        modules = PluginManager._get_modules(str(plugin_root))
        assert any(item["pname"] == "astrbot_plugin_tmemory" for item in modules), modules

        plugin_name = PluginManager._get_plugin_dir_name_from_metadata(str(installed_plugin))
        assert plugin_name == "astrbot_plugin_tmemory"

        metadata = PluginManager._load_plugin_metadata(str(installed_plugin))
        assert metadata is not None
        assert metadata.name == "astrbot_plugin_tmemory"
        assert metadata.version == "v0.8.0"

        module = importlib.import_module("data.plugins.astrbot_plugin_tmemory.main")
        assert module.TMemoryPlugin.__name__ == "TMemoryPlugin"
        """
    )

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd="/Users/tango/Documents/paperclip/astrbot_plugin_tmemory",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_web_server_admin_import_works_from_real_astrbot_plugin_package(tmp_path):
    astrbot_root = tmp_path / "astrbot-root"
    script = textwrap.dedent(
        f"""
        import importlib
        import os
        import pathlib
        import shutil
        import sys

        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

        repo_root = pathlib.Path(r"/Users/tango/Documents/paperclip/astrbot_plugin_tmemory")
        astrbot_root = pathlib.Path(r"{astrbot_root}")
        os.environ["ASTRBOT_ROOT"] = str(astrbot_root)

        plugin_root = pathlib.Path(get_astrbot_plugin_path())
        plugin_root.mkdir(parents=True, exist_ok=True)
        installed_plugin = plugin_root / "astrbot_plugin_tmemory"
        shutil.copytree(repo_root, installed_plugin, dirs_exist_ok=True)

        sys.path.insert(0, str(astrbot_root))

        main = importlib.import_module("data.plugins.astrbot_plugin_tmemory.main")
        plugin = main.TMemoryPlugin(context=None, config={{"webui_enabled": False}})
        web_server_cls = plugin._load_web_server_class()
        web_server = web_server_cls(plugin, {{"webui_password": "secret"}})
        admin = web_server._get_admin()

        assert admin.__class__.__name__ == "AdminService"
        """
    )

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd="/Users/tango/Documents/paperclip/astrbot_plugin_tmemory",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_docker_init_deepseek_injection_handles_bom_without_leaking_key(tmp_path):
    repo_root = pathlib.Path("/Users/tango/Documents/paperclip/astrbot_plugin_tmemory")
    init_script = repo_root / "docker" / "astrbot_init.sh"
    script_text = init_script.read_text(encoding="utf-8")
    python_block = script_text.split("python3 <<'PY'", 1)[1].split("\nPY", 1)[0]

    cmd_config = tmp_path / "cmd_config.json"
    cmd_config.write_text(
        '\ufeff{"provider": [], "provider_settings": {}, "dashboard": {}}',
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "CMD_CONFIG": str(cmd_config),
            "DEEPSEEK_API_KEY": "sk-test-secret-should-not-be-written",
            "DEEPSEEK_BASE_URL": "https://example.invalid/deepseek/",
            "DEEPSEEK_MODEL": "deepseek-test-model",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", python_block],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    saved_text = cmd_config.read_text(encoding="utf-8")
    saved_config = json.loads(saved_text)
    source = saved_config["provider_sources"][0]
    provider = saved_config["provider"][0]
    assert provider["id"] == "deepseek"
    assert provider["provider_source_id"] == "deepseek_source"
    assert source["id"] == "deepseek_source"
    assert source["provider_type"] == "chat_completion"
    assert source["api_base"] == "https://example.invalid/deepseek/v1"
    assert source["key"] == ["$DEEPSEEK_API_KEY"]
    assert "sk-test-secret-should-not-be-written" not in saved_text
