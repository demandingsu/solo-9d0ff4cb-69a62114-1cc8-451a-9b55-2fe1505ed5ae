"""原公告资格条件解析与企业证书核验。

资格条件来自**原公告详情页**的 `table.qualification-table`，按标段组织：

* 每个标段是一张（或一组）资格表；
* 表内每个 `tr.qual-group` 是一个“资格条件组”；
* 组内可有一个或多个 `span.candidate` 候选条件（类别 + 最低等级），
  同一组满足任一候选即可，所有组均须满足；
* 候选条件的等级是“门槛等级”：同类别证书等级数字越小越高，
  因此企业等级数字 <= 门槛等级即满足（例如“二级及以上”接受一级、二级）；
* 企业证书有效期不得早于（即必须 >=）项目生效投标截止日期。

本模块只做纯解析/判定，不访问网络、不依赖数据库，便于单元测试。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from bs4 import BeautifulSoup

# 中文数字（资格等级通常为“一级/二级/三级”）
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}
_CN_NUM_CHARS = "零一二两三四五六七八九十百壹贰叁肆伍陆柒捌玖"


class QualificationDataError(ValueError):
    """详情页资格表缺失、标段缺失或条件字段不完整。"""


@dataclass(frozen=True)
class CandidateCondition:
    category: str
    level: int
    text: str

    @property
    def level_text(self) -> str:
        return f"{self.level}级"


@dataclass
class QualificationGroup:
    name: str
    clause_source: str
    candidates: list[CandidateCondition] = field(default_factory=list)


@dataclass
class QualificationSection:
    section_no: str
    section_name: str
    groups: list[QualificationGroup] = field(default_factory=list)


@dataclass(frozen=True)
class CertificateInfo:
    """企业提供的一张证书（类别、等级、有效期）。"""

    category: str
    level: int
    valid_until: date


@dataclass
class GroupEvaluation:
    group: QualificationGroup
    satisfied: bool
    matched_candidate: CandidateCondition | None = None
    matched_certificate: CertificateInfo | None = None
    reason: str | None = None


@dataclass
class SectionEvaluation:
    satisfied: bool
    group_results: list[GroupEvaluation]
    unmet_conditions: list[dict]



# ---------------------------------------------------------------------------
# 文本规范化
# ---------------------------------------------------------------------------
def normalize_category(value: str) -> str:
    """类别比较忽略空白、全半角差异及可选的“资质”后缀。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", "", text)
    text = text.strip("。.;；,，")
    # “市政公用工程施工总承包资质” 与 “市政公用工程施工总承包” 视为同类
    if text.endswith("资质") and len(text) > 2:
        text = text[:-2]
    return text.casefold()


def parse_level(value: object) -> int:
    """把 '2'、2、'二级'、'贰级' 等统一为整数等级。"""
    if isinstance(value, bool):
        raise ValueError(f"无效的等级: {value!r}")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"等级必须为正整数: {value}")
        return value
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        raise ValueError("等级不能为空")
    digits = re.sub(r"[^\d]", "", text)
    if digits:
        level = int(digits)
        if level <= 0:
            raise ValueError(f"等级必须为正整数: {value!r}")
        return level
    cn = re.search(rf"[{_CN_NUM_CHARS}]+", text)
    if cn:
        level = _cn_to_int(cn.group(0))
        if level > 0:
            return level
    raise ValueError(f"无法解析证书等级: {value!r}")


def _cn_to_int(text: str) -> int:
    """支持二十以内的常见中文数字（资格等级最多到十级）。"""
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if "百" in text:
        left, _, right = text.partition("百")
        hundreds = _CN_DIGITS.get(left, 1) if left else 1
        return hundreds * 100
    return 0


