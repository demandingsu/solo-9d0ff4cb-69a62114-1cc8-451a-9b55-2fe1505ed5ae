#!/usr/bin/env python3
"""生成一个本地分页 HTML 示例公告站（市政工程招投标公告）。

生成结果写入 demo_site/，纯静态文件，可用任意 HTTP 服务器托管：
    python3 -m http.server 8001 --directory demo_site

数据特意构造为：
- 列表页按发布时间倒序排列（更正公告排在其引用的原公告之前），
  服务端不能依赖页面顺序来判断截止时间；
- 每个项目的“生效截止时间”由发布时间最新的那份公告（原公告或更正公告）决定；
- 含已过期与未过期两种项目；
- 原公告详情页含资格条件表：SZ-2026-001 有两个标段（跨标段），
  并分别在施工资质组提供公路工程、环保工程替代资质；SZ-2026-002 提供
  水利水电工程施工总承包替代资质，用于资格核验接口示例。
"""
from __future__ import annotations

import html
import os
from dataclasses import dataclass, field
from pathlib import Path

PAGE_SIZE = 4
OUT_DIR = Path(__file__).resolve().parent.parent / "demo_site"


@dataclass
class QualificationCandidate:
    category: str                  # 证书类别
    level: int                     # 门槛等级：数字越小等级越高
    alternative: bool = False      # 是否作为替代资质


@dataclass
class QualificationGroup:
    name: str                      # 资格条件组名称
    clause: str                    # 条款来源
    candidates: list[QualificationCandidate]


@dataclass
class QualificationSection:
    no: str                        # 标段号（规范化为 1、2 ...）
    name: str                      # 标段名称（一标段、二标段 ...）
    groups: list[QualificationGroup]


@dataclass
class Announcement:
    code: str                      # 公告编号
    title: str                     # 公告标题
    kind: str                      # original | correction
    project_name: str              # 项目名称
    published_at: str              # 发布时间 YYYY-MM-DD HH:MM
    deadline: str | None = None    # 投标截止时间（更正公告可为空）
    corrects_code: str | None = None  # 更正公告引用的原公告编号
    reason: str = ""               # 更正原因（仅更正公告）
    sections: list[QualificationSection] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


