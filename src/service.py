import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .rules import DRILL_ENTRY_KIND, DRILL_KIND, DRILL_SHARED_KINDS, RuleEngine, drill_effective_status


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
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        kind = self.rules.normalize_kind(entity["kind"])
        # During an active drill, shared equipment is owned by the drill
        # ledger: real actions are refused until the drill hands it over.
        if kind in DRILL_SHARED_KINDS:
            self._guard_active_drill(entity)
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
        if kind in DRILL_SHARED_KINDS:
            self._void_conflicting_ledger_entries(actor, updated)
        if kind == DRILL_KIND and action in ("handover", "reopen", "complete", "abort"):
            self._apply_drill_side_effects(actor, updated, action)
        return updated

    def _guard_active_drill(self, entity):
        for drill in self.repository.list_entities(kind=DRILL_KIND, status="active"):
            entries = self._drill_entries(drill["id"])
            if any(
                entry["status"] != "voided"
                and entry["data"].get("target_id") == entity["id"]
                for entry in entries
            ):
                raise ConflictError(
                    "%s is reserved by active drill %s; hand it over before a real action"
                    % (entity["id"], drill["id"])
                )

    def _drill_entries(self, drill_id):
        return [
            entry
            for entry in self.repository.list_entities(kind=DRILL_ENTRY_KIND)
            if entry["data"].get("drill_id") == drill_id
        ]

    def _live_entries_for(self, drill_id, target_id):
        return [
            entry
            for entry in self._drill_entries(drill_id)
            if entry["status"] != "voided" and entry["data"].get("target_id") == target_id
        ]

    def _force_status(self, actor, entry, status, detail):
        """System-driven ledger entry transition (void/settle), audited."""
        updated = self.repository.update_entity(
            entry["id"],
            entry["version"],
            status,
            dict(entry["data"]),
        )
        self.audit.record(
            entry["id"],
            actor,
            "system:" + detail["reason"],
            entry["status"],
            status,
            detail,
        )
        return updated

    def _void_conflicting_ledger_entries(self, actor, entity):
        """Real equipment changed under interrupted/reconciling drills.

        As soon as reality diverges from the drill's latest projection, that
        projection is voided; matching projections stay on the books and are
        re-checked against real state when the drill reopens.
        """
        drills = self.repository.list_entities(kind=DRILL_KIND)
        for drill in drills:
            if drill["status"] not in ("interrupted", "reconciling"):
                continue
            live = sorted(
                self._live_entries_for(drill["id"], entity["id"]),
                key=lambda item: item["data"].get("seq", 0),
            )
            if not live:
                continue
            latest = live[-1]
            projected_status = latest["data"].get("to_status")
            if projected_status != entity["status"]:
                self._force_status(
                    actor,
                    latest,
                    "voided",
                    {
                        "reason": "real_event_update",
                        "drill_id": drill["id"],
                        "target_id": entity["id"],
                        "projected_status": projected_status,
                        "real_status": entity["status"],
                    },
                )

    def _apply_drill_side_effects(self, actor, drill, action):
        if action == "complete":
            for entry in self._drill_entries(drill["id"]):
                if entry["status"] in ("projected", "confirmed"):
                    self._force_status(
                        actor,
                        entry,
                        "settled",
                        {"reason": "drill_closed", "drill_id": drill["id"]},
                    )
        elif action == "abort":
            for entry in self._drill_entries(drill["id"]):
                if entry["status"] in ("projected", "mismatch", "confirmed"):
                    self._force_status(
                        actor,
                        entry,
                        "voided",
                        {"reason": "drill_aborted", "drill_id": drill["id"]},
                    )
        elif action == "reopen":
            self._reconcile_drill(actor, drill)

    def _reconcile_drill(self, actor, drill):
        """Reopen against real state: flag the current projection per target.

        Only each target's latest live ledger entry represents what the drill
        currently claims; superseded entries are history and settle as-is.
        """
        entries = self._drill_entries(drill["id"])
        latest_by_target = {}
        for entry in entries:
            if entry["status"] != "projected":
                continue
            target_id = entry["data"]["target_id"]
            if target_id not in latest_by_target or entry["data"].get("seq", 0) > latest_by_target[target_id]["data"].get("seq", 0):
                latest_by_target[target_id] = entry
        for target_id, entry in latest_by_target.items():
            target = self.repository.get_entity(target_id)
            if target and entry["data"].get("to_status") != target["status"]:
                self.repository.update_entity(
                    entry["id"],
                    entry["version"],
                    "mismatch",
                    dict(entry["data"]),
                )
                self.audit.record(
                    entry["id"],
                    actor,
                    "system:reopen_mismatch",
                    "projected",
                    "mismatch",
                    {
                        "reason": "reopen_recompute",
                        "drill_id": drill["id"],
                        "projected_status": entry["data"].get("to_status"),
                        "real_status": target["status"],
                    },
                )

    def book_drill_action(self, actor, drill_id, target_id, action, data=None):
        """Book a shared-equipment action onto the drill ledger only."""
        if not target_id:
            raise ValidationError("target_id is required")
        drill = self.repository.get_entity(drill_id)
        if not drill or self.rules.normalize_kind(drill["kind"]) != DRILL_KIND:
            raise NotFoundError("drill not found: " + drill_id)
        if drill["status"] != "active":
            raise InvalidTransition("drill %s is not active (status %s)" % (drill_id, drill["status"]))
        target = self.repository.get_entity(target_id)
        if not target:
            raise NotFoundError("target entity not found: " + target_id)
        target_entries = self._drill_entries(drill_id)
        next_status, patch, virtual_status = self.rules.validate_drill_booking(
            actor, target, action, dict(data or {}), target_entries
        )
        seq = max((entry["data"].get("seq", 0) for entry in target_entries), default=0) + 1
        payload = {
            "drill_id": drill_id,
            "target_id": target_id,
            "target_kind": self.rules.normalize_kind(target["kind"]),
            "action": action,
            "from_status": virtual_status,
            "to_status": next_status,
            "patch": patch,
            "seq": seq,
            "booked_by": actor.user_id,
        }
        entry_id = "drill-entry-" + uuid4().hex[:16]
        entry = self.repository.create_entity(
            entry_id, DRILL_ENTRY_KIND, "projected", payload, actor.user_id
        )
        self.audit.record(
            entry_id,
            actor,
            "drill_book:" + action,
            None,
            "projected",
            {"drill_id": drill_id, "target_id": target_id, "patch": patch},
        )
        return entry

    def drill_ledger(self, drill_id):
        drill = self.repository.get_entity(drill_id)
        if not drill or self.rules.normalize_kind(drill["kind"]) != DRILL_KIND:
            raise NotFoundError("drill not found: " + drill_id)
        entries = sorted(self._drill_entries(drill_id), key=lambda item: item["data"].get("seq", 0))
        discrepancies = []
        for entry in entries:
            if entry["status"] != "mismatch":
                continue
            target = self.repository.get_entity(entry["data"]["target_id"])
            discrepancies.append({
                "entry_id": entry["id"],
                "target_id": entry["data"]["target_id"],
                "projected_status": entry["data"].get("to_status"),
                "real_status": target["status"] if target else None,
            })
        return {
            "drill": drill,
            "entries": entries,
            "discrepancies": discrepancies,
            "settled": not any(e["status"] in ("projected", "mismatch") for e in entries),
        }

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
