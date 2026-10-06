import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DrillLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.safety = Actor("safety-1", "safety")
        self.field = Actor("field-1", "field")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.admin, kind, data)

    def act(self, entity, action, data=None, actor=None, version=None):
        return self.service.transition(
            actor or self.admin, entity["id"], action, data or {}, version
        )

    def start_drill(self, name="季度演练", area="M-01"):
        drill = self.create(
            "drill",
            {"name": name, "area_code": area, "planned_at": "2026-10-06T09:00:00Z"},
        )
        return self.act(drill, "start")
    def book(self, drill, target, action, data=None, actor=None):
        return self.service.book_drill_action(
            actor or self.admin, drill["id"], target["id"], action, data or {}
        )

    def test_drill_actions_leave_real_equipment_untouched(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        passage = self.create("passage", {"from_location": "M-01", "to_location": "S-02", "width_m": 3})
        refuge = self.create("refuge", {"location_code": "M-01", "capacity": 10})

        drill = self.start_drill()
        self.book(drill, vent, "stop", {"reason": "drill"})
        self.book(drill, passage, "block", {})
        self.book(drill, refuge, "occupy", {"occupants": 4})

        # Real entities remain exactly as they were before the drill.
        self.assertEqual(self.service.get(vent["id"])["status"], "running")
        self.assertEqual(self.service.get(passage["id"])["status"], "open")
        self.assertEqual(self.service.get(refuge["id"])["status"], "available")

        # Ledger projections follow the simulated state machine.
        ledger = self.service.drill_ledger(drill["id"])
        self.assertEqual([e["data"]["to_status"] for e in ledger["entries"]],
                         ["stopped", "blocked", "occupied"])
        self.assertFalse(ledger["settled"])

    def test_drill_bookings_chain_on_projected_status(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        drill = self.start_drill()
        self.book(drill, vent, "degrade", {"reason": "drill"})
        # restore is legal from degraded, validated against the projected view
        self.book(drill, vent, "restore", {"tested_at": "2026-10-06T09:10:00Z"})
        # stop again after restored projection
        self.book(drill, vent, "stop", {"reason": "drill"})
        ledger = self.service.drill_ledger(drill["id"])
        self.assertEqual(
            [(e["data"]["from_status"], e["data"]["to_status"]) for e in ledger["entries"]],
            [("running", "degraded"), ("degraded", "running"), ("running", "stopped")],
        )
        # impossible transition from the projected status is rejected
        vent2 = self.create("ventilation", {"name": "fan-2", "area_code": "M-01", "capacity": 100})
        self.book(drill, vent2, "stop", {"reason": "drill"})
        with self.assertRaises(InvalidTransition):
            self.book(drill, vent2, "stop", {"reason": "drill again"})

    def test_refuge_occupancy_over_capacity_is_rejected(self):
        refuge = self.create("refuge", {"location_code": "M-01", "capacity": 5})
        drill = self.start_drill()
        # exactly at capacity is allowed
        self.book(drill, refuge, "occupy", {"occupants": 5})
        self.book(drill, refuge, "release", {})
        # a single intake larger than the rated capacity is refused
        with self.assertRaises(ConflictError):
            self.book(drill, refuge, "occupy", {"occupants": 6})
        # within capacity is booked against the ledger, real refuge untouched
        self.book(drill, refuge, "occupy", {"occupants": 4})
        self.assertEqual(self.service.get(refuge["id"])["status"], "available")

    def test_real_action_on_drill_reserved_equipment_is_blocked(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        drill = self.start_drill()
        self.book(drill, vent, "stop", {"reason": "drill"})
        with self.assertRaises(ConflictError):
            self.act(vent, "stop", {"reason": "real"})
        # equipment not used by the drill is unaffected
        other = self.create("ventilation", {"name": "fan-2", "area_code": "M-02", "capacity": 100})
        self.assertEqual(self.act(other, "stop", {"reason": "real"})["status"], "stopped")

    def test_normal_drill_completion_settles_ledger(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        drill = self.start_drill()
        self.book(drill, vent, "stop", {"reason": "drill"})
        drill = self.act(drill, "complete", {"summary": "演练正常结束"})
        self.assertEqual(drill["status"], "closed")
        ledger = self.service.drill_ledger(drill["id"])
        self.assertTrue(all(e["status"] == "settled" for e in ledger["entries"]))
        self.assertTrue(ledger["settled"])
        # equipment was never really touched
        self.assertEqual(self.service.get(vent["id"])["status"], "running")

    def test_real_event_takes_over_and_voids_conflicting_entries(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        passage = self.create("passage", {"from_location": "M-01", "to_location": "S-02", "width_m": 3})
        drill = self.start_drill()
        self.book(drill, vent, "stop", {"reason": "drill"})
        self.book(drill, passage, "block", {})

        incident = self.create(
            "incident", {"area_code": "M-01", "severity": "critical", "summary": "真实瓦斯事故"}
        )
        # hand shared equipment over to the real event
        drill = self.act(drill, "handover", {"incident_id": incident["id"]})
        self.assertEqual(drill["status"], "interrupted")

        # reality matches the drill projection (really stopped) -> entry survives
        vent = self.act(vent, "stop", {"reason": "real emergency"})
        ledger = self.service.drill_ledger(drill["id"])
        vent_entries = [e for e in ledger["entries"] if e["data"]["target_id"] == vent["id"]]
        self.assertEqual(vent_entries[0]["status"], "projected")

        # reality diverges: drill said blocked, the real event restricts -> voided
        passage = self.act(passage, "restrict", {"reason": "real evacuation route"})
        ledger = self.service.drill_ledger(drill["id"])
        passage_entries = [e for e in ledger["entries"] if e["data"]["target_id"] == passage["id"]]
        self.assertEqual(passage_entries[0]["status"], "voided")

        # while any ledger entry is still open, the incident cannot close
        incident = self.act(incident, "begin_evacuation")
        incident = self.act(incident, "search")
        incident = self.act(incident, "stabilize")
        incident = self.act(incident, "recover")
        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "all clear"})

    def test_reopen_recomputes_against_real_state_and_lists_mismatches(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        refuge = self.create("refuge", {"location_code": "M-01", "capacity": 8})
        drill = self.start_drill()
        self.book(drill, vent, "stop", {"reason": "drill"})
        self.book(drill, refuge, "occupy", {"occupants": 2})
        incident = self.create(
            "incident", {"area_code": "M-01", "severity": "high", "summary": "真实事故"}
        )
        drill = self.act(drill, "handover", {"incident_id": incident["id"]})

        # fan: real stop matches projection (kept), then real restore diverges
        # and voids the drill entry immediately
        self.act(vent, "stop", {"reason": "real"})
        self.act(vent, "restore", {"tested_at": "2026-10-06T10:00:00Z"})
        # refuge is really occupied too -> projection still matches
        self.act(refuge, "occupy", {"occupants": 2})

        drill = self.act(drill, "reopen")
        self.assertEqual(drill["status"], "reconciling")
        ledger = self.service.drill_ledger(drill["id"])
        vent_entries = [e for e in ledger["entries"] if e["data"]["target_id"] == vent["id"]]
        refuge_entries = [e for e in ledger["entries"] if e["data"]["target_id"] == refuge["id"]]
        # a projection already voided by the real-event update is not revived
        self.assertEqual([e["status"] for e in vent_entries], ["voided"])
        # matching projection survives recompute and settles on close
        self.assertEqual([e["status"] for e in refuge_entries], ["projected"])
        self.assertEqual(ledger["discrepancies"], [])

        # nothing to confirm; matching projection settles automatically
        drill = self.act(drill, "complete", {"summary": "演练收尾完成"})
        final = self.service.drill_ledger(drill["id"])
        self.assertTrue(final["settled"])
        self.assertEqual({e["status"] for e in final["entries"]}, {"voided", "settled"})

    def test_reopen_flags_untouched_projection_as_mismatch(self):
        # drill booked the fan stopped, the real event never touched it:
        # reopen-recompute must flag the drift and wait for confirmation
        vent = self.create("ventilation", {"name": "fan-2", "area_code": "M-02", "capacity": 100})
        drill = self.start_drill(name="二次演练", area="M-02")
        self.book(drill, vent, "stop", {"reason": "drill-2"})
        incident = self.create(
            "incident", {"area_code": "M-02", "severity": "high", "summary": "又一起"}
        )
        drill = self.act(drill, "handover", {"incident_id": incident["id"]})
        drill = self.act(drill, "reopen")

        ledger = self.service.drill_ledger(drill["id"])
        entry = ledger["entries"][0]
        self.assertEqual(entry["status"], "mismatch")
        self.assertEqual(ledger["discrepancies"][0]["projected_status"], "stopped")
        self.assertEqual(ledger["discrepancies"][0]["real_status"], "running")

        with self.assertRaises(ConflictError):
            self.act(drill, "complete", {"summary": "done"})

        self.act(entry, "confirm", {"note": "风机演练期间未真实停运，确认以现场为准"})
        drill = self.act(drill, "complete", {"summary": "演练收尾完成"})
        self.assertTrue(self.service.drill_ledger(drill["id"])["settled"])

    def test_reopen_ignores_superseded_projections(self):
        # occupy then release in the drill: the current projection is
        # available; the historical occupy entry must not be flagged.
        refuge = self.create("refuge", {"location_code": "M-01", "capacity": 8})
        drill = self.start_drill()
        self.book(drill, refuge, "occupy", {"occupants": 3})
        self.book(drill, refuge, "release", {})
        incident = self.create(
            "incident", {"area_code": "M-01", "severity": "low", "summary": "x"}
        )
        drill = self.act(drill, "handover", {"incident_id": incident["id"]})
        drill = self.act(drill, "reopen")
        ledger = self.service.drill_ledger(drill["id"])
        self.assertEqual(ledger["discrepancies"], [])
        statuses = sorted(e["status"] for e in ledger["entries"])
        self.assertEqual(statuses, ["projected", "projected"])

    def test_handover_requires_open_incident(self):
        drill = self.start_drill()
        incident = self.create(
            "incident", {"area_code": "M-01", "severity": "low", "summary": "x"}
        )
        incident = self.act(incident, "begin_evacuation")
        incident = self.act(incident, "search")
        incident = self.act(incident, "stabilize")
        incident = self.act(incident, "recover")
        incident = self.act(incident, "close", {"summary": "done"})
        with self.assertRaises(ValidationError):
            self.act(drill, "handover", {"incident_id": incident["id"]})

    def test_bookings_only_while_drill_active(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        drill = self.create(
            "drill",
            {"name": "未来演练", "area_code": "M-01", "planned_at": "2026-10-07T09:00:00Z"},
        )
        with self.assertRaises(InvalidTransition):
            self.book(drill, vent, "stop", {"reason": "drill"})

    def test_ledger_entries_cannot_be_created_generically(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "drill_entry", {"drill_id": "x"})

    def test_drill_create_validation_and_roles(self):
        with self.assertRaises(ValidationError):
            self.create("drill", {"name": "", "area_code": "M-01", "planned_at": "bad"})
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.field,
                "drill",
                {"name": "演练", "area_code": "M-01", "planned_at": "2026-10-06T09:00:00Z"},
            )

    def test_drill_respects_action_roles_and_required_fields(self):
        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        drill = self.start_drill()
        # viewer cannot book anything
        with self.assertRaises(PermissionDenied):
            self.service.book_drill_action(
                Actor("v", "viewer"), drill["id"], vent["id"], "stop", {"reason": "x"}
            )
        # degrade requires a reason
        with self.assertRaises(ValidationError):
            self.book(drill, vent, "degrade", {})


if __name__ == "__main__":
    unittest.main()
