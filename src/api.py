"""FastAPI endpoints for the Version 1 UBC Course Finder catalog."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .filter_courses import ALLOWED_INTEREST_CATEGORIES
from .search_courses import ranked_courses


DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "ubc_courses_full_final.json"
LOCAL_DEVELOPMENT_ORIGINS = (
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://127.0.0.1:5501",
    "http://localhost:5501",
)


def configured_allowed_origins(environment_value: str | None = None) -> list[str]:
    """Combine safe local origins with comma-separated production origins.

    Render can provide ``ALLOWED_ORIGINS`` after the Vercel URL is known, for
    example ``https://example.vercel.app,https://www.example.vercel.app``.
    Wildcard CORS is deliberately rejected so production origins remain
    explicit.
    """
    raw_origins = os.getenv("ALLOWED_ORIGINS", "") if environment_value is None else environment_value
    production_origins = [origin.strip().rstrip("/") for origin in raw_origins.split(",") if origin.strip()]
    if "*" in production_origins:
        raise ValueError("ALLOWED_ORIGINS must list explicit origins; '*' is not allowed")
    return list(dict.fromkeys((*LOCAL_DEVELOPMENT_ORIGINS, *production_origins)))


class HealthResponse(BaseModel):
    """Confirm that startup loaded the reusable course catalog."""

    status: str
    course_count: int


class InterestsResponse(BaseModel):
    """List the single source-of-truth Phase 2 interest categories."""

    interests: list[str]


class CourseSearchRequest(BaseModel):
    """The supported Version 1 filters and page controls."""

    query: str = ""
    interests: list[str] = Field(default_factory=list)
    higher_level: bool = False
    high_gpa: bool = False
    sort_by: Literal["course_code", "highest_average"] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class CourseResult(BaseModel):
    """Course fields useful to the later frontend, plus temporary match metadata."""

    course_code: str
    subject: str
    course_number: int
    title: str
    description: str | None = None
    credits: int | float | None = None
    credits_raw: str | None = None
    level: int
    faculty_school: str | None = None
    source_url: str
    interest_tags: list[str]
    latest_available_average: float | None = None
    grade_session: str | None = None
    grade_reported_students: int | None = None
    grade_status: str | None = None
    matched_interests: list[str]
    matched_interest_count: int


class CourseSearchResponse(BaseModel):
    """A page of already-filtered, already-sorted course records."""

    total_results: int
    returned_results: int
    limit: int
    offset: int
    results: list[CourseResult]


def load_course_dataset(path: Path = DATASET_PATH) -> tuple[dict[str, Any], ...]:
    """Load and lightly validate the final JSON catalog once at application startup."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not load final course dataset from {path}: {error}") from error
    if not isinstance(value, list) or not all(isinstance(course, dict) for course in value):
        raise RuntimeError(f"Final course dataset must be a JSON list of course records: {path}")
    return tuple(value)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Store the loaded catalog for reuse by every request."""
    app.state.courses = load_course_dataset()
    yield


app = FastAPI(
    title="UBC Course Finder API",
    version="1.0.0",
    description="Version 1 course filtering over the local UBC undergraduate catalog.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def loaded_courses(request: Request) -> Sequence[dict[str, Any]]:
    """Retrieve the startup-loaded catalog without rereading the JSON file."""
    return request.app.state.courses


@app.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Return startup health and the actual catalog size."""
    return HealthResponse(status="ok", course_count=len(loaded_courses(request)))


@app.get("/interests", response_model=InterestsResponse)
def interests() -> InterestsResponse:
    """Return the exact approved categories reused by the Phase 2 engine."""
    return InterestsResponse(interests=list(ALLOWED_INTEREST_CATEGORIES))


@app.post("/courses/search", response_model=CourseSearchResponse)
def search_courses(request: Request, search: CourseSearchRequest) -> CourseSearchResponse:
    """Use Phase 2 filtering plus real code search before paginating globally."""
    try:
        matching_courses = ranked_courses(
            loaded_courses(request),
            query=search.query,
            interests=search.interests,
            higher_level=search.higher_level,
            high_gpa=search.high_gpa,
            sort_by=search.sort_by,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    page = matching_courses[search.offset : search.offset + search.limit]
    return CourseSearchResponse(
        total_results=len(matching_courses),
        returned_results=len(page),
        limit=search.limit,
        offset=search.offset,
        results=page,
    )