def normalize_section_no(value: object) -> str:
    """把 '1'、'01'、'G1'、'一标段'、'标段一' 等统一为 '1'。"""
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", "", raw).casefold()
    if not text:
        return ""
    text = text.replace("第", "").replace("标段", "")
    for prefix in ("section", "bid", "tender", "lot"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text in _CN_DIGITS:
        return str(_CN_DIGITS[text])
    match = re.fullmatch(r"(?:g|lot)?0*(\d+)", text)
    if match:
        return str(int(match.group(1)))
    match = re.fullmatch(r"0*(\d+)(?:号|#)", text)
    if match:
        return str(int(match.group(1)))
    return text


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return None
    # 支持 YYYY-MM-DD、YYYY/MM/DD、YYYY-MM-DD HH:MM 及 ISO 形式
    text = text.replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _attr(el, *names: str) -> str:
    for name in names:
        value = el.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _level_from_text(text: str) -> int | None:
    match = re.search(rf"([{_CN_NUM_CHARS}]+|\d+)\s*级", text)
    if not match:
        return None
    raw = re.sub(r"\s+", "", match.group(1))
    try:
        return parse_level(raw)
    except ValueError:
        return None


def _category_from_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).strip()
    text = re.sub(r"（替代资质）|\(替代资质\)|（替代）|\(替代\)", "", text)
    # 去掉括号中的等级要求，例如“（二级及以上）”
    text = re.sub(
        rf"[（(]\s*([{_CN_NUM_CHARS}]+|\d+)\s*级"
        rf"(?:\s*及以上|\s*以上)?\s*[）)]",
        "",
        text,
    )
    # 去掉“二级及以上”“贰级”等结尾的等级要求
    text = re.sub(
        rf"([{_CN_NUM_CHARS}]+|\d+)\s*级(?:\s*及以上|\s*以上)?\s*$",
        "",
        text,
    )
    text = re.sub(r"[（(].*?[）)]\s*$", "", text)
    return text.strip(" ，,。.;；:：")


# ---------------------------------------------------------------------------
# HTML 解析
# ---------------------------------------------------------------------------
def _parse_candidate(el) -> CandidateCondition:
    text = el.get_text(" ", strip=True)
    category = _attr(
        el,
        "data-category",
        "data-qualification-category",
        "data-cert-category",
        "data-name",
    )
    level_raw = _attr(
        el,
        "data-level",
        "data-min-level",
        "data-grade",
        "data-rank",
        "data-cert-level",
    )
    if not category:
        category = _category_from_text(text)
    if level_raw:
        try:
            level = parse_level(level_raw)
        except ValueError as exc:
            raise QualificationDataError(
                f"资格条件候选等级无效: {level_raw!r}"
            ) from exc
    else:
        level = _level_from_text(text)
    if not category or level is None:
        raise QualificationDataError(
            f"资格条件候选缺少类别或等级: {text or el.get('data-category') or '未知候选'}"
        )
    return CandidateCondition(category=category, level=level, text=text)


def _parse_group(row) -> QualificationGroup:
    name = _attr(row, "data-group", "data-group-name", "data-condition-group")
    if not name:
        name_el = row.select_one(".group-name, [data-field='资格条件组']")
        if name_el is not None:
            name = name_el.get_text(" ", strip=True)
    if not name:
        cells = row.select("th, td")
        if cells:
            name = cells[0].get_text(" ", strip=True)
    clause = _attr(row, "data-clause", "data-clause-source", "data-source")
    if not clause:
        clause_el = row.select_one(".clause-source, [data-field='条款来源']")
        if clause_el is not None:
            clause = clause_el.get_text(" ", strip=True)

    candidate_els = row.select(
        "span.candidate, .candidate, [data-category], [data-qualification-category]"
    )
    if not candidate_els:
        # 兼容没有辅助 class/属性的简单表格：取包含“X级”的单元格作为候选
        candidate_els = [
            cell
            for cell in row.select("td")
            if _level_from_text(cell.get_text(" ", strip=True)) is not None
        ]
    # 若整行本身就是候选（没有独立候选元素），也允许 data-category 在 tr 上
    if not candidate_els and _attr(
        row, "data-category", "data-qualification-category"
    ):
        candidate_els = [row]
    candidates = [_parse_candidate(el) for el in candidate_els]
    if not candidates:
        raise QualificationDataError(f"资格条件组“{name or '未命名'}”缺少候选条件")
    return QualificationGroup(name=name or "资格条件", clause_source=clause, candidates=candidates)


def _section_from_table(table) -> QualificationSection:
    section_no = _attr(
        table,
        "data-section",
        "data-section-no",
        "data-section-number",
        "data-bid-section",
    )
    section_name = _attr(table, "data-section-name", "data-bid-section-name")
    caption = table.select_one("caption")
    if not section_name and caption is not None:
        section_name = caption.get_text(" ", strip=True)
    if not section_no and caption is not None:
        # caption 例如“一标段（SZ-2026-001-1）”
        section_no = caption.get_text(" ", strip=True)
    if not section_no:
        section_no = section_name
    if not section_no:
        raise QualificationDataError("资格表缺少标段号（data-section）")

    rows = table.select("tr.qual-group, tr[data-group], tr[data-condition-group]")
    if not rows:
        rows = [
            tr
            for tr in table.select("tbody tr")
            if tr.select(".candidate, [data-category]")
            or _attr(tr, "data-category", "data-qualification-category")
        ]
    groups = [_parse_group(row) for row in rows]
    if not groups:
        raise QualificationDataError(f"标段 {section_no} 的资格条件表为空")

    # 某些站点会用“每个候选一行”的写法（同一个 data-group 重复出现），
    # 这里按组名合并，避免把同一组拆成多个必须同时满足的组。
    merged: dict[str, QualificationGroup] = {}
    for group in groups:
        key = normalize_category(group.name) or group.name
        existing = merged.get(key)
        if existing is None:
            merged[key] = QualificationGroup(
                name=group.name,
                clause_source=group.clause_source,
                candidates=list(group.candidates),
            )
        else:
            existing.candidates.extend(group.candidates)
            if not existing.clause_source:
                existing.clause_source = group.clause_source
    groups = list(merged.values())

    return QualificationSection(
        section_no=normalize_section_no(section_no),
        section_name=(section_name or section_no).strip(),
        groups=groups,
    )


