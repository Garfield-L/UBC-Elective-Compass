"""Offline tests for the optional persistent global interaction counter."""

from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import requests
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import api  # noqa: E402
from src.interaction_counter import (  # noqa: E402
    INITIAL_INTERACTION_TOTAL,
    INTERACTION_COUNTER_KEY,
    InteractionCounterUnavailable,
    UpstashRedisInteractionCounter,
)


class AtomicFakeCounterStore:
    """Thread-safe fake used to prove API behavior without a real Upstash DB."""

    def __init__(self, *, failing: bool = False) -> None:
        self.total: int | None = None
        self.failing = failing
        self.lock = threading.Lock()

    def _initialized_total(self) -> int:
        if self.total is None:
            self.total = INITIAL_INTERACTION_TOTAL
        return self.total

    def get_total(self) -> int:
        with self.lock:
            if self.failing:
                raise InteractionCounterUnavailable("offline")
            return self._initialized_total()

    def increment(self) -> int:
        with self.lock:
            if self.failing:
                raise InteractionCounterUnavailable("offline")
            self.total = self._initialized_total() + 1
            return self.total


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class RedisCommandFake:
    """Small Redis-command fake that atomically models SET NX, GET, and INCR."""

    def __init__(self) -> None:
        self.value: int | None = None
        self.commands: list[list[str | int]] = []
        self.lock = threading.Lock()

    def post(self, _url: str, *, json: list[str | int], **_kwargs: object) -> FakeResponse:
        with self.lock:
            self.commands.append(json)
            if json[:1] == ["SET"]:
                if self.value is None:
                    self.value = int(json[2])
                    return FakeResponse({"result": "OK"})
                return FakeResponse({"result": None})
            if json[:1] == ["GET"]:
                return FakeResponse({"result": self.value})
            if json[:1] == ["INCR"]:
                assert self.value is not None
                self.value += 1
                return FakeResponse({"result": self.value})
        raise AssertionError(f"Unexpected command: {json}")


class FailingHttpClient:
    def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
        raise requests.ConnectionError("offline")


class InteractionCounterStoreTests(unittest.TestCase):
    def test_upstash_store_seeds_once_and_uses_atomic_increment(self) -> None:
        http = RedisCommandFake()
        store = UpstashRedisInteractionCounter("https://redis.example.test/", "test-token", http_client=http)

        self.assertEqual(store.get_total(), 100)
        self.assertEqual(store.increment(), 101)
        self.assertEqual(store.increment(), 102)
        self.assertEqual(http.value, 102)
        self.assertTrue(all(command[:2] == ["SET", INTERACTION_COUNTER_KEY] for command in http.commands[::2]))
        self.assertEqual(http.commands[0], ["SET", INTERACTION_COUNTER_KEY, 100, "NX"])

    def test_concurrent_increments_do_not_lose_updates(self) -> None:
        http = RedisCommandFake()
        store = UpstashRedisInteractionCounter("https://redis.example.test", "test-token", http_client=http)

        with ThreadPoolExecutor(max_workers=12) as executor:
            totals = list(executor.map(lambda _value: store.increment(), range(30)))

        self.assertEqual(sorted(totals), list(range(101, 131)))
        self.assertEqual(store.get_total(), 130)

    def test_storage_transport_failure_is_a_noncritical_domain_error(self) -> None:
        store = UpstashRedisInteractionCounter("https://redis.example.test", "test-token", http_client=FailingHttpClient())
        with self.assertRaises(InteractionCounterUnavailable):
            store.get_total()


