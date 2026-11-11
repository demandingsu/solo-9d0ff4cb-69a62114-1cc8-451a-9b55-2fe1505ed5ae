"""端到端测试：用线程内静态 HTTP 服务器托管生成的示例公告站，
通过 FastAPI TestClient 走完整的采集/查询/失败保留流程。
"""
from __future__ import annotations

import functools
import http.server
import importlib.util
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo_site"

# 加载示例数据生成器（脚本名带连字符，用 importlib）
spec = importlib.util.spec_from_file_location(
    "generate_demo_site", ROOT / "scripts" / "generate_demo_site.py"
)
gen = importlib.util.module_from_spec(spec)
sys.modules["generate_demo_site"] = gen
spec.loader.exec_module(gen)


# ---------------------------------------------------------------------------
# 静态文件服务器（支持可选的故障注入）
# ---------------------------------------------------------------------------
class FlakyHandler(http.server.SimpleHTTPRequestHandler):
    # 类属性：需要返回 404 的路径集合
    fail_paths: set[str] = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self):  # noqa: N802
        if self.path in self.fail_paths:
            self.send_error(404, "injected failure")
            return
        return super().do_GET()

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture(scope="session")
def demo_server():
    handler = functools.partial(FlakyHandler)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}", httpd, FlakyHandler
    httpd.shutdown()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个测试使用独立临时数据库
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "test.db"))
    from app import config, database
    importlib.reload(config)
    import app.main as main
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, main, database


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def collect(client, base):
    resp = client.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def project_map(payload):
    return {p["project_name"]: p for p in payload["projects"]}


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------
def test_full_collection_and_deadline_resolution(client, demo_server):
    c, main, _ = client
    base, _, _ = demo_server
    payload = collect(c, base)
    assert payload["collected"] == 10

    projects = project_map(payload)
    assert set(projects) == {a.project_name for a in gen.ANNOUNCEMENTS}

    # A：更正延期生效
    a = projects["滨河路市政改造工程"]
    assert a["current_deadline"] == "2026-11-25 14:30"
    assert a["deadline_source_code"] == "SZ-2026-001-G1"
    assert a["deadline_source_url"].endswith("SZ-2026-001-G1.html")
    assert a["is_expired"] is False

    # B：两次更正，以发布时间最新的第二次为准（不能受页面顺序影响）
    b = projects["城北污水处理厂扩建工程"]
    assert b["current_deadline"] == "2026-11-30 10:00"
    assert b["deadline_source_code"] == "SZ-2026-002-G2"

    # C：无更正、已过期
    cproj = projects["东风桥加固维修工程"]
    assert cproj["current_deadline"] == "2026-10-30 17:00"
    assert cproj["deadline_source_code"] == "SZ-2026-003"
    assert cproj["is_expired"] is True

    # D：最新更正未给截止时间，沿用上一份带时间的公告（原公告）
    d = projects["高新区雨污分流管网工程"]
    assert d["current_deadline"] == "2026-11-28 09:30"
    assert d["deadline_source_code"] == "SZ-2026-004"

    # E：原公告在最后一页、更正公告在第一页（跨页乱序）
    e = projects["湖滨公园配套市政道路工程"]
    assert e["current_deadline"] == "2026-11-20 15:00"
    assert e["deadline_source_code"] == "SZ-2026-005-G1"


def test_order_independence():
    """打乱输入顺序，归并结果必须一致。"""
    from app.merge import merge_projects

    rows = [
        {
            "code": a.code,
            "title": a.title,
            "kind": a.kind,
            "project_name": a.project_name,
            "published_at": a.published_at,
            "deadline": a.deadline,
            "corrects_code": a.corrects_code,
            "detail_url": f"http://x/{a.code}.html",
        }
        for a in gen.ANNOUNCEMENTS
    ]
    baseline = merge_projects(rows)
    shuffled = list(reversed(rows))
    import random

    random.Random(42).shuffle(shuffled)
    assert merge_projects(shuffled) == baseline


def test_duplicate_collection_creates_no_duplicates(client, demo_server):
    c, main, database = client
    base, _, _ = demo_server
    collect(c, base)
    collect(c, base)  # 重复采集

    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM announcements"
        ).fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert total == 10
    assert distinct == 10
    assert runs == 2

    # 查询接口仍然是 5 个项目
    resp = c.get("/api/projects")
    assert resp.status_code == 200
    assert len(resp.json()) == 5


def test_detail_failure_keeps_last_successful_result(client, demo_server):
    c, main, database = client
    base, httpd, handler = demo_server

    # 首次成功采集
    first = collect(c, base)
    assert first["collected"] == 10

    # 注入故障：某个详情页 404
    handler.fail_paths = {"/SZ-2026-002-G2.html"}
    try:
        resp = c.post(
            "/api/collect", json={"list_url": f"{base}/list_1.html"}
        )
        assert resp.status_code == 422
        assert "SZ-2026-002-G2" in resp.json()["detail"] or "HTTP 404" in resp.json()["detail"]
    finally:
        handler.fail_paths = set()

    # 上次成功采集的完整结果必须保留
    resp = c.get("/api/projects")
    projects = {p["project_name"]: p for p in resp.json()}
    assert len(projects) == 5
    assert (
        projects["城北污水处理厂扩建工程"]["deadline_source_code"]
        == "SZ-2026-002-G2"
    )

    # 失败被记录，且不影响后续恢复采集
    runs = c.get("/api/collections").json()
    statuses = [r["status"] for r in runs]
    assert "failed" in statuses and "success" in statuses

    again = collect(c, base)
    assert again["collected"] == 10


