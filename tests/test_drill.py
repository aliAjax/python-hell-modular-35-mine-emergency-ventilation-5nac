import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DrillLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("drill-tester", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None, version=None):
        return self.service.transition(self.actor, entity["id"], action, data or {}, version)

    def test_drill_action_posts_to_ledger_and_real_equipment_unchanged(self):
        drill = self.create("drill", {"name": "emergency drill", "area_code": "M-01"})
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        entry = self.service.book_drill_action(self.actor, drill["id"], vent["id"], "stop")
        self.assertEqual(entry["status"], "booked")
        self.assertEqual(entry["effect"], {"status": "stopped"})
        self.assertEqual(entry["snapshot_status"], "running")
        # real equipment must stay as it was before the drill
        real = self.service.get(vent["id"])
        self.assertEqual(real["status"], "running")
        # drill tracks the borrowed equipment
        drill_after = self.service.get(drill["id"])
        self.assertEqual(drill_after["data"]["booked_count"], 1)

    def test_capacity_overrun_is_rejected(self):
        drill = self.create("drill", {"name": "refuge drill", "area_code": "M-01"})
        refuge = self.create("refuge", {"location_code": "R-1", "capacity": 10})
        self.service.book_drill_action(self.actor, drill["id"], refuge["id"], "occupy", {"count": 6})
        with self.assertRaises(ConflictError):
            self.service.book_drill_action(self.actor, drill["id"], refuge["id"], "occupy", {"count": 5})
        # exactly at capacity is allowed
        entry = self.service.book_drill_action(self.actor, drill["id"], refuge["id"], "occupy", {"count": 4})
        self.assertEqual(entry["status"], "booked")

    def test_drill_action_on_already_stopped_fan_is_rejected(self):
        drill = self.create("drill", {"name": "drill", "area_code": "M-01"})
        vent = self.create("ventilation", {"name": "fan-2", "area_code": "M-01", "capacity": 100})
        self.act(vent, "stop")
        with self.assertRaises(ConflictError):
            self.service.book_drill_action(self.actor, drill["id"], vent["id"], "stop")

    def test_real_incident_voids_conflicting_ledger_entries(self):
        drill = self.create("drill", {"name": "drill", "area_code": "M-01"})
        vent = self.create("ventilation", {"name": "fan-3", "area_code": "M-01", "capacity": 100})
        entry = self.service.book_drill_action(self.actor, drill["id"], vent["id"], "stop")
        self.assertEqual(entry["status"], "booked")
        # a real incident takes over the shared fan
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "gas leak"})
        self.act(incident, "begin_evacuation")
        self.act(vent, "stop")
        entries = self.service.list_drill_ledger(drill["id"])
        self.assertEqual(entries[0]["status"], "void")

    def test_reconcile_lists_mismatches_without_voiding(self):
        drill = self.create("drill", {"name": "drill", "area_code": "M-01"})
        vent = self.create("ventilation", {"name": "fan-4", "area_code": "M-01", "capacity": 100})
        entry = self.service.book_drill_action(self.actor, drill["id"], vent["id"], "stop")
        # real state changes outside the normal flow
        self.service.repository.update_entity(vent["id"], vent["version"], "stopped", vent["data"])
        report = self.service.reconcile_drill(self.actor, drill["id"])
        self.assertEqual(len(report["mismatches"]), 1)
        self.assertEqual(report["mismatches"][0]["id"], entry["id"])
        # on-demand reconcile lists but does not void
        self.assertEqual(report["mismatches"][0]["status"], "booked")

    def test_settle_requires_confirmation_and_releases_entries(self):
        drill = self.create("drill", {"name": "drill", "area_code": "M-01"})
        vent = self.create("ventilation", {"name": "fan-5", "area_code": "M-01", "capacity": 100})
        self.service.book_drill_action(self.actor, drill["id"], vent["id"], "stop")
        # real state changes outside the normal flow
        self.service.repository.update_entity(vent["id"], vent["version"], "stopped", vent["data"])
        with self.assertRaises(ConflictError):
            self.service.settle_drill(self.actor, drill["id"])
        settled = self.service.settle_drill(self.actor, drill["id"], confirm=True)
        self.assertEqual(settled["status"], "settled")
        entries = self.service.list_drill_ledger(drill["id"])
        statuses = {e["status"] for e in entries}
        self.assertEqual(statuses, {"void"})

    def test_incident_cannot_close_while_drill_unsettled(self):
        drill = self.create("drill", {"name": "drill", "area_code": "M-01"})
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "x"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        vent = self.create("ventilation", {"name": "fan-6", "area_code": "M-01", "capacity": 100})
        self.service.book_drill_action(self.actor, drill["id"], vent["id"], "stop")
        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "done"})
        # after the drill is settled, the incident can close
        self.service.settle_drill(self.actor, drill["id"])
        incident = self.act(incident, "close", {"summary": "done"})
        self.assertEqual(incident["status"], "closed")

    def test_incident_can_close_when_drill_in_other_area(self):
        drill = self.create("drill", {"name": "drill", "area_code": "M-02"})
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "x"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        vent = self.create("ventilation", {"name": "fan-7", "area_code": "M-02", "capacity": 100})
        self.service.book_drill_action(self.actor, drill["id"], vent["id"], "stop")
        incident = self.act(incident, "close", {"summary": "done"})
        self.assertEqual(incident["status"], "closed")


if __name__ == "__main__":
    unittest.main()
