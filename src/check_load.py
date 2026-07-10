from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import traceback
import urllib.request
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover; Python < 3.11 fallback, kept for local testing
    tomllib = None  # type: ignore[assignment]

_CANONICALIZE_REGEX = re.compile(r"[-_.]+")
RESULT_MARKER = "__NONEBOT_PLUGIN_LOAD_CHECK_RESULT__="


def truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def canonicalize_name(name: str) -> str:
    return _CANONICALIZE_REGEX.sub("-", name).lower()


def strip_ansi(text: str | None) -> str:
    if not text:
        return ""
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def read_pyproject(workdir: Path) -> dict[str, Any]:
    pyproject = workdir / "pyproject.toml"
    if not pyproject.exists() or tomllib is None:
        return {}
    try:
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return {}


def detect_project_link(workdir: Path, package_spec: str) -> str:
    data = read_pyproject(workdir)
    project_name = data.get("project", {}).get("name")
    if isinstance(project_name, str) and project_name:
        return project_name

    # Best-effort fallback for a PyPI package spec such as nonebot-plugin-foo>=1.0.
    if package_spec and package_spec not in {".", "./"}:
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)", package_spec)
        if match:
            return match.group(1)
    return ""


def detect_module_name(workdir: Path, project_link: str) -> str:
    data = read_pyproject(workdir)
    nonebot_config = data.get("tool", {}).get("nonebot", {}) if data else {}

    plugins = nonebot_config.get("plugins")
    if isinstance(plugins, list):
        plugin_names = [item for item in plugins if isinstance(item, str) and item]
        if len(plugin_names) == 1:
            return plugin_names[0]

    if project_link:
        normalized = canonicalize_name(project_link)
        if normalized.startswith("nonebot-plugin-"):
            return normalized.replace("-", "_")

    candidates: list[str] = []
    for child in workdir.iterdir():
        if child.is_dir() and child.name.startswith("nonebot_plugin_"):
            if (child / "__init__.py").exists():
                candidates.append(child.name)
    if len(candidates) == 1:
        return candidates[0]

    return ""


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_bin(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts"
    return venv_dir / "bin"


def run_command(
    cmd: list[str] | str,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 600,
    shell: bool = False,
) -> tuple[bool, str, str]:
    proc = subprocess.run(  # noqa: S603
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout,
        text=True,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode == 0, proc.stdout or "", proc.stderr or ""


def write_output(name: str, value: str) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as f:
        if "\n" in value:
            delimiter = f"EOF_{name}_{os.getpid()}"
            f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
        else:
            f.write(f"{name}={value}\n")


def group(title: str, body: str) -> None:
    print(f"::group::{title}")
    if body.strip():
        print(body.rstrip())
    print("::endgroup::")


def install_target(package_spec: str, editable: bool) -> list[str]:
    package_spec = package_spec.strip() or "."
    args: list[str] = []
    package_path = package_spec.split("[", 1)[0].strip()
    looks_local = package_path in {".", "./"} or package_path.startswith(("./", "../", "/"))
    if editable and looks_local:
        args.append("-e")
    args.append(package_spec)
    return args


def project_dependency_groups(workdir: Path) -> tuple[list[str], list[str], list[str]] | None:
    data = read_pyproject(workdir)
    project = data.get("project") if data else None
    if not isinstance(project, dict):
        return None

    dependencies = project.get("dependencies") or []
    optional_dependencies = project.get("optional-dependencies") or {}
    if not isinstance(dependencies, list) or not isinstance(optional_dependencies, dict):
        return None

    all_dependencies = [item for item in dependencies if isinstance(item, str)]
    optional_requirements: list[str] = []
    extras: list[str] = []
    for extra, requirements in optional_dependencies.items():
        if not isinstance(extra, str) or not isinstance(requirements, list):
            continue
        extras.append(extra)
        optional_requirements.extend(item for item in requirements if isinstance(item, str))
    return all_dependencies, optional_requirements, extras


def with_optional_extras(package_spec: str, extras: list[str]) -> str:
    if not extras:
        return package_spec

    package_path, separator, existing = package_spec.partition("[")
    package_path = package_path.strip()
    looks_local = package_path in {".", "./"} or package_path.startswith(("./", "../", "/"))
    if not looks_local or (separator and not existing.endswith("]")):
        raise RuntimeError(
            "check-optional-dependencies requires a local package spec such as . or .[extra]."
        )

    existing_extras = [] if not separator else [
        item.strip() for item in existing[:-1].split(",") if item.strip()
    ]
    merged_extras = list(dict.fromkeys([*existing_extras, *extras]))
    return f"{package_path}[{','.join(merged_extras)}]"


def parse_requirement_name(requirement: str) -> str | None:
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)", requirement)
    if not match:
        return None
    return canonicalize_name(match.group(1))


def fetch_registry_plugins(registry_url: str) -> dict[str, str]:
    if not registry_url:
        return {}
    with urllib.request.urlopen(registry_url, timeout=30) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    result: dict[str, str] = {}
    for item in data:
        project_link = item.get("project_link")
        module_name = item.get("module_name")
        if isinstance(project_link, str) and isinstance(module_name, str):
            result[canonicalize_name(project_link)] = module_name
    return result


def get_installed_package_names(py: Path, timeout: int) -> set[str]:
    code = r"""
import importlib.metadata as metadata
import json
print(json.dumps([
    dist.metadata["Name"]
    for dist in metadata.distributions()
    if dist.metadata.get("Name")
]))
"""
    ok, stdout, stderr = run_command([str(py), "-c", code], cwd=Path.cwd(), timeout=timeout)
    if not ok:
        group("Failed to inspect installed packages", stdout + stderr)
        return set()
    try:
        return {canonicalize_name(name) for name in json.loads(stdout.strip() or "[]")}
    except json.JSONDecodeError:
        return set()


def get_package_version(py: Path, package_name: str, timeout: int) -> str | None:
    code = r"""
import importlib.metadata as metadata
import json
import sys
name = sys.argv[1]
try:
    print(metadata.version(name))
except metadata.PackageNotFoundError:
    print("")
"""
    ok, stdout, _ = run_command([str(py), "-c", code, package_name], cwd=Path.cwd(), timeout=timeout)
    if ok:
        version = stdout.strip()
        return version or None
    return None


def build_dependency_modules(
    *,
    requirements: list[str],
    installed_packages: set[str],
    project_link: str,
    registry_url: str,
) -> list[str]:
    try:
        plugin_list = fetch_registry_plugins(registry_url)
    except Exception as e:
        group("Failed to fetch NoneBot registry plugin list", str(e))
        return []

    self_name = canonicalize_name(project_link) if project_link else ""
    deps: list[str] = []
    for requirement in requirements:
        package_name = parse_requirement_name(requirement)
        if not package_name or package_name == self_name or package_name not in installed_packages:
            continue
        module_name = plugin_list.get(package_name)
        if module_name and module_name not in deps:
            deps.append(module_name)
    return deps


def write_fake_driver(test_dir: Path) -> None:
    (test_dir / "fake.py").write_text(
        textwrap.dedent(
            '''
            from collections.abc import AsyncGenerator
            from typing import Optional, Union

            from nonebot import logger
            from nonebot.drivers import (
                ASGIMixin,
                HTTPClientMixin,
                HTTPClientSession,
                HTTPVersion,
                Request,
                Response,
                WebSocketClientMixin,
            )
            from nonebot.drivers import Driver as BaseDriver
            from nonebot.internal.driver.model import CookieTypes, HeaderTypes, QueryTypes


            class Driver(BaseDriver, ASGIMixin, HTTPClientMixin, WebSocketClientMixin):
                @property
                def type(self) -> str:
                    return "fake"

                @property
                def logger(self):
                    return logger

                def run(self, *args, **kwargs):
                    super().run(*args, **kwargs)

                @property
                def server_app(self):
                    return None

                @property
                def asgi(self):
                    raise NotImplementedError

                def setup_http_server(self, setup):
                    raise NotImplementedError

                def setup_websocket_server(self, setup):
                    raise NotImplementedError

                async def request(self, setup: Request) -> Response:
                    raise NotImplementedError

                async def websocket(self, setup: Request) -> Response:
                    raise NotImplementedError

                async def stream_request(
                    self,
                    setup: Request,
                    *,
                    chunk_size: int = 1024,
                ) -> AsyncGenerator[Response, None]:
                    raise NotImplementedError

                def get_session(
                    self,
                    params: QueryTypes = None,
                    headers: HeaderTypes = None,
                    cookies: CookieTypes = None,
                    version: Union[str, HTTPVersion] = HTTPVersion.H11,
                    timeout: Optional[float] = None,
                    proxy: Optional[str] = None,
                ) -> HTTPClientSession:
                    raise NotImplementedError
            '''
        ).lstrip(),
        encoding="utf-8",
    )


def write_runner(test_dir: Path, module_name: str, deps: list[str]) -> None:
    (test_dir / "runner.py").write_text(
        textwrap.dedent(
            r'''
            from __future__ import annotations

            import json
            import sys
            import traceback

            from nonebot import init, load_plugin, logger, require
            from pydantic import BaseModel

            MODULE_NAME = __MODULE_NAME__
            DEPS = __DEPS__
            RESULT_MARKER = "__NONEBOT_PLUGIN_LOAD_CHECK_RESULT__="


            class SetEncoder(json.JSONEncoder):
                def default(self, value):
                    if isinstance(value, set):
                        return sorted(value)
                    return json.JSONEncoder.default(self, value)


            def dump_metadata(plugin):
                if not plugin or not plugin.metadata:
                    return None
                metadata = plugin.metadata
                return {
                    "name": metadata.name,
                    "desc": metadata.description,
                    "usage": metadata.usage,
                    "type": metadata.type,
                    "homepage": metadata.homepage,
                    "supported_adapters": metadata.supported_adapters,
                }


            def main() -> int:
                result = {"load": False, "metadata": None, "error": ""}
                try:
                    init()
                    plugin = load_plugin(MODULE_NAME)
                    if not plugin:
                        result["error"] = f"nonebot.load_plugin({MODULE_NAME!r}) returned None"
                        return 1

                    result["metadata"] = dump_metadata(plugin)

                    if plugin.metadata and plugin.metadata.config:
                        if not issubclass(plugin.metadata.config, BaseModel):
                            logger.error("插件配置项不是 Pydantic BaseModel 的子类")
                            result["error"] = "插件配置项不是 Pydantic BaseModel 的子类"
                            return 1

                    for dep in DEPS:
                        require(dep)

                    result["load"] = True
                    return 0
                except BaseException:
                    result["error"] = traceback.format_exc()
                    return 1
                finally:
                    print(RESULT_MARKER + json.dumps(result, ensure_ascii=False, cls=SetEncoder))


            if __name__ == "__main__":
                sys.exit(main())
            '''
        )
        .lstrip()
        .replace("__MODULE_NAME__", json.dumps(module_name))
        .replace("__DEPS__", json.dumps(deps, ensure_ascii=False)),
        encoding="utf-8",
    )


def parse_runner_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line.removeprefix(RESULT_MARKER))
    return {"load": False, "metadata": None, "error": "runner did not emit a structured result"}


