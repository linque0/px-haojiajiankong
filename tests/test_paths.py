"""数据路径自定义（config/paths_override.json）测试：校验、加载应用、CLI 子命令。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import config as cfg  # noqa: E402
from pxb7 import cli  # noqa: E402


@pytest.fixture()
def override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "paths_override.json"
    monkeypatch.setattr(cfg, "PATHS_OVERRIDE_FILE", target)
    return target


def test_validate_rejects_bad_input(override_file: Path, tmp_path: Path) -> None:
    with pytest.raises(cfg.PathOverrideError):
        cfg.validate_path_overrides({"state_dir": str(tmp_path)})      # 不可自定义键
    with pytest.raises(cfg.PathOverrideError):
        cfg.validate_path_overrides({"db": "relative.duckdb"})         # 相对路径
    with pytest.raises(cfg.PathOverrideError):
        cfg.validate_path_overrides({"db": ""})                        # 空
    with pytest.raises(cfg.PathOverrideError):
        cfg.validate_path_overrides({"db": str(tmp_path) + "\x00x"})   # 空字符
    with pytest.raises(cfg.PathOverrideError):
        cfg.validate_path_overrides(["db"])                            # 非映射
    resolved = cfg.validate_path_overrides({"runs": str(tmp_path / "runs")})
    assert resolved["runs"] == (tmp_path / "runs").resolve()


def test_save_load_clear_roundtrip(override_file: Path, tmp_path: Path) -> None:
    assert cfg.load_path_overrides() == {}
    saved = cfg.save_path_overrides({"db": str(tmp_path / "data" / "pxb7.duckdb")})
    assert saved["db"].endswith("pxb7.duckdb") and override_file.is_file()
    assert cfg.load_path_overrides()["db"] == (tmp_path / "data" / "pxb7.duckdb").resolve()
    # 空对象 = 恢复默认（删除文件）
    assert cfg.save_path_overrides({}) == {}
    assert not override_file.is_file()
    assert cfg.load_path_overrides() == {}
    assert cfg.clear_path_overrides() is False, "文件已不存在时返回 False"
    cfg.save_path_overrides({"runs": str(tmp_path / "runs")})
    assert cfg.clear_path_overrides() is True


def test_corrupt_or_invalid_file_is_tolerated(override_file: Path, tmp_path: Path) -> None:
    override_file.parent.mkdir(parents=True, exist_ok=True)
    override_file.write_text("{bad json", encoding="utf-8")
    assert cfg.load_path_overrides() == {}, "损坏文件不得阻断启动"
    override_file.write_text(json.dumps({"db": "rel.duckdb", "runs": 123}),
                             encoding="utf-8")
    assert cfg.load_path_overrides() == {}, "非法条目整体忽略"
    override_file.write_text(json.dumps({"runs": str(tmp_path / "runs")}), encoding="utf-8")
    assert set(cfg.load_path_overrides()) == {"runs"}, "合法条目仍生效"


def test_load_settings_applies_override(override_file: Path, tmp_path: Path) -> None:
    cfg.save_path_overrides({"runs": str(tmp_path / "custom-runs"),
                             "raw_root": str(tmp_path / "custom-raw")})
    settings = cfg.load_settings()
    assert settings.paths.runs == (tmp_path / "custom-runs").resolve()
    assert settings.paths.raw_root == (tmp_path / "custom-raw").resolve()
    assert any("自定义数据路径" in w for w in settings.warnings)
    items = cfg.paths_summary(settings)
    customized = {i["key"] for i in items if i["customized"]}
    assert customized == {"runs", "raw_root"}
    assert items[-1]["key"] == "plugin_config", "插件配置文件路径也纳入展示"


def test_env_overrides_win_over_file(override_file: Path, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    cfg.save_path_overrides({"db": str(tmp_path / "file.duckdb")})
    monkeypatch.setenv(cfg.ENV_DB_PATH, str(tmp_path / "env.duckdb"))
    settings = cfg.load_settings()
    assert settings.paths.db == (tmp_path / "env.duckdb").resolve(), \
        "优先级：settings.yaml < 覆写文件 < 环境变量"
    assert any(cfg.ENV_DB_PATH in w for w in settings.warnings)


def test_prepare_data_dirs_creates_and_probes(override_file: Path, tmp_path: Path) -> None:
    base = cfg.load_settings()
    import dataclasses
    settings = dataclasses.replace(base, paths=dataclasses.replace(
        base.paths, db=tmp_path / "d" / "sub" / "pxb7.duckdb",
        raw_root=tmp_path / "d" / "raw", runs=tmp_path / "d" / "runs",
        log_dir=tmp_path / "d" / "logs"))
    created = cfg.prepare_data_dirs(settings)
    assert len(created) == 4
    for folder in (tmp_path / "d" / "sub", tmp_path / "d" / "raw",
                   tmp_path / "d" / "runs", tmp_path / "d" / "logs"):
        assert folder.is_dir()
    assert not list((tmp_path / "d").rglob(".pxb7-write-probe.tmp")), "探针文件必须清理"


def test_cli_paths_set_reset_json(override_file: Path, tmp_path: Path,
                                  capsys: pytest.CaptureFixture) -> None:
    new_root = tmp_path / "cli-data"
    code = cli.main(["paths", "--set", f"runs={new_root / 'runs'}",
                     "--set", f"log_dir={new_root / 'logs'}", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])           # 提示行之后是 JSON 主体
    assert payload["ok"] is True
    customized = {i["key"] for i in payload["items"] if i["customized"]}
    assert customized == {"runs", "log_dir"}
    assert (new_root / "runs").is_dir(), "CLI 设置同样建目录"

    code = cli.main(["paths", "--set", "db=rel.duckdb"])
    assert code == 1, "相对路径必须拒绝（退出码 1）"
    capsys.readouterr()

    code = cli.main(["paths", "--reset"])
    assert code == 0 and not override_file.is_file()
    assert cfg.load_path_overrides() == {}
    capsys.readouterr()

    code = cli.main(["paths", "--set", "nonsense"])
    assert code == 2, "格式错误按用法错误（退出码 2）"