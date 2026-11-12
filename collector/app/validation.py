"""发布前校验：CSV 中每条记录的产线、日期都必须与任务请求吻合。"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass
class ValidationResult:
    ok: bool
    row_count: int
    message: str


def validate_csv(path: str | Path, expected_line: str, expected_date: str) -> ValidationResult:
    """校验导出文件。

    规则：
    1. 表头必须是 产线,日期,样品号,测量值；
    2. 至少有一条数据记录；
    3. 每条记录的产线、日期都与请求一致（任一条不符即整体拒绝发布）；
    4. 样品号、测量值不得为空。
    """
    path = Path(path)
    try:
        # utf-8-sig 自动吃掉门户导出的 BOM。
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                return ValidationResult(False, 0, "CSV 为空，缺少表头")
            if [h.strip() for h in header] != config.CSV_HEADER:
                return ValidationResult(
                    False, 0, f"表头不匹配：实际 {header}，期望 {config.CSV_HEADER}"
                )
            row_count = 0
            for index, row in enumerate(reader, start=2):  # 行号含表头
                if not row or all(not cell.strip() for cell in row):
                    continue  # 容忍末尾空行
                row_count += 1
                if len(row) != 4:
                    return ValidationResult(
                        False, row_count, f"第 {index} 行列数为 {len(row)}，应为 4"
                    )
                line, day, sample, value = (cell.strip() for cell in row)
                if line != expected_line:
                    return ValidationResult(
                        False, row_count,
                        f"第 {index} 行产线为 {line!r}，与请求产线 {expected_line!r} 不符，拒绝发布",
                    )
                if day != expected_date:
                    return ValidationResult(
                        False, row_count,
                        f"第 {index} 行日期为 {day!r}，与请求日期 {expected_date!r} 不符，拒绝发布",
                    )
                if not sample:
                    return ValidationResult(False, row_count, f"第 {index} 行样品号为空")
                if not value:
                    return ValidationResult(False, row_count, f"第 {index} 行测量值为空")
    except UnicodeDecodeError as exc:
        return ValidationResult(False, 0, f"CSV 编码无法按 UTF-8 解析：{exc}")
    except OSError as exc:
        return ValidationResult(False, 0, f"CSV 读取失败：{exc}")

    if row_count == 0:
        return ValidationResult(False, 0, "导出文件无数据记录，拒绝发布")
    return ValidationResult(True, row_count, f"校验通过，共 {row_count} 条记录")
