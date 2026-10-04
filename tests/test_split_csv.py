"""split-csv（把分析 CSV 按游戏拆成多个表）测试：纯文件操作，离线。"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import analysis as A  # noqa: E402


def _write_csv(path: Path, rows: list[dict[str, object]], *, bom: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["game_id", "game_name", "price_yuan", "title"])
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_split_by_game_writes_one_file_per_game(tmp_path: Path) -> None:
    src = tmp_path / "pxb7-listings-20261003.csv"
    _write_csv(src, [
        {"game_id": "10302", "game_name": "鸣潮", "price_yuan": 199, "title": "鸣潮 甲"},
        {"game_id": "10302", "game_name": "鸣潮", "price_yuan": 299, "title": "鸣潮 乙"},
        {"game_id": "10026", "game_name": "原神", "price_yuan": 300, "title": "原神 甲"},
        {"game_id": "10371", "game_name": "三角洲行动", "price_yuan": 180, "title": "三角洲 甲"},
    ])
    result = A.split_csv_by_game(src)
    files = {Path(item["file"]).name: item["rows"] for item in result["files"]}
    assert files == {"pxb7-listings-20261003-鸣潮-10302.csv": 2,
                     "pxb7-listings-20261003-原神-10026.csv": 1,
                     "pxb7-listings-20261003-三角洲行动-10371.csv": 1}
    assert result["out_dir"] == str(tmp_path / "by_game")
    # 行不重不漏、每份只含一个游戏
    all_rows = [r for item in result["files"] for r in _read(Path(item["file"]))]
    assert len(all_rows) == 4 and {r["game_id"] for r in all_rows} == {"10302", "10026", "10371"}
    assert result["rows"] == 4 and result["column_count"] == 4


def test_split_preserves_columns_bom_and_order(tmp_path: Path) -> None:
    src = tmp_path / "in.csv"
    rows = [{"game_id": "10026", "game_name": "原神", "price_yuan": "300", "title": "T1"},
            {"game_id": "10302", "game_name": "鸣潮", "price_yuan": "199", "title": "T2"}]
    _write_csv(src, rows)
    result = A.split_csv_by_game(src, out_dir=tmp_path / "out")
    first = _read(Path(result["files"][0]["file"]))
    with Path(result["files"][0]["file"]).open("rb") as f:
        assert f.read(3) == b"\xef\xbb\xbf", "输出必须带 UTF-8 BOM（Excel 直接打开不乱码）"
    assert list(first[0]) == ["game_id", "game_name", "price_yuan", "title"], "列与列序保持一致"
    assert first[0]["title"] == "T1" and first[0]["price_yuan"] == "300", "值原样保留（不重排不转换）"


def test_split_rows_without_game_go_to_unclassified(tmp_path: Path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, [
        {"game_id": "10026", "game_name": "原神", "price_yuan": "300", "title": "T1"},
        {"game_id": "", "game_name": "", "price_yuan": "1", "title": "坏行"},
        {"game_id": "999", "game_name": "", "price_yuan": "2", "title": "只有id"},
    ])
    result = A.split_csv_by_game(src)
    labels = {item["label"]: item["rows"] for item in result["files"]}
    assert labels == {"原神-10026": 1, "999": 1, "未分类": 1}, "缺游戏信息的行进未分类，不丢行"
    bad = _read(tmp_path / "by_game" / "in-未分类.csv")
    assert bad[0]["title"] == "坏行"


def test_split_sanitizes_illegal_filename_characters(tmp_path: Path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, [{"game_id": "1", "game_name": 'a/b:c*', "price_yuan": "5", "title": "T"}])
    result = A.split_csv_by_game(src)
    name = Path(result["files"][0]["file"]).name
    assert set(name) <= set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                            "-_.（）" + "".join(chr(c) for c in range(0x4E00, 0x9FFF + 1))), \
        f"文件名含 Windows 非法字符：{name}"


def test_split_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        A.split_csv_by_game(tmp_path / "nope.csv")
