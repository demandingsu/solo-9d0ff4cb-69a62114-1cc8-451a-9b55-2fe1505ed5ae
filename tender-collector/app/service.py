"""采集编排服务：抓取 -> 归并校验 -> 单事务落库；失败时保留上次成功结果。"""
from __future__ import annotations

from datetime import datetime

from . import database
from .merge import EffectiveDeadline, MergeError, merge_projects
from .qualification import (
    QualificationDataError,
    _parse_date,
    evaluate_section,
    find_section,
    parse_qualification_sections,
)
from .scraper import CollectionError, collect, fetch_page

TIME_FORMAT = "%Y-%m-%d %H:%M"


class ServiceError(Exception):
    """对外统一的采集错误（带可读原因）。"""


class QualificationNotFound(ServiceError):
    """请求的原公告未采集，或编号不是原公告。"""


class QualificationRequestError(ServiceError):
    """资格核验请求无法处理（例如归属数据异常）。"""


def _is_expired(deadline_text: str, now: datetime | None = None) -> bool:
    try:
        deadline = datetime.strptime(deadline_text, TIME_FORMAT)
    except ValueError:
        # 容错：支持带秒或 ISO 形式
        deadline = datetime.fromisoformat(deadline_text)
    return deadline <= (now or datetime.now())


def _to_project(d: EffectiveDeadline, now: datetime | None = None) -> dict:
    return {
        "project_name": d.project_name,
        "current_deadline": d.deadline,
        "deadline_source_code": d.source_code,
        "deadline_source_url": d.source_url,
        "is_expired": _is_expired(d.deadline, now),
    }


def build_projects(rows: list[dict], now: datetime | None = None) -> list[dict]:
    return [_to_project(d, now) for d in merge_projects(rows)]


def run_collection(list_url: str) -> dict:
    started_at = database.utcnow_iso()
    try:
        raw = collect(list_url)
        # 先在内存完成归并校验：数据有问题时不写库
        projects = build_projects(raw)
    except (CollectionError, MergeError) as exc:
        # 记录失败采集；announcements 表完全不动，保留上次成功结果
        with database.get_conn() as conn:
            database.record_run(
                conn,
                source_url=list_url,
                status="failed",
                items_collected=0,
                error=str(exc),
                started_at=started_at,
            )
            conn.commit()
        raise ServiceError(str(exc)) from exc

    # 成功：单事务 UPSERT + 运行记录，保证原子性
    with database.get_conn() as conn:
        try:
            database.upsert_announcements(conn, raw)
            database.record_run(
                conn,
                source_url=list_url,
                status="success",
                items_collected=len(raw),
                error=None,
                started_at=started_at,
            )
            conn.commit()
        except Exception as exc:  # pragma: no cover - 兜底回滚
            conn.rollback()
            raise ServiceError(f"写入数据库失败: {exc}") from exc

    return {"source_url": list_url, "collected": len(raw), "projects": projects}


def query_projects(now: datetime | None = None) -> list[dict]:
    with database.get_conn() as conn:
        rows = [dict(r) for r in database.fetch_all_announcements(conn)]
    if not rows:
        return []
    return build_projects(rows, now)


# ---------------------------------------------------------------------------
# 资格核验
# ---------------------------------------------------------------------------
def _certificate_dict(cert) -> dict:
    return {
        "category": cert.category,
        "level": cert.level,
        "level_text": f"{cert.level}级",
        "valid_until": cert.valid_until.isoformat(),
    }


def _undetermined(
    req,
    reason: str,
    *,
    project_name: str | None = None,
    deadline: str | None = None,
    deadline_source_code: str | None = None,
    deadline_source_url: str | None = None,
    detail_url: str | None = None,
) -> dict:
    return {
        "status": "undetermined",
        "verdict": "无法判定",
        "status_text": "无法判定",
        "can_satisfy": None,
        "qualified": None,
        "satisfied": None,
        "announcement_code": req.announcement_code.strip(),
        "section_no": str(req.section_no),
        "section_name": None,
        "matched_section_no": None,
        "project_name": project_name,
        "deadline": deadline,
        "deadline_source_code": deadline_source_code,
        "deadline_source_url": deadline_source_url,
        "detail_url": detail_url,
        "groups": [],
        "unmet_conditions": [],
        "reason": reason,
    }


