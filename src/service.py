import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind in ("incident", "ventilation", "passage", "refuge"):
            self._reconcile_active_drills()
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] in ("incident", "ventilation", "passage", "refuge"):
            self._reconcile_active_drills()
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # Drill ledger (演练账)
    #
    # Drill actions never mutate real equipment. They post to a separate
    # ledger that snapshots the real state at booking time. When a real
    # incident takes over shared equipment, conflicting entries are
    # voided. An incident cannot close while a drill ledger is unsettled.
    # ------------------------------------------------------------------

    DRILL_ACTIONS = {
        "ventilation": ("stop",),
        "passage": ("block",),
        "refuge": ("occupy",),
    }
    DRILL_EFFECTS = {
        "ventilation": ("stopped",),
        "passage": ("blocked",),
        "refuge": ("occupied",),
    }

    def _get_drill(self, drill_id):
        drill = self.repository.get_entity(drill_id)
        if not drill or drill["kind"] != "drill":
            raise NotFoundError("drill not found: " + str(drill_id))
        return drill

    def _detect_mismatches(self, drill_id):
        """Booked entries whose snapshot no longer matches real state."""
        mismatches = []
        for entry in self.repository.list_ledger_entries(drill_id=drill_id, status="booked"):
            real = self.repository.get_entity(entry["target_id"])
            if not real or real["status"] != entry["snapshot_status"]:
                mismatches.append(entry)
        return mismatches

    def _refresh_drill_counts(self, drill_id):
        entries = self.repository.list_ledger_entries(drill_id=drill_id)
        booked = [e for e in entries if e["status"] == "booked"]
        mismatch_count = 0
        for entry in booked:
            real = self.repository.get_entity(entry["target_id"])
            if not real or real["status"] != entry["snapshot_status"]:
                mismatch_count += 1
        drill = self.repository.get_entity(drill_id)
        data = dict(drill["data"])
        data["booked_count"] = len(booked)
        data["mismatch_count"] = mismatch_count
        self.repository.update_entity(drill_id, drill["version"], drill["status"], data)

    def _reconcile_drill(self, drill_id, auto_void=True):
        mismatches = self._detect_mismatches(drill_id)
        voided = []
        if auto_void:
            for entry in mismatches:
                self.repository.update_ledger_entry_status(entry["id"], "void")
                voided.append(entry)
        self._refresh_drill_counts(drill_id)
        entries = self.repository.list_ledger_entries(drill_id=drill_id)
        booked = [e for e in entries if e["status"] == "booked"]
        return {"booked": booked, "voided": voided, "mismatches": mismatches}

    def _reconcile_active_drills(self):
        for drill in self.repository.list_entities(kind="drill", status="active"):
            self._reconcile_drill(drill["id"], auto_void=True)

    def book_drill_action(self, actor, drill_id, target_id, action, data=None):
        data = dict(data or {})
        drill = self._get_drill(drill_id)
        if drill["status"] != "active":
            raise ConflictError("drill is not active: " + drill["status"])
        target = self.repository.get_entity(target_id)
        if not target:
            raise NotFoundError("target not found: " + str(target_id))
        target_kind = target["kind"]
        if target_kind not in self.DRILL_ACTIONS:
            raise ValidationError("drill actions only apply to ventilation, passage, refuge")
        if action not in self.DRILL_ACTIONS[target_kind]:
            raise ValidationError("action %s is not valid for %s" % (action, target_kind))
        real_status = target["status"]
        effect_status = self.DRILL_EFFECTS[target_kind][0]
        if target_kind == "refuge":
            if real_status != "available":
                raise ConflictError(
                    "cannot simulate occupy: refuge is %s in reality" % real_status
                )
            count = int(data.get("count", 1))
            if count <= 0:
                raise ValidationError("count must be positive")
            capacity = float(target["data"].get("capacity", 0))
            booked = self.repository.list_ledger_entries(
                status="booked", target_kind="refuge", target_id=target_id
            )
            occupied = sum(int(e["detail"].get("count", 1)) for e in booked)
            if occupied + count > capacity:
                raise ConflictError(
                    "occupation exceeds approved capacity: %d + %d > %d"
                    % (occupied, count, int(capacity))
                )
        elif real_status not in ("running", "open", "restricted", "degraded"):
            raise ConflictError(
                "cannot simulate %s: %s is %s in reality" % (action, target_kind, real_status)
            )
        entry_id = "ledger-" + str(uuid4())
        detail = dict(data)
        detail["action"] = action
        entry = self.repository.create_ledger_entry(
            entry_id,
            drill_id,
            target_kind,
            target_id,
            action,
            {"status": effect_status},
            real_status,
            "booked",
            detail,
        )
        self._refresh_drill_counts(drill_id)
        self.audit.record(
            drill_id, actor, "book_action", None, "booked",
            {"target": target_id, "action": action, "entry": entry_id},
        )
        return entry

    def reconcile_drill(self, actor, drill_id):
        drill = self._get_drill(drill_id)
        if drill["status"] != "active":
            raise ConflictError("drill is not active: " + drill["status"])
        return self._reconcile_drill(drill_id, auto_void=False)

    def settle_drill(self, actor, drill_id, confirm=False):
        drill = self._get_drill(drill_id)
        if drill["status"] != "active":
            raise ConflictError("drill is not active: " + drill["status"])
        mismatches = self._detect_mismatches(drill_id)
        if mismatches and not confirm:
            raise ConflictError(
                "drill ledger has %d mismatches against real state; "
                "confirm to settle: %s"
                % (len(mismatches), ", ".join(e["id"] for e in mismatches))
            )
        for entry in mismatches:
            self.repository.update_ledger_entry_status(entry["id"], "void")
        for entry in self.repository.list_ledger_entries(drill_id=drill_id, status="booked"):
            self.repository.update_ledger_entry_status(entry["id"], "released")
        data = dict(drill["data"])
        data["booked_count"] = 0
        data["mismatch_count"] = 0
        updated = self.repository.update_entity(drill_id, drill["version"], "settled", data)
        self.audit.record(
            drill_id, actor, "settle", "active", "settled",
            {"voided": [e["id"] for e in mismatches]},
        )
        return updated

    def list_drill_ledger(self, drill_id, status=None):
        self._get_drill(drill_id)
        return self.repository.list_ledger_entries(drill_id=drill_id, status=status)