# ---------------------------------------------------------------------------
# 示例数据：5 个项目，其中 3 个含更正公告，共 10 条公告
# ---------------------------------------------------------------------------
ANNOUNCEMENTS: list[Announcement] = [
    # 项目 A：原公告已过期，更正延期后仍在有效期内
    Announcement(
        code="SZ-2026-001",
        title="滨河路市政改造工程施工招标公告",
        kind="original",
        project_name="滨河路市政改造工程",
        published_at="2026-10-20 09:00",
        deadline="2026-11-05 14:30",
        # 跨标段示例：同一原公告分两个标段，资格审查条件不同
        sections=[
            QualificationSection(
                no="1",
                name="一标段",
                groups=[
                    QualificationGroup(
                        name="施工资质",
                        clause="招标公告第三章 资格审查办法 2.1",
                        candidates=[
                            QualificationCandidate("市政公用工程施工总承包", 2),
                            QualificationCandidate(
                                "公路工程施工总承包", 2, alternative=True
                            ),
                        ],
                    ),
                    QualificationGroup(
                        name="项目经理资格",
                        clause="招标公告第三章 资格审查办法 2.2",
                        candidates=[
                            QualificationCandidate(
                                "市政公用工程专业注册建造师", 1
                            ),
                        ],
                    ),
                ],
            ),
            QualificationSection(
                no="2",
                name="二标段",
                groups=[
                    QualificationGroup(
                        name="施工资质",
                        clause="招标公告第三章 资格审查办法 3.1",
                        candidates=[
                            QualificationCandidate("市政公用工程施工总承包", 2),
                            QualificationCandidate(
                                "环保工程专业承包", 2, alternative=True
                            ),
                        ],
                    ),
                    QualificationGroup(
                        name="项目经理资格",
                        clause="招标公告第三章 资格审查办法 3.2",
                        candidates=[
                            QualificationCandidate(
                                "市政公用工程专业注册建造师", 1
                            ),
                        ],
                    ),
                ],
            ),
        ],
    ),
    Announcement(
        code="SZ-2026-001-G1",
        title="滨河路市政改造工程施工招标更正公告（第一次）",
        kind="correction",
        project_name="滨河路市政改造工程",
        published_at="2026-10-28 16:00",
        deadline="2026-11-25 14:30",
        corrects_code="SZ-2026-001",
        reason="因招标文件图纸调整，投标截止时间顺延。",
    ),
    # 项目 B：两次更正，最终截止时间以发布时间最新的第二次更正为准
    Announcement(
        code="SZ-2026-002",
        title="城北污水处理厂扩建工程招标公告",
        kind="original",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-12 10:30",
        deadline="2026-11-10 09:00",
        # 替代资质示例：施工资质组接受两类证书，满足任一即可
        sections=[
            QualificationSection(
                no="1",
                name="一标段",
                groups=[
                    QualificationGroup(
                        name="施工资质",
                        clause="招标公告第三章 资格审查办法 2.1",
                        candidates=[
                            QualificationCandidate("市政公用工程施工总承包", 1),
                            QualificationCandidate(
                                "水利水电工程施工总承包", 2, alternative=True
                            ),
                        ],
                    ),
                ],
            ),
        ],
    ),
    Announcement(
        code="SZ-2026-002-G1",
        title="城北污水处理厂扩建工程招标更正公告",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-22 15:00",
        deadline="2026-11-18 09:00",
        corrects_code="SZ-2026-002",
        reason="答疑澄清，截止时间调整。",
    ),
    Announcement(
        code="SZ-2026-002-G2",
        title="城北污水处理厂扩建工程招标更正公告（第二次）",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-11-03 11:20",
        deadline="2026-11-30 10:00",
        corrects_code="SZ-2026-002",
        reason="再次延期，以本公告时间为准。",
    ),
    # 项目 C：无更正，且已过期
    Announcement(
        code="SZ-2026-003",
        title="东风桥加固维修工程招标公告",
        kind="original",
        project_name="东风桥加固维修工程",
        published_at="2026-09-15 08:45",
        deadline="2026-10-30 17:00",
    ),
    # 项目 D：更正公告不修改截止时间（deadline 为空），生效时间仍取原公告
    Announcement(
        code="SZ-2026-004",
        title="高新区雨污分流管网工程招标公告",
        kind="original",
        project_name="高新区雨污分流管网工程",
        published_at="2026-10-25 14:00",
        deadline="2026-11-28 09:30",
    ),
    Announcement(
        code="SZ-2026-004-G1",
        title="高新区雨污分流管网工程招标更正公告",
        kind="correction",
        project_name="高新区雨污分流管网工程",
        published_at="2026-11-01 09:10",
        deadline=None,
        corrects_code="SZ-2026-004",
        reason="仅更正联系人电话，投标截止时间不变。",
    ),
    # 项目 E：原公告在最后一个列表页、更正公告在第一页（跨页乱序）
    Announcement(
        code="SZ-2026-005",
        title="湖滨公园配套市政道路工程招标公告",
        kind="original",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-10-08 13:00",
        deadline="2026-11-08 15:00",
    ),
    Announcement(
        code="SZ-2026-005-G1",
        title="湖滨公园配套市政道路工程招标更正公告",
        kind="correction",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-11-05 17:30",
        deadline="2026-11-20 15:00",
        corrects_code="SZ-2026-005",
        reason="工程量清单调整，截止时间顺延。",
    ),
]


_LEVEL_CN = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}


def _level_label(level: int) -> str:
    return f"{_LEVEL_CN.get(level, str(level))}级"