def write_summary(result: dict[str, Any]) -> None:
    if not truthy(os.environ.get("INPUT_SUMMARY"), default=True):
        return
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    metadata = result.get("metadata") or {}
    output = strip_ansi(result.get("output", ""))
    status = "成功" if result.get("load") else "失败"
    lines = [
        "## NoneBot 插件加载检查",
        "",
        f"- 插件包：`{result.get('project_link') or ''}`",
        f"- 模块名：`{result.get('module_name') or ''}`",
        f"- 加载结果：**{status}**",
        f"- 已安装版本：`{result.get('version') or ''}`",
        f"- 测试环境：`{result.get('test_env') or ''}`",
        "",
        "### 插件元数据",
        "",
        "```json",
        json.dumps(metadata, ensure_ascii=False, indent=2),
        "```",
        "",
        "### 输出",
        "",
        "```text",
        output[-12000:],
        "```",
        "",
    ]
    with open(summary_file, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> int:
    workdir = Path.cwd().resolve()
    package_spec = os.environ.get("INPUT_PACKAGE", ".").strip() or "."
    timeout = int(os.environ.get("INPUT_TIMEOUT", "600") or "600")
    editable = truthy(os.environ.get("INPUT_EDITABLE"), default=True)
    check_optional_dependencies = truthy(
        os.environ.get("INPUT_CHECK_OPTIONAL_DEPENDENCIES"), default=False
    )
    fail_on_error = truthy(os.environ.get("INPUT_FAIL_ON_ERROR"), default=True)

    project_link = os.environ.get("INPUT_PROJECT_LINK", "").strip()
    if not project_link:
        project_link = detect_project_link(workdir, package_spec)

    module_name = os.environ.get("INPUT_MODULE_NAME", "").strip()
    if not module_name:
        module_name = detect_module_name(workdir, project_link)

    temp_parent = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()).resolve()
    test_dir = temp_parent / "nonebot-plugin-load-check"
    venv_dir = test_dir / ".venv"
    result_file = test_dir / "result.json"

    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "metadata": None,
        "output": "",
        "load": False,
        "run": False,
        "version": None,
        "config_present": False,
        "test_env": "",
        "module_name": module_name,
        "project_link": project_link,
        "dependencies": [],
        "optional_dependencies": [],
        "result_file": str(result_file),
    }

    try:
        if not module_name:
            raise RuntimeError(
                "Unable to determine plugin module name. Set inputs.module-name explicitly."
            )

        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("uv is required but was not found in PATH")

        project_dependencies = project_dependency_groups(workdir)
        if check_optional_dependencies:
            if project_dependencies is None:
                raise RuntimeError(
                    "check-optional-dependencies requires [project] metadata in pyproject.toml."
                )
            if os.environ.get("INPUT_INSTALL_COMMAND", "").strip():
                raise RuntimeError(
                    "check-optional-dependencies cannot be used with install-command."
                )
            base_dependencies, optional_dependencies, extras = project_dependencies
            package_spec = with_optional_extras(package_spec, extras)
            dependency_requirements = [*base_dependencies, *optional_dependencies]
            result["optional_dependencies"] = optional_dependencies
        elif project_dependencies is not None:
            dependency_requirements = project_dependencies[0]
        else:
            dependency_requirements = []

        print(f"Using workdir: {workdir}")
        print(f"Using package spec: {package_spec}")
        print(f"Using project link: {project_link or '<unknown>'}")
        print(f"Using module name: {module_name}")

        python_version_input = (
            os.environ.get("INPUT_PYTHON_VERSION", "").strip()
            or os.environ.get("UV_PYTHON", "").strip()
            or "3.12"
        )

        ok, stdout, stderr = run_command(
            [uv, "python", "install", python_version_input],
            cwd=workdir,
            timeout=timeout,
        )
        group("Install Python with uv", stdout + stderr)
        if not ok:
            raise RuntimeError(f"uv python install {python_version_input!r} failed")

        ok, stdout, stderr = run_command(
            [uv, "venv", "--managed-python", "--seed", "--python", python_version_input, str(venv_dir)],
            cwd=workdir,
            timeout=timeout,
        )
        group("Create uv virtual environment", stdout + stderr)
        if not ok:
            raise RuntimeError("uv venv failed")

        py = venv_python(venv_dir)
        extra_args = shlex.split(os.environ.get("INPUT_INSTALL_EXTRA_ARGS", ""))

        install_command = os.environ.get("INPUT_INSTALL_COMMAND", "").strip()
        if install_command:
            env = os.environ.copy()
            env["PATH"] = f"{venv_bin(venv_dir)}{os.pathsep}{env.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(venv_dir)
            ok, stdout, stderr = run_command(
                install_command,
                cwd=workdir,
                env=env,
                timeout=timeout,
                shell=True,
            )
            group("Install plugin", stdout + stderr)
            if not ok:
                raise RuntimeError("custom install-command failed")
        else:
            install_args = [
                uv,
                "pip",
                "install",
                "--python",
                str(py),
                *extra_args,
                *install_target(package_spec, editable),
            ]
            ok, stdout, stderr = run_command(install_args, cwd=workdir, timeout=timeout)
            group("Install plugin with uv", stdout + stderr)
            if not ok:
                raise RuntimeError("uv pip install failed")

        version = get_package_version(py, project_link, timeout) if project_link else None
        result["version"] = version

        deps: list[str] = []
        if truthy(os.environ.get("INPUT_LOAD_DEPENDENCIES"), default=True):
            deps = build_dependency_modules(
                requirements=dependency_requirements,
                installed_packages=get_installed_package_names(py, timeout),
                project_link=project_link,
                registry_url=os.environ.get("INPUT_REGISTRY_URL", ""),
            )
        result["dependencies"] = deps

        python_version = subprocess.check_output([str(py), "--version"], text=True).strip()
        python_version = python_version.removeprefix("Python ")
        nonebot_version = get_package_version(py, "nonebot2", timeout)
        pydantic_version = get_package_version(py, "pydantic", timeout)
        env_parts = [f"python=={python_version}"]
        if nonebot_version:
            env_parts.append(f"nonebot2=={nonebot_version}")
        if pydantic_version:
            env_parts.append(f"pydantic=={pydantic_version}")
        result["test_env"] = " ".join(env_parts)

        plugin_config = os.environ.get("INPUT_PLUGIN_CONFIG", "")
        config_file = os.environ.get("INPUT_PLUGIN_CONFIG_FILE", "").strip()
        if config_file:
            config_path = Path(config_file)
            if not config_path.is_absolute():
                config_path = workdir / config_path
            plugin_config = (plugin_config + "\n" + config_path.read_text(encoding="utf-8")).strip()
        result["config_present"] = bool(plugin_config)

        (test_dir / ".env").write_text("DRIVER=fake\n", encoding="utf-8")
        if plugin_config:
            (test_dir / ".env.prod").write_text(plugin_config + "\n", encoding="utf-8")

        write_fake_driver(test_dir)
        write_runner(test_dir, module_name, deps)

        result["run"] = True
        ok, stdout, stderr = run_command(
            [str(py), str(test_dir / "runner.py")],
            cwd=test_dir,
            timeout=timeout,
        )
        output = stdout + stderr
        runner_result = parse_runner_result(stdout)
        result["load"] = bool(runner_result.get("load")) and ok
        result["metadata"] = runner_result.get("metadata")
        runner_error = runner_result.get("error") or ""
        result["output"] = output if not runner_error else output + "\n" + runner_error
        group("Plugin load output", result["output"])

    except Exception as e:
        result["output"] = (result.get("output") or "") + "\n" + "".join(
            traceback.format_exception(type(e), e, e.__traceback__)
        )
        group("Plugin load check failed before runner", result["output"])

    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    write_output("load", "true" if result.get("load") else "false")
    write_output("run", "true" if result.get("run") else "false")
    write_output("version", str(result.get("version") or ""))
    write_output("metadata", json.dumps(result.get("metadata") or {}, ensure_ascii=False, separators=(",", ":")))
    write_output("result-file", str(result_file))
    write_output("test-env", str(result.get("test_env") or ""))

    write_summary(result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if fail_on_error and not result.get("load"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
