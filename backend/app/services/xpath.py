"""轻量 XPath 子集：让解析模板能从采集示例里取到字段值，且不额外引入三方可依赖。

支持的写法（足够覆盖字段解析器的模板配置）：
- 绝对路径：``/html/body/div[@id="detail"]/h1/text()``
- 相对路径：``.//span[@class="title"]/text()`` 或 ``./td/text()``
- 子节点/后代节点：``/`` 与 ``//``
- 属性等值谓词：``[@class="price"]``、``[@id='detail']``
- 位置谓词：``li[2]``
- 通配标签：``*``
- 结尾 ``/text()``：取节点全部可见文本（含子节点文本，便于后续去标签清洗）

不支持函数、轴、逻辑谓词；遇到这类表达式会抛出 ``XPathError``，由批次生成流程转成可读的失败原因，
而不是悄悄返回空值。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

_TOKEN_RE = re.compile(r"^(?P<tag>\*|[A-Za-z_][\w:.-]*)\s*(?P<preds>(?:\[[^\]]+\])*)$")
_ATTR_PRED_RE = re.compile(r"^@(?P<name>[\w:.-]+)\s*=\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)')$")
_POS_PRED_RE = re.compile(r"^\d+$")


class XPathError(ValueError):
    """XPath 表达式不受支持或无法求值。"""


class _HtmlTreeBuilder(HTMLParser):
    """把宽松的 HTML 片段转成 ElementTree，补上 XML 解析不接受的场景。"""

    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("html")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = ET.SubElement(self._stack[-1], tag, {key: value or "" for key, value in attrs})
        if tag not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ET.SubElement(self._stack[-1], tag, {key: value or "" for key, value in attrs})

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        last = self._stack[-1]
        if len(last):
            last[-1].tail = (last[-1].tail or "") + data
        else:
            last.text = (last.text or "") + data


def parse_fragment(fragment: str) -> ET.Element:
    """把 HTML 片段挂到合成根节点下，让绝对路径模板可以稳定求值。"""
    if not str(fragment or "").strip():
        raise XPathError("采集示例为空，无法生成解析字段结果")
    wrapped = f"<html><body>{fragment}</body></html>"
    try:
        parsed = ET.fromstring(wrapped)
    except ET.ParseError:
        builder = _HtmlTreeBuilder()
        try:
            builder.feed(wrapped)
            builder.close()
        except Exception as error:  # pragma: no cover - 仅兜底极端输入
            raise XPathError(f"采集示例无法解析为 HTML：{error}") from error
        parsed = builder.root
    document = ET.Element("document")
    document.append(parsed)
    return document


def _parse_expression(expression: str) -> list[tuple[str, str]]:
    """把表达式拆成 (轴, 步骤) 序列；绝对路径从合成根节点开始匹配。"""
    text = str(expression or "").strip()
    if not text:
        raise XPathError("XPath 表达式为空")
    if text.endswith("/text()"):
        text = text[: -len("/text()")]
    if not text:
        raise XPathError("XPath 表达式只有 text()，缺少节点路径")

    steps: list[tuple[str, str]] = []
    buffer = ""
    index = 0
    axis = "child"
    while index < len(text):
        char = text[index]
        if char == "/":
            if buffer.strip():
                steps.append((axis, buffer.strip()))
                buffer = ""
            if index + 1 < len(text) and text[index + 1] == "/":
                axis = "desc"
                index += 2
            else:
                axis = "child"
                index += 1
            continue
        buffer += char
        index += 1
    if buffer.strip():
        steps.append((axis, buffer.strip()))
    if not steps:
        raise XPathError(f"无法解析 XPath 表达式：{expression}")
    return steps


def _parse_step(step: str) -> tuple[str, list[tuple[str, str]], int | None]:
    match = _TOKEN_RE.match(step)
    if not match:
        raise XPathError(f"暂不支持的 XPath 片段：{step}")
    tag = match.group("tag")
    predicates = re.findall(r"\[([^\]]+)\]", match.group("preds") or "")
    attributes: list[tuple[str, str]] = []
    position: int | None = None
    for predicate in predicates:
        predicate = predicate.strip()
        if _POS_PRED_RE.match(predicate):
            if position is not None:
                raise XPathError(f"不支持连续位置谓词：{step}")
            position = int(predicate)
            if position <= 0:
                raise XPathError(f"位置谓词必须从 1 开始：{step}")
            continue
        attribute = _ATTR_PRED_RE.match(predicate)
        if not attribute:
            raise XPathError(f"暂不支持的 XPath 谓词：[{predicate}]")
        expected = attribute.group("dq")
        if expected is None:
            expected = attribute.group("sq")
        attributes.append((attribute.group("name"), expected or ""))
    return tag, attributes, position


def _matches(node: ET.Element, tag: str, attributes: list[tuple[str, str]]) -> bool:
    if tag != "*" and node.tag != tag:
        return False
    return all(node.get(name) == value for name, value in attributes)


def _apply(nodes: list[ET.Element], axis: str, step: str) -> list[ET.Element]:
    tag, attributes, position = _parse_step(step)
    result: list[ET.Element] = []
    for node in nodes:
        if axis == "desc":
            candidates = [item for item in node.iter() if item is not node]
        else:
            candidates = list(node)
        matched = [item for item in candidates if _matches(item, tag, attributes)]
        if position is not None:
            matched = [matched[position - 1]] if len(matched) >= position else []
        result.extend(matched)
    return result


def select(root: ET.Element, expression: str) -> list[ET.Element]:
    """按表达式返回元素节点列表。"""
    nodes = [root]
    for axis, step in _parse_expression(expression):
        if step == ".":
            continue
        nodes = _apply(nodes, axis, step)
    return nodes


def select_text(root: ET.Element, expression: str) -> list[str]:
    """返回表达式的文本结果；``/text()`` 结尾与元素节点都取全部可见文本。"""
    return ["".join(node.itertext()) for node in select(root, expression)]


def validate(expression: str) -> None:
    """只做语法层面的检查，不要求一定命中节点。"""
    for _axis, step in _parse_expression(expression):
        if step != ".":
            _parse_step(step)
