"""清洗批次与文件交付的回归测试：直接调用服务层，不依赖测试框架。"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.cleaning import BatchError, CleaningBatchService  # noqa: E402
from app.services.dataclean import DatacleanService  # noqa: E402
from app.services.delivery import DeliveryService, DeliveryError  # noqa: E402
from app.services.parser import ParserService  # noqa: E402
from app.services.xpath import XPathError, parse_fragment, select_text  # noqa: E402
from app.store import Store, store  # noqa: E402

CHECKS: list[tuple[str, callable]] = []


def check(name: str):
    def register(func):
        CHECKS.append((name, func))
        return func
    return register


def reset() -> None:
    fresh = Store()
    for module in list(store._tables):
        if module not in fresh._tables:
            del store._tables[module]
    for module, rows in fresh._tables.items():
        store._tables[module] = rows


def batch_of(template_id: int) -> dict:
    outcome = CleaningBatchService().submit_template(template_id)
    return outcome["batch"]


@check("PARS-0002：字段类型、解析字段结果与清洗结果可核对")
def test_pars_0002() -> None:
    reset()
    batch = batch_of(2)
    fields = CleaningBatchService().field_results(batch["id"])
    cleans = CleaningBatchService().clean_results(batch["id"])
    by_key = {(row["字段名"], row["记录序号"]): row for row in cleans}

    assert {row["字段类型"] for row in batch["目标字段"]} == {"文本", "数值", "文本列表"}
    assert all(row["字段类型"] for row in fields), "解析字段结果必须带字段类型"
    assert len(fields) == len(cleans) == 8
    assert {(row["字段名"], row["记录序号"]) for row in fields} == set(by_key)

    title = by_key[("标题", 1)]
    assert title["清洗值"] == ["便携折叠水杯"]
    # 标题同时命中 DATA-0001(20) 与 DATA-0003(15)，按优先级取 DATA-0001。
    assert title["去噪规则编号"] == "DATA-0001"

    price = by_key[("价格", 1)]
    assert price["原始值"] == 45.5 and price["清洗值"] == ["45.5"]

    spec = by_key[("规格", 1)]
    assert spec["多值分隔符"] == "|"
    assert spec["清洗值序列"] == ["容量：500ml", "颜色：深灰", "售后：138****8000"]
    assert spec["多值项数"] == len(spec["清洗值序列"]) == 3

    masked = by_key[("规格", 2)]
    assert masked["清洗值"] == ["****样机"], "敏感词脱敏必须生效"

    note = by_key[("备注", 1)]
    assert note["原始值"] is None
    assert note["清洗值"] == ["无备注"]
    assert note["使用默认填充值"] is True
    assert note["空值原因"] == "XPath 未命中该记录，解析值为空"
    assert note["执行轨迹"]["默认填充值"]["来源"] == "目标字段"


@check("DATA-0002 / DATA-0003：冻结的正则、默认值与规则版本")
def test_data_rules_frozen() -> None:
    reset()
    batch = batch_of(2)
    rules = batch["去噪规则"]
    assert rules["DATA-0002"]["正则匹配式"] == "[￥¥$,，\\s]"
    assert rules["DATA-0002"]["正则版本"] == "DATA-0002.REGEX@V1"
    assert rules["DATA-0002"]["默认填充值"] == "面议"
    assert rules["DATA-0002"]["默认值版本"] == "DATA-0002.DEFAULT@V1"
    assert rules["DATA-0003"]["脱敏替换模板"] == "\\1****\\2"
    assert rules["DATA-0003"]["脱敏版本"] == "DATA-0003.MASK@V1"
    assert rules["DATA-0003"]["规则版本"] == "DATA-0003@V1"
    assert rules["DATA-0003"]["优先级"] == 15


@check("冲突裁决：同字段多规则有确定赢家与理由")
def test_conflict_resolution() -> None:
    reset()
    batch = batch_of(2)
    notes = [note for note in batch["冲突裁决"] if note["字段"] == "标题"]
    assert len(notes) == 2
    winners = [note for note in notes if note["是否生效"]]
    assert len(winners) == 1 and winners[0]["规则编号"] == "DATA-0001"
    assert all(note["裁决理由"] for note in notes)

    assert batch["冲突裁决数"] == 4

    # 库存只命中 DATA-0004(20) 与 DATA-0003(15) 时，优先级高者胜，且胜者不必是脱敏规则。
    parser, cleaner = ParserService(), DatacleanService()
    parser.update_entry(1, {"fields": [{"字段名": "库存", "正则匹配式": ""}]})
    store.find("dataclean", 3)["目标字段"] = "库存"
    release = batch_of(1)
    stock = [note for note in release["冲突裁决"] if note["字段"] == "库存"]
    assert len(stock) == 2
    assert [note["规则编号"] for note in stock if note["是否生效"]] == ["DATA-0004"]
    assert any("优先级" in note["裁决理由"] for note in stock)


@check("同优先级冲突裁决：脱敏优先、版本再新、编号兜底")
def test_conflict_ties() -> None:
    from app.services.cleaning import resolve_conflicts

    candidates = [
        {"规则编号": "DATA-0009", "规则版本": "DATA-0009@V2", "优先级": 10, "正则用途": "过滤"},
        {"规则编号": "DATA-0008", "规则版本": "DATA-0008@V2", "优先级": 10, "正则用途": "过滤"},
        {"规则编号": "DATA-0007", "规则版本": "DATA-0007@V3", "优先级": 10, "正则用途": "脱敏"},
    ]
    winner, notes = resolve_conflicts("字段", candidates)
    assert winner["规则编号"] == "DATA-0007"
    assert len([note for note in notes if note["是否生效"]]) == 1
    # 同优先级下非脱敏规则让位给脱敏规则；同为过滤时按规则编号稳定排序。
    assert [note["规则编号"] for note in notes[1:]] == ["DATA-0008", "DATA-0009"]
    assert "脱敏" in notes[1]["裁决理由"]
    # 结果必须可复现：打乱输入顺序后赢家不变。
    winner_again, _ = resolve_conflicts("字段", list(reversed(candidates)))
    assert winner_again["规则编号"] == "DATA-0007"


@check("重复提交不生成重复批次")
def test_idempotent_submit() -> None:
    reset()
    svc = CleaningBatchService()
    first = svc.submit_template(2)
    second = svc.submit_template(2)
    assert second["duplicated"] is True
    assert second["batch"]["id"] == first["batch"]["id"]
    assert len(svc.list_batches(template_no="PARS-0002")) == 1


@check("规则改版形成新批次，旧批次保持原样")
def test_new_version_new_batch() -> None:
    reset()
    svc = CleaningBatchService()
    first = svc.submit_template(2)["batch"]
    before = svc.clean_results(first["id"])
    message = DatacleanService().update_entry(3, {"长度截断阈值": 8, "默认填充值": "待补充"})[1]
    assert "V2" in message
    second = svc.submit_template(2)["batch"]

    assert second["id"] != first["id"]
    assert second["批次编号"].endswith("0002")
    after = svc.clean_results(first["id"])
    assert after == before, "旧批次结果不能被新版规则改写"
    assert second["去噪规则"]["DATA-0003"]["规则版本"] != first["去噪规则"]["DATA-0003"]["规则版本"]
    assert second["去噪规则"]["DATA-0003"]["长度截断阈值"] == 8
    # 新批次冻结了新版本，且规格被截断
    assert any(len(value) <= 8 for row in svc.clean_results(second["id"]) if row["字段名"] == "规格" for value in row["清洗值"])


@check("模板字段改版：只递增变化组件，新批次重新冻结")
def test_field_version_bumps() -> None:
    reset()
    parser = ParserService()
    entry = parser.get_entry(2)
    old_version = next(row for row in parser.list_fields("PARS-0002") if row["字段名"] == "价格")["字段版本"]
    parser.update_entry(2, {"fields": [{"字段名": "价格", "XPath表达式": ".//span[@class='price']"}]})
    updated = entry["模板版本"]
    fields = {row["字段名"]: row for row in parser.list_fields("PARS-0002")}
    assert fields["价格"]["字段版本"] != old_version
    assert fields["价格"]["XPath版本"].endswith("XPATH@V2")
    assert fields["价格"]["正则版本"].endswith("REGEX@V1")
    assert updated != "PARS-0002@V1"

    batch = batch_of(2)
    frozen = {row["字段名"]: row for row in batch["目标字段"]}
    assert frozen["价格"]["XPath表达式"] == ".//span[@class='price']"
    assert frozen["价格"]["XPath版本"].endswith("XPATH@V2")
    assert frozen["价格"]["正则版本"].endswith("REGEX@V1")
    assert batch["模板版本"] == updated


@check("字段类型与登记字段数不一致时拒绝生成批次")
def test_field_count_guard() -> None:
    reset()
    store.find("parser", 2)["目标字段数"] = 5
    try:
        batch_of(2)
    except BatchError as error:
        assert "字段类型不一致" in str(error)
    else:
        raise AssertionError("字段数不一致必须拒绝生成批次")
    assert not store.rows("_cleaning_batches")


@check("写入失败时解析与清洗两侧同时回滚")
def test_atomic_rollback(monkeypatch=None) -> None:
    reset()
    svc = CleaningBatchService()
    counts = lambda: (len(store.rows("_cleaning_batches")), len(store.rows("_batch_field_results")), len(store.rows("_batch_clean_results")))
    before = counts()
    settings_failure = settings.cleaning_failure_injection
    object.__setattr__(settings, "cleaning_failure_injection", "fail_after")
    try:
        try:
            svc.submit_template(2)
        except BatchError as error:
            assert "整批回滚" in str(error)
        else:
            raise AssertionError("注入失败后不应生成批次")
        # 解析结果写入后因清洗结果失败而回滚：两侧都必须为空。
        assert counts() == before == (0, 0, 0)
        assert not any(row.get("操作类型") == "生成清洗批次" for row in store.rows("audittrail"))
    finally:
        object.__setattr__(settings, "cleaning_failure_injection", settings_failure)


@check("文件交付：导出/下载/打包保留版本且与结果一致")
def test_delivery() -> None:
    reset()
    batch = batch_of(2)
    delivery = DeliveryService()

    content, filename, record = delivery.export_batch(batch["id"])
    manifest = json.loads(content)
    assert manifest["批次编号"] == batch["批次编号"]
    assert manifest["模板版本"] == batch["模板版本"]
    assert manifest["去噪规则"]["DATA-0003"]["规则版本"] == "DATA-0003@V1"
    assert [item["字段名"] for item in manifest["目标字段"]] == ["标题", "价格", "规格", "备注"]
    assert manifest["字段结果"] and manifest["清洗结果"]
    assert filename.startswith(batch["批次编号"])

    content, filename, record = delivery.download_table(batch["id"])
    text = content.decode("utf-8-sig")
    assert "模板版本" in text.splitlines()[0]
    assert "去噪规则版本" in text.splitlines()[0]
    assert "138****8000" in text

    content, _filename, _record = delivery.package_batch(batch["id"])
    archive = zipfile.ZipFile(io.BytesIO(content))
    assert {"manifest.json", "field-results.csv", "clean-results.csv", "versions.csv", "conflicts.csv", "checksums.json"} <= set(archive.namelist())
    versions_csv = archive.read("versions.csv").decode("utf-8-sig")
    assert "PARS-0002.标题.XPATH@V1" in versions_csv
    assert "DATA-0003@V1" in versions_csv
    clean_csv = archive.read("clean-results.csv").decode("utf-8-sig")
    assert "138****8000" in clean_csv


@check("交付物使用批次快照：规则改版后旧批次导出不变")
def test_delivery_snapshot_stable() -> None:
    reset()
    batch = batch_of(2)
    delivery = DeliveryService()
    before, _name, _record = delivery.export_batch(batch["id"])
    DatacleanService().update_entry(3, {"默认填充值": "改版值"})
    after, _name, _record = delivery.export_batch(batch["id"])
    assert json.loads(before)["去噪规则"] == json.loads(after)["去噪规则"]
    assert json.loads(after)["去噪规则"]["DATA-0003"]["默认填充值"] == "暂无规格"


@check("XPath 子集：绝对路径、相对路径、位置谓词")
def test_xpath() -> None:
    document = parse_fragment('<div id="a"><span class="x"> 一 </span><ul><li>1</li><li>2</li></ul></div>')
    assert select_text(document, '/html/body/div[@id="a"]/span[@class="x"]/text()') == [" 一 "]
    assert select_text(document, "//li[2]/text()") == ["2"]
    assert select_text(document, "/html/body/div/span") == [" 一 "]
    try:
        select_text(document, "//span[contains(@class, 'x')]")
    except XPathError:
        pass
    else:
        raise AssertionError("不支持的谓词必须报错而不是静默返回空")


@check("校验接口：多值项数与序列一致，规则改版给出提示")
def test_verify() -> None:
    reset()
    batch = batch_of(2)
    report = DeliveryService().verify_batch(batch["id"])
    assert report["一致"] is True
    DatacleanService().update_entry(3, {"长度截断阈值": 70})
    report = DeliveryService().verify_batch(batch["id"])
    assert report["一致"] is True
    assert any("旧批次保持原样" in issue["说明"] for issue in report["问题"])


@check("PARS-0003：默认填充值与空值原因")
def test_pars_0003_defaults() -> None:
    reset()
    batch = batch_of(3)
    cleans = {(row["字段名"], row["记录序号"]): row for row in CleaningBatchService().clean_results(batch["id"])}
    salary = cleans[("薪资", 2)]
    assert salary["清洗值"] == ["面议"] and salary["使用默认填充值"] is True
    assert salary["空值原因"] == "XPath 命中节点但文本为空"
    assert salary["执行轨迹"]["默认填充值"]["版本"] == "PARS-0003.薪资.DEFAULT@V1"
    assert cleans[("薪资", 1)]["清洗值"] == ["15000.0-25000.0"]
    assert cleans[("城市", 2)]["清洗值"] == ["上海"]


def main() -> int:
    failures = []
    for name, func in CHECKS:
        try:
            func()
            print(f"PASS  {name}")
        except Exception as error:  # noqa: BLE001
            failures.append((name, error))
            print(f"FAIL  {name}: {error}")
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