def _qualification_html(a: Announcement) -> str:
    """渲染原公告的资格条件表；更正公告没有资格表。"""
    if not a.sections:
        return ""
    tables: list[str] = []
    for section in a.sections:
        group_rows: list[str] = []
        for group in section.groups:
            candidates = []
            for candidate in group.candidates:
                label = f"{candidate.category}{_level_label(candidate.level)}及以上"
                if candidate.alternative:
                    label += "（替代资质）"
                alt_attr = ' data-alternative="true"' if candidate.alternative else ""
                candidates.append(
                    f'              <span class="candidate" '
                    f'data-category="{html.escape(candidate.category)}" '
                    f'data-level="{candidate.level}"{alt_attr}>'
                    f"{html.escape(label)}</span>"
                )
            group_rows.append(
                f'        <tr class="qual-group" '
                f'data-group="{html.escape(group.name)}" '
                f'data-clause="{html.escape(group.clause)}">\n'
                f'          <td class="group-name">{html.escape(group.name)}</td>\n'
                f'          <td class="clause-source">{html.escape(group.clause)}</td>\n'
                f'          <td class="candidate-list">\n'
                f"{chr(10).join(candidates)}\n"
                f"          </td>\n"
                f"        </tr>"
            )
        tables.append(
            f'    <table class="qualification-table" '
            f'data-section="{html.escape(section.no)}" '
            f'data-section-name="{html.escape(section.name)}">\n'
            f"      <caption>{html.escape(section.name)}资格审查条件</caption>\n"
            f"      <thead>\n"
            f"        <tr><th>资格条件组</th><th>条款来源</th><th>候选条件（满足任一）</th></tr>\n"
            f"      </thead>\n"
            f"      <tbody>\n"
            f"{chr(10).join(group_rows)}\n"
            f"      </tbody>\n"
            f"    </table>"
        )
    return (
        '    <h2 class="qualification-title">标段划分与资格审查条件</h2>\n'
        + "\n".join(tables)
        + "\n"
    )


def _detail_page(a: Announcement) -> str:
    rows = [
        ("公告编号", a.code),
        ("公告类型", "更正公告" if a.kind == "correction" else "招标公告"),
        ("项目名称", a.project_name),
        ("发布时间", a.published_at),
    ]
    if a.deadline is not None:
        rows.append(("投标截止时间", a.deadline))
    if a.kind == "correction":
        rows.append(("原公告编号", a.corrects_code or ""))
        rows.append(("更正事项", a.reason))

    trs = "\n".join(
        f'      <tr><th data-field="{html.escape(label)}">{html.escape(label)}</th>'
        f'<td data-value="{html.escape(label)}">{html.escape(value)}</td></tr>'
        for label, value in rows
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(a.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{html.escape(a.code)}"
           data-kind="{a.kind}"
           data-published-at="{html.escape(a.published_at)}"
           {f'data-corrects="{html.escape(a.corrects_code)}"' if a.corrects_code else ""}>
    <h1 class="detail-title">{html.escape(a.title)}</h1>
    <table class="detail-table">
{trs}
    </table>
{_qualification_html(a)}    <p><a href="list_1.html">返回列表</a></p>
  </article>
</body>
</html>
"""


def _list_page(items: list[Announcement], page: int, total_pages: int) -> str:
    lis = []
    for a in items:
        lis.append(
            f'      <li class="announcement-item" data-code="{html.escape(a.code)}">\n'
            f'        <a class="detail-link" href="{html.escape(a.filename)}">{html.escape(a.title)}</a>\n'
            f'        <span class="published-at">{html.escape(a.published_at)}</span>\n'
            f'      </li>'
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(f'<a class="page-link" href="list_{p}.html">{p}</a>')
    next_link = (
        f'<a class="page-next" href="list_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>市政工程公告站 - 第 {page} 页</title>
</head>
<body>
  <h1>市政工程招投标公告列表（第 {page}/{total_pages} 页）</h1>
  <ul class="announcement-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 列表按发布时间倒序 —— 更正公告天然排在原公告前面，制造页面顺序干扰
    ordered = sorted(ANNOUNCEMENTS, key=lambda a: a.published_at, reverse=True)
    total_pages = (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE

    for a in ordered:
        (OUT_DIR / a.filename).write_text(_detail_page(a), encoding="utf-8")

    for page in range(1, total_pages + 1):
        chunk = ordered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        (OUT_DIR / f"list_{page}.html").write_text(
            _list_page(chunk, page, total_pages), encoding="utf-8"
        )

    (OUT_DIR / "index.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=list_1.html">'
        "<title>公告站</title></head><body>跳转中…</body></html>\n",
        encoding="utf-8",
    )
    print(f"generated {len(ordered)} detail pages, {total_pages} list pages in {OUT_DIR}")


if __name__ == "__main__":
    main()