def parse_qualification_sections(html_text: str) -> list[QualificationSection]:
    """解析详情页中的全部标段资格表；没有资格表时返回空列表。"""
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.select("table.qualification-table, table[data-section]")
    sections: list[QualificationSection] = []
    for table in tables:
        parsed = _section_from_table(table)
        existing = next(
            (s for s in sections if s.section_no == parsed.section_no), None
        )
        if existing is None:
            sections.append(parsed)
        else:
            # 同一标段的资格条件可能拆在多张表中
            known = {normalize_category(g.name) for g in existing.groups}
            for group in parsed.groups:
                if normalize_category(group.name) in known:
                    target = next(
                        g
                        for g in existing.groups
                        if normalize_category(g.name)
                        == normalize_category(group.name)
                    )
                    target.candidates.extend(group.candidates)
                else:
                    existing.groups.append(group)
                    known.add(normalize_category(group.name))
    return sections


def find_section(
    sections: Iterable[QualificationSection], section_no: object
) -> QualificationSection | None:
    wanted = normalize_section_no(section_no)
    for section in sections:
        if section.section_no == wanted:
            return section
        if normalize_section_no(section.section_name) == wanted:
            return section
    return None


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def _certificate_level_text(level: int) -> str:
    return f"{level}级"


def evaluate_section(
    section: QualificationSection,
    certificates: list[CertificateInfo],
    deadline: date | datetime,
) -> SectionEvaluation:
    """逐组判定；同组任一候选满足即可，全部组满足才算满足。"""
    deadline_date = deadline.date() if isinstance(deadline, datetime) else deadline
    group_results: list[GroupEvaluation] = []
    unmet: list[dict] = []

    for group in section.groups:
        matched_candidate: CandidateCondition | None = None
        matched_certificate: CertificateInfo | None = None
        issues: list[str] = []

        for cert in certificates:
            for candidate in group.candidates:
                if normalize_category(cert.category) != normalize_category(
                    candidate.category
                ):
                    continue
                if cert.level > candidate.level:
                    issues.append(
                        f"证书“{cert.category}”等级为{cert.level}级，"
                        f"要求{candidate.level}级及以上（数字越小等级越高）"
                    )
                    continue
                if cert.valid_until < deadline_date:
                    issues.append(
                        f"证书“{cert.category}”有效期至{cert.valid_until.isoformat()}，"
                        f"早于投标截止日期{deadline_date.isoformat()}"
                    )
                    continue
                matched_candidate = candidate
                matched_certificate = cert
                break
            if matched_candidate is not None:
                break

        if matched_candidate is not None:
            group_results.append(
                GroupEvaluation(
                    group=group,
                    satisfied=True,
                    matched_candidate=matched_candidate,
                    matched_certificate=matched_certificate,
                )
            )
            continue

        if issues:
            reason = "；".join(dict.fromkeys(issues))
        else:
            reason = "企业证书类别未覆盖本组任一候选条件"
        requirement = "；".join(
            c.text or f"{c.category}{_certificate_level_text(c.level)}及以上"
            for c in group.candidates
        )
        unmet.append(
            {
                "group": group.name,
                "clause_source": group.clause_source,
                "requirement": f"满足任一：{requirement}",
                "candidates": [
                    {
                        "category": c.category,
                        "level": c.level,
                        "level_text": _certificate_level_text(c.level),
                        "text": c.text,
                    }
                    for c in group.candidates
                ],
                "reason": reason,
            }
        )
        group_results.append(
            GroupEvaluation(group=group, satisfied=False, reason=reason)
        )

    return SectionEvaluation(
        satisfied=all(g.satisfied for g in group_results),
        group_results=group_results,
        unmet_conditions=unmet,
    )