def test_list_page_failure_returns_error(client, demo_server):
    c, main, database = client
    base, httpd, handler = demo_server
    collect(c, base)

    handler.fail_paths = {"/list_2.html"}
    try:
        resp = c.post(
            "/api/collect", json={"list_url": f"{base}/list_1.html"}
        )
        assert resp.status_code == 422
        assert "list_2" in resp.json()["detail"]
    finally:
        handler.fail_paths = set()

    # 数据仍是上次成功的 10 条
    with database.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 10


def test_correction_referencing_missing_original_rejected():
    from app.merge import MergeError, merge_projects

    rows = [
        {
            "code": "X-1", "title": "t", "kind": "correction",
            "project_name": "P", "published_at": "2026-11-01 10:00",
            "deadline": "2026-12-01 10:00", "corrects_code": "X-0",
            "detail_url": "http://x/X-1.html",
        }
    ]
    with pytest.raises(MergeError, match="X-0"):
        merge_projects(rows)


def test_expired_flag_uses_fixed_now(client, demo_server):
    """注入固定当前时间验证过期判定边界。"""
    from app import service

    c, main, _ = client
    base, _, _ = demo_server
    collect(c, base)

    fixed_now = datetime(2026, 11, 26, 0, 0)
    projects = {p["project_name"]: p for p in service.query_projects(fixed_now)}
    # A 的截止 11-25 已过期；B 的 11-30 未过期
    assert projects["滨河路市政改造工程"]["is_expired"] is True
    assert projects["城北污水处理厂扩建工程"]["is_expired"] is False


# ---------------------------------------------------------------------------
# 资格核验 API
# ---------------------------------------------------------------------------
def verify(c, code, section, certificates):
    return c.post(
        "/api/qualifications/verify",
        json={
            "announcement_code": code,
            "section_no": section,
            "certificates": certificates,
        },
    )


CONSTRUCTION = {
    "category": "市政公用工程施工总承包",
    "level": 2,
    "valid_until": "2026-11-25",
}
MANAGER = {
    "category": "市政公用工程专业注册建造师",
    "level": 1,
    "valid_until": "2026-11-25",
}


def test_qualification_satisfied_across_sections(client, demo_server):
    c, main, _ = client
    base, _, _ = demo_server
    collect(c, base)

    # 一标段：施工 + 项目经理两组均满足；截止日期来自更正归并（11-25）
    resp = verify(c, "SZ-2026-001", "1", [CONSTRUCTION, MANAGER])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "satisfied"
    assert body["verdict"] == "满足"
    assert body["can_satisfy"] is True
    assert body["section_name"] == "一标段"
    assert body["matched_section_no"] == "1"
    assert body["deadline"] == "2026-11-25 14:30"
    assert body["deadline_source_code"] == "SZ-2026-001-G1"
    assert body["unmet_conditions"] == []

    # 标段号支持“一标段”与“G1”写法
    assert verify(c, "SZ-2026-001", "一标段", [CONSTRUCTION, MANAGER]).json()[
        "matched_section_no"
    ] == "1"
    assert verify(c, "SZ-2026-001", "G1", [CONSTRUCTION, MANAGER]).json()[
        "matched_section_no"
    ] == "1"

    # 二标段与一标段的资格条件不同，但同一组证书仍满足
    sec2 = verify(c, "SZ-2026-001", "2", [CONSTRUCTION, MANAGER])
    assert sec2.status_code == 200
    assert sec2.json()["status"] == "satisfied"
    assert sec2.json()["section_name"] == "二标段"


def test_qualification_alternative_certificate_satisfies_group(client, demo_server):
    c, main, _ = client
    base, _, _ = demo_server
    collect(c, base)

    # 001 一标段的施工资质组允许“公路工程施工总承包”作为替代资质
    alt = {
        "category": "公路工程施工总承包",
        "level": 2,
        "valid_until": "2026-12-31",
    }
    body = verify(c, "SZ-2026-001", "1", [alt, MANAGER]).json()
    assert body["status"] == "satisfied"
    matched = {
        g["group"]: g for g in body["groups"]
    }["施工资质"]["matched_candidate"]
    assert matched["category"] == "公路工程施工总承包"

    # 002 的施工资质组允许“水利水电工程施工总承包”替代
    water = {
        "category": "水利水电工程施工总承包",
        "level": 2,
        "valid_until": "2026-12-31",
    }
    body = verify(c, "SZ-2026-002", "1", [water]).json()
    assert body["status"] == "satisfied"


