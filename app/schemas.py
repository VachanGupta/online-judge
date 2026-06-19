"""Pydantic request/response models for the HTTP API.

These define the API contract and are kept separate from the ORM models so the
wire format and the storage schema can evolve independently. Response models use
``from_attributes`` so they can be built directly from ORM objects.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import CompareMode, SubmissionStatus, Verdict
from app.languages import supported_languages

# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class TestCaseIn(BaseModel):
    input_data: str
    expected_output: str
    is_sample: bool = False
    points: int = Field(default=1, ge=0)


class ProblemCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9\-]*$")
    title: str = Field(min_length=1, max_length=256)
    statement: str = ""
    # Limits default (when omitted) to the server-configured defaults.
    time_limit_ms: int | None = Field(default=None, gt=0, le=60_000)
    memory_limit_mb: int | None = Field(default=None, gt=0, le=4096)
    output_limit_kb: int | None = Field(default=None, gt=0, le=131_072)
    compare_mode: CompareMode = CompareMode.TRIM
    float_tolerance: float = Field(default=1e-6, gt=0)
    test_cases: list[TestCaseIn] = Field(min_length=1)


class SubmissionCreate(BaseModel):
    problem_id: int
    language: str
    source_code: str = Field(min_length=1, max_length=1_000_000)

    @field_validator("language")
    @classmethod
    def _known_language(cls, value: str) -> str:
        if value not in supported_languages():
            raise ValueError(f"unsupported language {value!r}; supported: {supported_languages()}")
        return value


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class ProblemSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    title: str
    time_limit_ms: int
    memory_limit_mb: int
    output_limit_kb: int
    compare_mode: CompareMode
    num_test_cases: int


class TestCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    input_data: str
    expected_output: str
    is_sample: bool
    points: int


class ProblemDetail(ProblemSummary):
    statement: str
    float_tolerance: float
    # Only sample test cases are exposed; hidden tests stay private.
    sample_test_cases: list[TestCaseOut]


class SubmissionCreated(BaseModel):
    id: int
    status: SubmissionStatus


class TestResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    verdict: Verdict
    time_ms: int | None
    cpu_ms: int | None
    memory_kb: int | None
    exit_code: int | None
    signal: int | None
    stderr_snippet: str | None


class SubmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    problem_id: int
    language: str
    status: SubmissionStatus
    verdict: Verdict | None
    compile_output: str | None
    max_time_ms: int | None
    max_memory_kb: int | None
    score: int | None
    max_score: int | None
    error_message: str | None
    created_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    test_results: list[TestResultOut]


class SubmissionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    problem_id: int
    language: str
    status: SubmissionStatus
    verdict: Verdict | None
    created_at: datetime


class LanguagesOut(BaseModel):
    languages: list[str]