class InteractionCounterApiTests(unittest.TestCase):
    def test_stats_start_at_100_increment_by_one_and_reject_invalid_requests(self) -> None:
        fake_store = AtomicFakeCounterStore()
        with patch.object(api, "interaction_store_from_environment", return_value=fake_store):
            with TestClient(api.app) as client:
                self.assertEqual(client.get("/stats/interactions").json(), {"total_interactions": 100})
                self.assertEqual(client.get("/stats/interactions").json(), {"total_interactions": 100})
                for event_type, expected_total in (("visit", 101), ("search", 102), ("save", 103)):
                    response = client.post("/stats/interactions", json={"event_type": event_type})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"total_interactions": expected_total})
                self.assertEqual(client.post("/stats/interactions", json={"event_type": "filter"}).status_code, 422)
                self.assertEqual(
                    client.post("/stats/interactions", json={"event_type": "visit", "increment": 1000}).status_code,
                    422,
                )

    def test_missing_or_failed_stats_storage_does_not_break_course_endpoints(self) -> None:
        with patch.object(api, "interaction_store_from_environment", return_value=None):
            with TestClient(api.app) as client:
                self.assertEqual(client.get("/stats/interactions").status_code, 503)
                self.assertEqual(client.post("/stats/interactions", json={"event_type": "visit"}).status_code, 503)
                self.assertEqual(client.get("/health").json()["course_count"], 5722)
                self.assertEqual(client.post("/courses/search", json={"query": "CPSC"}).status_code, 200)

        with patch.object(api, "interaction_store_from_environment", return_value=AtomicFakeCounterStore(failing=True)):
            with TestClient(api.app) as client:
                self.assertEqual(client.get("/stats/interactions").status_code, 503)
                self.assertEqual(client.post("/courses/search", json={"query": "LAW"}).status_code, 200)


class FrontendInteractionAuditTests(unittest.TestCase):
    def test_counted_actions_are_explicit_and_non_counted_paths_do_not_record_events(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        app_js = (root / "frontend" / "app.js").read_text(encoding="utf-8")

        self.assertIn("Total number of interactions: <strong>—</strong>", html)
        self.assertEqual(app_js.count('recordInteraction("visit")'), 1)
        self.assertIn('if (query) recordInteraction("search");', app_js)
        self.assertIn("elements.searchButton.addEventListener(\"click\", submitCourseSearch);", app_js)
        self.assertIn("submitCourseSearch();", app_js)
        self.assertIn('if (!wasSaved) recordInteraction("save");', app_js)
        self.assertNotIn('loadSuggestions(query);\n  recordInteraction', app_js)
        self.assertNotIn('loadCourses({ append: true });\nrecordInteraction', app_js)

    def test_signature_is_header_only_and_does_not_record_interactions(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        css = (root / "frontend" / "styles.css").read_text(encoding="utf-8")
        app_js = (root / "frontend" / "app.js").read_text(encoding="utf-8")

        self.assertNotIn("UBC course exploration", html)
        self.assertEqual(html.count(">x_y<"), 1)
        self.assertIn('id="signatureButton"', html)
        self.assertNotIn("class=\"signature\"", html)
        self.assertNotIn(".signature {", css)
        self.assertIn("function popDeveloperSignature()", app_js)
        self.assertIn("function createSignatureParticles()", app_js)
        self.assertIn("signaturePopActive", app_js)
        self.assertIn("prefers-reduced-motion: reduce", css)
        pop_start = app_js.index("function popDeveloperSignature()")
        pop_end = app_js.index("function getSavedCourses()", pop_start)
        self.assertNotIn("recordInteraction", app_js[pop_start:pop_end])

    def test_description_toggles_only_for_overflowing_meaningful_text_without_counting(self) -> None:
        root = Path(__file__).resolve().parents[1]
        css = (root / "frontend" / "styles.css").read_text(encoding="utf-8")
        app_js = (root / "frontend" / "app.js").read_text(encoding="utf-8")

        self.assertIn("-webkit-line-clamp: 3", css)
        self.assertIn(".course-description.is-expanded", css)
        self.assertIn("function refreshDescriptionToggles(container)", app_js)
        self.assertIn("description.scrollHeight > description.clientHeight + 1", app_js)
        self.assertIn("data-has-description='true'", app_js)
        self.assertIn("course.description.trim().length > 0", app_js)
        self.assertIn('descriptionToggle.setAttribute("aria-controls", description.id)', app_js)
        self.assertIn('descriptionToggle.setAttribute("aria-expanded", "false")', app_js)
        self.assertIn('toggle.setAttribute("aria-expanded", String(!isExpanded))', app_js)
        self.assertIn("elements.courseResults.addEventListener(\"click\", toggleCourseDescription);", app_js)
        self.assertIn("elements.savedCourses.addEventListener(\"click\", toggleCourseDescription);", app_js)
        toggle_start = app_js.index("function toggleCourseDescription(event)")
        toggle_end = app_js.index("function courseCard(course)", toggle_start)
        self.assertNotIn("recordInteraction", app_js[toggle_start:toggle_end])


if __name__ == "__main__":
    unittest.main()