def test_qualification_all_groups_required_and_reports_clause(client, demo_server):
    c, main, _ = client
    base, _, _ = demo_server
    collect(c, base)

    # 只有施工资质证书，缺项目经理资格 -> 未满足，并返回条款来源
    body = verify(c, "SZ-2026-001", "1", [CONSTRUCTION]).json()
    assert body["status"] == "unsatisfied"
    assert body["verdict"] == "不满足"
    assert body["can_satisfy"] is False
    assert len(body["unmet_conditions"]) == 1
    unmet = body["unmet_conditions"][0]
    assert unmet["group"] == "项目经理资格"
    assert unmet["clause_source"] == "招标公告第三章 资格审查办法 2.2"
    assert "任一" in unmet["requirement"]

    # 等级数字越大越低：要求二级及以上时，三级不满足；一级满足
    low = {**CONSTRUCTION, "level": 3}
    body = verify(c, "SZ-2026-001", "1", [low, MANAGER]).json()
    assert body["status"] == "unsatisfied"
    assert body["unmet_conditions"][0]["group"] == "施工资质"
    assert "数字越小等级越高" in body["unmet_conditions"][0]["reason"]

    high = {**CONSTRUCTION, "level": 1}
    body = verify(c, "SZ-2026-001", "1", [high, MANAGER]).json()
    assert body["status"] == "satisfied"


def test_qualification_validity_must_not_precede_deadline(client, demo_server):
    c, main, _ = client
    base, _, _ = demo_server
    collect(c, base)

    # 有效期恰好等于截止日期（同一天）允许
    exact = {**CONSTRUCTION, "valid_until": "2026-11-25"}
    body = verify(c, "SZ-2026-001", "1", [exact, MANAGER]).json()
    assert body["status"] == "satisfied"

    # 早于更正后的截止日期 2026-11-25 -> 不满足（不能退回使用原公告 11-05）
    expired = {**CONSTRUCTION, "valid_until": "2026-11-20"}
    body = verify(c, "SZ-2026-001", "1", [expired, MANAGER]).json()
    assert body["status"] == "unsatisfied"
    unmet = body["unmet_conditions"][0]
    assert unmet["group"] == "施工资质"
    assert "早于投标截止日期" in unmet["reason"]


def test_qualification_undetermined_on_missing_conditions_or_detail_failure(
    client, demo_server
):
    c, main, _ = client
    base, httpd, handler = demo_server
    collect(c, base)

    # 标段不存在 -> 无法判定
    body = verify(c, "SZ-2026-001", "9", [CONSTRUCTION, MANAGER]).json()
    assert body["status"] == "undetermined"
    assert body["verdict"] == "无法判定"
    assert body["can_satisfy"] is None
    assert "未找到标段" in body["reason"]

    # 该原公告没有资格表 -> 无法判定（不能当作不满足）
    body = verify(c, "SZ-2026-003", "1", [CONSTRUCTION]).json()
    assert body["status"] == "undetermined"
    assert body["can_satisfy"] is None
    assert "未提供资格条件表" in body["reason"]

    # 详情页读取失败 -> 无法判定
    handler.fail_paths = {"/SZ-2026-001.html"}
    try:
        resp = verify(c, "SZ-2026-001", "1", [CONSTRUCTION, MANAGER])
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "undetermined"
        assert body["can_satisfy"] is None
        assert "详情页读取失败" in body["reason"]
        # 即使无法判定，已能确定的更正后截止时间仍返回
        assert body["deadline"] == "2026-11-25 14:30"
    finally:
        handler.fail_paths = set()


def test_qualification_request_validation_and_unknown_code(client, demo_server):
    c, main, _ = client
    base, _, _ = demo_server
    collect(c, base)

    # 未采集的公告编号
    resp = verify(c, "SZ-9999-999", "1", [CONSTRUCTION])
    assert resp.status_code == 404

    # 更正公告不能作为核验依据
    resp = verify(c, "SZ-2026-001-G1", "1", [CONSTRUCTION])
    assert resp.status_code == 404

    # 顶层单证书简写也支持（002 只有一个资格组）
    resp = c.post(
        "/api/qualifications/verify",
        json={
            "original_code": "SZ-2026-002",
            "section": "1",
            "category": "水利水电工程施工总承包",
            "level": "二级",
            "valid_until": "2026-12-31",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "satisfied"


def test_qualification_missing_candidate_fields_is_undetermined():
    """资格候选缺类别/等级时属于“条件缺项”，不能误判为不满足。"""
    from app.qualification import QualificationDataError, parse_qualification_sections

    bad = """
    <table class="qualification-table" data-section="1">
      <tr class="qual-group" data-group="施工资质" data-clause="X">
        <td class="group-name">施工资质</td>
        <td><span class="candidate" data-category="市政公用工程施工总承包">施工总承包</span></td>
      </tr>
    </table>
    """
    with pytest.raises(QualificationDataError):
        parse_qualification_sections(bad)