def verify_qualification(req) -> dict:
    """核验企业证书是否满足原公告指定标段的全部资格条件。

    * 原公告编号必须已在数据库中且类型为原公告；
    * 截止时间复用采集层的更正归并结果（与 /api/projects 一致）；
    * 资格条件实时从原公告详情页读取；
    * 详情页读取失败、资格表/标段/候选缺项时返回 undetermined，而非误判为不满足。
    """
    code = req.announcement_code.strip()
    with database.get_conn() as conn:
        rows = [dict(r) for r in database.fetch_all_announcements(conn)]

    original = next((r for r in rows if r["code"] == code), None)
    if original is None:
        raise QualificationNotFound(f"原公告 {code} 尚未采集")
    if original["kind"] != "original":
        raise QualificationNotFound(f"公告 {code} 不是原公告，无法作为核验依据")

    # 原公告 + 所有引用它的更正公告 —— 复用现有归并规则得到生效截止时间
    group_rows = [
        r
        for r in rows
        if r["code"] == code
        or (r["kind"] == "correction" and r.get("corrects_code") == code)
    ]
    try:
        effective = merge_projects(group_rows)
    except MergeError as exc:
        return _undetermined(
            req,
            f"无法从已采集数据确定生效投标截止时间: {exc}",
            project_name=original["project_name"],
            detail_url=original["detail_url"],
        )
    if not effective:
        return _undetermined(
            req,
            "已采集数据中没有可用的投标截止时间",
            project_name=original["project_name"],
            detail_url=original["detail_url"],
        )

    deadline_text = effective[0].deadline
    deadline_date = _parse_date(deadline_text)
    common = {
        "project_name": original["project_name"],
        "deadline": deadline_text,
        "deadline_source_code": effective[0].source_code,
        "deadline_source_url": effective[0].source_url,
        "detail_url": original["detail_url"],
    }
    if deadline_date is None:
        return _undetermined(
            req, f"投标截止时间格式无法解析: {deadline_text}", **common
        )

    # 资格条件只存在于原公告详情页，必须实时读取
    try:
        html_text = fetch_page(original["detail_url"])
    except CollectionError as exc:
        return _undetermined(req, f"原公告详情页读取失败: {exc}", **common)

    try:
        sections = parse_qualification_sections(html_text)
    except QualificationDataError as exc:
        return _undetermined(req, f"资格条件缺项: {exc}", **common)
    if not sections:
        return _undetermined(req, "原公告详情页未提供资格条件表", **common)

    section = find_section(sections, req.section_no)
    if section is None:
        known = "、".join(
            f"{s.section_no}({s.section_name})" for s in sections
        )
        return _undetermined(
            req,
            f"未找到标段 {req.section_no} 的资格条件；详情页现有标段: {known}",
            **common,
        )

    evaluation = evaluate_section(
        section, req.certificate_infos(), deadline_date
    )

    groups = []
    for result in evaluation.group_results:
        matched_candidate = None
        if result.matched_candidate is not None:
            matched_candidate = {
                "category": result.matched_candidate.category,
                "level": result.matched_candidate.level,
                "level_text": f"{result.matched_candidate.level}级",
                "text": result.matched_candidate.text,
            }
        groups.append(
            {
                "group": result.group.name,
                "clause_source": result.group.clause_source,
                "satisfied": result.satisfied,
                "reason": result.reason,
                "matched_candidate": matched_candidate,
                "matched_certificate": (
                    _certificate_dict(result.matched_certificate)
                    if result.matched_certificate is not None
                    else None
                ),
            }
        )

    return {
        "status": "satisfied" if evaluation.satisfied else "unsatisfied",
        "verdict": "满足" if evaluation.satisfied else "不满足",
        "status_text": "满足" if evaluation.satisfied else "不满足",
        "can_satisfy": evaluation.satisfied,
        "qualified": evaluation.satisfied,
        "satisfied": evaluation.satisfied,
        "announcement_code": code,
        "section_no": str(req.section_no),
        "section_name": section.section_name,
        "matched_section_no": section.section_no,
        "project_name": original["project_name"],
        "deadline": deadline_text,
        "deadline_source_code": effective[0].source_code,
        "deadline_source_url": effective[0].source_url,
        "detail_url": original["detail_url"],
        "groups": groups,
        "unmet_conditions": evaluation.unmet_conditions,
        "reason": None,
    }
