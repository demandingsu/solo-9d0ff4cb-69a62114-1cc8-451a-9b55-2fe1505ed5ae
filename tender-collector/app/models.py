from __future__ import annotations

from datetime import date, datetime

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

from .qualification import parse_level, _parse_date


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ProjectOut(BaseModel):
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    collected: int
    projects: list[ProjectOut]


class RunOut(BaseModel):
    id: int
    source_url: str
    status: str
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str


# ---------------------------------------------------------------------------
# 资格核验
# ---------------------------------------------------------------------------
class CertificateIn(BaseModel):
    """企业提供的证书：类别、等级、有效期。"""

    category: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "category",
            "cert_category",
            "certificate_category",
            "certificate_type",
            "类别",
        ),
        description="证书类别，例如“市政公用工程施工总承包”",
    )
    level: int = Field(
        ...,
        validation_alias=AliasChoices(
            "level", "grade", "rank", "cert_level", "certificate_level", "等级"
        ),
        description="证书等级数字；同类别下数字越小等级越高",
    )
    valid_until: date = Field(
        ...,
        validation_alias=AliasChoices(
            "valid_until",
            "valid_to",
            "expires_at",
            "expiry_date",
            "expiration_date",
            "validity_end",
            "valid_date",
            "有效期",
        ),
        description="证书有效期（含当日），不得早于投标截止日期",
    )

    @field_validator("level", mode="before")
    @classmethod
    def _coerce_level(cls, value: object) -> int:
        return parse_level(value)

    @field_validator("valid_until", mode="before")
    @classmethod
    def _coerce_valid_until(cls, value: object) -> object:
        parsed = _parse_date(value)
        if parsed is None:
            raise ValueError(f"无法解析证书有效期: {value!r}")
        return parsed


class QualificationVerifyRequest(BaseModel):
    """资格核验请求。

    兼容两种常见写法：`certificates` 数组，或直接在请求顶层给出
    单张证书的 `category` / `level` / `valid_until`。
    """

    announcement_code: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "announcement_code",
            "original_code",
            "original_announcement_code",
            "original_announcement_no",
            "announcement_no",
            "code",
            "原公告编号",
        ),
        description="已采集的原公告编号",
    )
    section_no: str | int = Field(
        ...,
        validation_alias=AliasChoices(
            "section_no",
            "section",
            "section_code",
            "section_number",
            "section_id",
            "bid_section",
            "bid_section_no",
            "tender_section",
            "lot_no",
            "标段号",
        ),
        description="标段号，例如 1、G1、一标段",
    )
    certificates: list[CertificateIn] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "certificates", "certs", "certificate_list", "证书列表"
        ),
    )
    certificate: CertificateIn | None = Field(
        default=None,
        validation_alias=AliasChoices("certificate", "cert"),
    )

    # 单张证书的顶层便捷字段
    category: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "category", "certificate_category", "cert_category"
        ),
        description="证书类别（单证书简写）",
    )
    level: str | int | None = Field(
        default=None,
        validation_alias=AliasChoices("level", "certificate_level", "cert_level"),
        description="证书等级（单证书简写）",
    )
    valid_until: str | date | datetime | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "valid_until", "certificate_valid_until", "expiry_date", "expires_at"
        ),
        description="证书有效期（单证书简写）",
    )

    @model_validator(mode="after")
    def _normalise_certificates(self) -> "QualificationVerifyRequest":
        certs = list(self.certificates or [])
        if self.certificate is not None:
            certs.append(self.certificate)
        if not certs:
            if (
                self.category is not None
                and self.level is not None
                and self.valid_until is not None
            ):
                certs.append(
                    CertificateIn(
                        category=self.category,
                        level=self.level,
                        valid_until=self.valid_until,
                    )
                )
        if not certs:
            raise ValueError(
                "请提供企业证书：certificates 数组，或 category/level/valid_until"
            )
        self.certificates = certs
        return self

    def certificate_infos(self) -> list:
        """转为判定层使用的 CertificateInfo（已完成等级/日期解析）。"""
        from .qualification import CertificateInfo

        return [
            CertificateInfo(
                category=c.category,
                level=c.level,
                valid_until=c.valid_until,
            )
            for c in (self.certificates or [])
        ]


class CandidateOut(BaseModel):
    category: str
    level: int
    level_text: str
    text: str = ""


class GroupResultOut(BaseModel):
    group: str
    clause_source: str
    satisfied: bool
    reason: str | None = None
    matched_candidate: CandidateOut | None = None
    matched_certificate: dict | None = None


class UnmetConditionOut(BaseModel):
    group: str
    clause_source: str
    requirement: str
    candidates: list[CandidateOut]
    reason: str


class QualificationVerifyResponse(BaseModel):
    status: str = Field(description="satisfied / unsatisfied / undetermined")
    verdict: str = Field(description="满足 / 不满足 / 无法判定")
    status_text: str | None = Field(default=None, description="verdict 的别名")
    can_satisfy: bool | None = Field(
        default=None, description="能否满足；无法判定时为 null"
    )
    qualified: bool | None = Field(
        default=None, description="can_satisfy 的别名"
    )
    satisfied: bool | None = Field(
        default=None, description="can_satisfy 的别名"
    )
    announcement_code: str
    section_no: str
    section_name: str | None = None
    matched_section_no: str | None = None
    project_name: str | None = None
    deadline: str | None = None
    deadline_source_code: str | None = None
    deadline_source_url: str | None = None
    detail_url: str | None = None
    groups: list[GroupResultOut] = Field(default_factory=list)
    unmet_conditions: list[UnmetConditionOut] = Field(default_factory=list)
    reason: str | None = Field(default=None, description="无法判定或整体失败的原因")
