"""Feedback metadata endurance, isolated from models, HTTP and production data."""
from collections import OrderedDict
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


class FeedbackCapacityTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "isolated_feedback", Path(__file__).parents[1] / "backend/core/processing_feedback.py")
        self.feedback = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.feedback)
        self.now = 0.0
        clock = patch.object(self.feedback.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def test_sustained_metadata_traffic_across_retention_window(self):
        # Metadata only: this is NOT an HTTP/inference throughput benchmark.
        for second in range(660):
            self.now = float(second)
            for event in range(150):
                key = f"{second}:{event}"
                self.assertTrue(self.feedback.reserve(key), f"capacity exhausted at {second}s")
                self.feedback.complete(key, "saved")
                self.assertEqual(self.feedback.lookup(key), "saved")
        self.assertLessEqual(len(self.feedback._events), self.feedback.MAX_EVENTS)
        self.assertIsNone(self.feedback.lookup("0:0"))
        self.assertEqual(self.feedback.lookup("659:149"), "saved")

    def test_full_cache_preserves_unexpired_pending_and_completed_events(self):
        self.feedback.MAX_EVENTS = 2
        self.assertTrue(self.feedback.reserve("pending"))
        self.assertTrue(self.feedback.reserve("completed"))
        self.feedback.complete("completed", "saved")
        self.assertFalse(self.feedback.reserve("new"))
        self.assertEqual(self.feedback.lookup("pending"), "pending")
        self.assertEqual(self.feedback.lookup("completed"), "saved")
        self.now = self.feedback.TTL + 1
        self.assertTrue(self.feedback.reserve("new"))

    def test_completion_refreshes_expiry_order_without_resurrecting_expired_work(self):
        self.feedback.reserve("first")
        self.now = 1
        self.feedback.reserve("second")
        self.now = 2
        self.feedback.complete("first", "saved")
        self.now = self.feedback.TTL + 1
        self.assertIsNone(self.feedback.lookup("second"))
        self.assertEqual(self.feedback.lookup("first"), "saved")
        self.now += 2
        self.feedback.complete("first", "saved")
        self.assertIsNone(self.feedback.lookup("first"))

    def test_lookup_does_not_scan_all_unexpired_records(self):
        class NoFullScan(OrderedDict):
            def items(self):
                raise AssertionError("lookups must not scan the entire cache")
        self.feedback._events = NoFullScan()
        self.feedback.reserve("event")
        self.assertEqual(self.feedback.lookup("event"), "pending")
