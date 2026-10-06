from datetime import datetime, timedelta

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _number(value, field):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")


def _validate_worker(data):
    if len(str(data.get("name", "")).strip()) < 2:
        raise ValidationError("worker name is too short")


def _validate_sensor(data):
    gas = _number(data.get("gas_ppm"), "gas_ppm")
    threshold = _number(data.get("threshold_ppm"), "threshold_ppm")
    if gas < 0 or threshold <= 0:
        raise ValidationError("gas readings and thresholds must be positive")
    data["severity"] = "alarm" if gas >= threshold * 1.5 else "warning" if gas >= threshold else "normal"


def _validate_capacity(data, field):
    if _number(data.get(field), field) <= 0:
        raise ValidationError(field + " must be positive")


def _validate_passage(data):
    if _number(data.get("width_m"), "width_m") <= 0:
        raise ValidationError("width_m must be positive")
    if data.get("from_location") == data.get("to_location"):
        raise ValidationError("passage endpoints must differ")


def _validate_incident(data):
    if data.get("severity") not in ("low", "medium", "high", "critical"):
        raise ValidationError("invalid incident severity")


def _validate_task(data, lookup):
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident or incident["status"] in ("closed",):
        raise ValidationError("task requires an open incident")
    if data.get("task_type") not in ("evacuation", "search", "rescue", "ventilation", "medical", "repair"):
        raise ValidationError("invalid task_type")
    key = data.get("dedupe_key")
    for task in _all(lookup, "task"):
        if task["data"].get("dedupe_key") == key and task["status"] not in ("completed", "cancelled"):
            raise ConflictError("active task already exists for dedupe_key: " + str(key))


def _validate_offline(data):
    if not isinstance(data.get("payload"), dict):
        raise ValidationError("offline payload must be an object")
    try:
        datetime.fromisoformat(str(data.get("recorded_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("recorded_at must be ISO-8601")


def _validate_drill(data):
    if not str(data.get("name", "")).strip():
        raise ValidationError("drill name is required")
    if not str(data.get("area_code", "")).strip():
        raise ValidationError("area_code is required")
    try:
        datetime.fromisoformat(str(data.get("planned_at", "")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("planned_at must be ISO-8601")


def _validate_drill_entry_direct(data):
    raise ValidationError("drill ledger entries must be booked via /api/drills/<id>/ledger")


# Kinds of equipment shared between drills and real operations. Actions on these
# during an active drill are only booked against the drill ledger.
DRILL_SHARED_KINDS = ("ventilation", "passage", "refuge")
DRILL_ENTRY_KIND = "drill_entry"
DRILL_KIND = "drill"


def drill_effective_status(entries):
    """Replay non-voided ledger entries in order; None means no live entry."""
    status = None
    for entry in entries:
        if entry["status"] == "voided":
            continue
        status = entry["data"].get("to_status")
    return status


def _refuge_drill_occupancy(entries, target_id):
    total = 0
    status = None
    for entry in entries:
        if entry["status"] == "voided" or entry["data"].get("target_id") != target_id:
            continue
        if entry["data"].get("action") == "occupy":
            status = "occupied"
            total += int(float(entry["data"].get("occupants", 1)))
        elif entry["data"].get("action") == "release":
            status = "available"
            total = 0
    return total, status


def _sensor_alarm(actor, entity, data, lookup):
    if float(entity["data"].get("gas_ppm", 0)) < float(entity["data"].get("threshold_ppm", 1)):
        raise ValidationError("alarm requires a reading at or above threshold")
    return {"acknowledged_by": actor.user_id}


def _complete_task(actor, entity, data, lookup):
    if not str(data.get("result", "")).strip():
        raise ValidationError("result is required")
    return {"completed_by": actor.user_id}


def _close_incident(actor, entity, data, lookup):
    if [w for w in _all(lookup, "worker") if w["status"] in ("missing", "located")]:
        raise ConflictError("cannot close incident while workers are missing or located")
    active_tasks = [t for t in _all(lookup, "task") if t["status"] not in ("completed", "cancelled")]
    if active_tasks:
        raise ConflictError("cannot close incident while tasks remain active")
    if [v for v in _all(lookup, "ventilation") if v["status"] != "running"]:
        raise ConflictError("cannot close incident until ventilation is restored")
    # A drill interrupted by a real event leaves its ledger open; the physical
    # equipment must be handed back and the ledger cleared before closing.
    pending = [
        e for e in _all(lookup, DRILL_ENTRY_KIND)
        if e["status"] in ("projected", "mismatch")
    ]
    if pending:
        raise ConflictError("cannot close incident while drill ledger entries remain unsettled")
    return {"closed_by": actor.user_id}


def _drill_handover(actor, entity, data, lookup):
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident:
        raise ValidationError("incident_id must reference an existing incident")
    if incident["status"] == "closed":
        raise ValidationError("cannot hand over to a closed incident")
    return {}


def _drill_complete(actor, entity, data, lookup):
    entries = _all(lookup, DRILL_ENTRY_KIND)
    drill_id = entity["id"]
    # Projections are settled automatically on close; only entries that
    # failed reopen-recompute must be acknowledged first.
    mismatches = [
        e for e in entries
        if e["data"].get("drill_id") == drill_id and e["status"] == "mismatch"
    ]
    if mismatches:
        raise ConflictError(
            "drill ledger has %s unconfirmed mismatch entr%s"
            % (len(mismatches), "y" if len(mismatches) == 1 else "ies")
        )
    return {}


def _drill_entry_confirm(actor, entity, data, lookup):
    if not str(data.get("note", "")).strip():
        raise ValidationError("note is required to confirm a mismatch")
    return {"confirmed_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "workers": "worker", "sensors": "sensor", "ventilations": "ventilation",
        "passages": "passage", "refuges": "refuge", "incidents": "incident",
        "tasks": "task", "offline-records": "offline_record", "offline_records": "offline_record",
        "drills": "drill", "entries": "drill_entry", "drill_entries": "drill_entry",
        "drill-ledger": "drill_entry", "drill_ledger": "drill_entry",
    }
    INITIAL_STATUS = {
        "worker": "active", "sensor": "normal", "ventilation": "running",
        "passage": "open", "refuge": "available", "incident": "detected",
        "task": "proposed", "offline_record": "merged",
        "drill": "planned", "drill_entry": "projected",
    }
    TRANSITIONS = {
        "worker": {
            "mark_missing": (("active",), "missing"),
            "locate": (("missing",), "located"),
            "evacuate": (("missing", "located"), "evacuated"),
            "rescue": (("missing", "located"), "rescued"),
            "find_safe": (("missing",), "active"),
            "deactivate": (("active",), "inactive"),
        },
        "sensor": {
            "raise_warning": (("normal",), "warning"),
            "raise_alarm": (("normal", "warning"), "alarm"),
            "clear": (("warning", "alarm"), "normal"),
            "mark_faulty": (("normal", "warning", "alarm"), "faulty"),
            "verify_misread": (("faulty",), "normal"),
        },
        "ventilation": {
            "degrade": (("running",), "degraded"),
            "stop": (("running", "degraded"), "stopped"),
            "restore": (("stopped", "degraded"), "running"),
        },
        "passage": {
            "restrict": (("open",), "restricted"),
            "block": (("open", "restricted"), "blocked"),
            "clear": (("blocked", "restricted"), "open"),
        },
        "refuge": {
            "occupy": (("available",), "occupied"),
            "release": (("occupied",), "available"),
            "maintain": (("available",), "maintenance"),
            "reopen": (("maintenance",), "available"),
        },
        "incident": {
            "begin_evacuation": (("detected",), "evacuating"),
            "search": (("evacuating",), "searching"),
            "stabilize": (("searching",), "stabilizing"),
            "recover": (("stabilizing",), "recovering"),
            "close": (("recovering",), "closed"),
            "reopen": (("closed",), "detected"),
        },
        "task": {
            "assign": (("proposed",), "assigned"),
            "accept": (("assigned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
            "cancel": (("proposed", "assigned", "in_progress"), "cancelled"),
        },
        "drill": {
            "start": (("planned",), "active"),
            "handover": (("active",), "interrupted"),
            "reopen": (("interrupted",), "reconciling"),
            "complete": (("active", "reconciling"), "closed"),
            "abort": (("planned", "active", "reconciling"), "aborted"),
        },
        "drill_entry": {
            "confirm": (("mismatch",), "confirmed"),
            "settle": (("projected", "confirmed"), "settled"),
            "void": (("projected", "mismatch"), "voided"),
        },
    }
    CREATE_REQUIRED = {
        "worker": ("name", "location_code", "team"),
        "sensor": ("location_code", "gas_ppm", "threshold_ppm"),
        "ventilation": ("name", "area_code", "capacity"),
        "passage": ("from_location", "to_location", "width_m"),
        "refuge": ("location_code", "capacity"),
        "incident": ("area_code", "severity", "summary"),
        "task": ("incident_id", "task_type", "target", "dedupe_key"),
        "offline_record": ("source_id", "record_id", "recorded_at", "payload"),
        "drill": ("name", "area_code", "planned_at"),
    }
    ACTION_REQUIRED = {
        ("worker", "rescue"): ("incident_id",),
        ("sensor", "mark_faulty"): ("reason",),
        ("ventilation", "restore"): ("tested_at",),
        ("ventilation", "degrade"): ("reason",),
        ("incident", "close"): ("summary",),
        ("task", "complete"): ("result",),
        ("task", "cancel"): ("reason",),
        ("drill", "handover"): ("incident_id",),
        ("drill", "complete"): ("summary",),
        ("drill_entry", "confirm"): ("note",),
    }
    CREATE_ROLES = {
        "worker": ("admin", "safety", "dispatcher"),
        "sensor": ("admin", "safety", "field"),
        "ventilation": ("admin", "safety"),
        "passage": ("admin", "safety", "field"),
        "refuge": ("admin", "safety"),
        "incident": ("admin", "safety", "dispatcher"),
        "task": ("admin", "dispatcher", "safety"),
        "offline_record": ("admin", "safety", "dispatcher", "field"),
        "drill": ("admin", "safety", "dispatcher"),
        "drill_entry": ("admin", "safety"),
    }
    ROLE_ACTIONS = {
        "mark_missing": ("admin", "safety", "dispatcher"),
        "locate": ("admin", "field", "safety"),
        "evacuate": ("admin", "field", "dispatcher"),
        "rescue": ("admin", "field", "safety"),
        "find_safe": ("admin", "field", "safety"),
        "deactivate": ("admin", "safety"),
        "raise_warning": ("admin", "field", "safety"),
        "raise_alarm": ("admin", "field", "safety"),
        "clear": ("admin", "safety"),
        "mark_faulty": ("admin", "safety"),
        "verify_misread": ("admin", "safety"),
        "degrade": ("admin", "safety"),
        "stop": ("admin", "safety"),
        "restore": ("admin", "safety"),
        "restrict": ("admin", "safety", "field"),
        "block": ("admin", "safety", "field"),
        "clear": ("admin", "safety", "field"),
        "occupy": ("admin", "field", "safety"),
        "release": ("admin", "field", "safety"),
        "maintain": ("admin", "safety"),
        "reopen": ("admin", "safety"),
        "begin_evacuation": ("admin", "safety", "dispatcher"),
        "search": ("admin", "safety", "dispatcher"),
        "stabilize": ("admin", "safety", "dispatcher"),
        "recover": ("admin", "safety", "dispatcher"),
        "close": ("admin", "safety"),
        "assign": ("admin", "dispatcher", "safety"),
        "accept": ("admin", "field", "dispatcher"),
        "complete": ("admin", "field", "dispatcher"),
        "cancel": ("admin", "dispatcher", "safety"),
        "start": ("admin", "safety", "dispatcher"),
        "handover": ("admin", "safety", "dispatcher"),
        "reopen": ("admin", "safety", "dispatcher"),
        "abort": ("admin", "safety", "dispatcher"),
        "confirm": ("admin", "safety"),
        "settle": ("admin", "safety"),
        "void": ("admin", "safety"),
    }
    CUSTOM_CREATE = {
        "worker": lambda a, d, l: _validate_worker(d),
        "sensor": lambda a, d, l: _validate_sensor(d),
        "ventilation": lambda a, d, l: _validate_capacity(d, "capacity"),
        "passage": lambda a, d, l: _validate_passage(d),
        "refuge": lambda a, d, l: _validate_capacity(d, "capacity"),
        "incident": lambda a, d, l: _validate_incident(d),
        "task": lambda a, d, l: _validate_task(d, l),
        "offline_record": lambda a, d, l: _validate_offline(d),
        "drill": lambda a, d, l: _validate_drill(d),
        "drill_entry": lambda a, d, l: _validate_drill_entry_direct(d),
    }
    CUSTOM_TRANSITIONS = {
        ("sensor", "raise_alarm"): _sensor_alarm,
        ("incident", "close"): _close_incident,
        ("task", "complete"): _complete_task,
        ("drill", "handover"): _drill_handover,
        ("drill", "complete"): _drill_complete,
        ("drill_entry", "confirm"): _drill_entry_confirm,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def validate_drill_booking(self, actor, target, action, data, target_entries):
        """Dry-run a shared-equipment action against the drill ledger view.

        The real entity is untouched: the transition is validated from the
        status the ledger projects, or from the real status when the ledger
        has no live entry for the target.
        """
        kind = self.normalize_kind(target["kind"])
        if kind not in DRILL_SHARED_KINDS:
            raise InvalidTransition(
                "drill ledger only accepts ventilation, passage and refuge actions"
            )
        ordered = sorted(
            (entry for entry in target_entries if entry["data"].get("target_id") == target["id"]),
            key=lambda entry: entry["data"].get("seq", 0),
        )
        projected = drill_effective_status(ordered)
        virtual_status = projected or target["status"]
        synthetic = {
            "id": target["id"],
            "kind": kind,
            "status": virtual_status,
            "data": dict(target["data"]),
            "version": target["version"],
        }
        next_status, patch = self.validate_transition(
            actor, synthetic, action, dict(data or {}), None
        )
        if kind == "refuge" and action == "occupy":
            try:
                occupants = int(float(patch.get("occupants", 1)))
            except (TypeError, ValueError):
                raise ValidationError("occupants must be numeric")
            if occupants <= 0:
                raise ValidationError("occupants must be positive")
            patch["occupants"] = occupants
            current, state = _refuge_drill_occupancy(ordered, target["id"])
            if state == "available":
                current = 0
            capacity = int(float(target["data"].get("capacity", 0)))
            if current + occupants > capacity:
                raise ConflictError(
                    "drill occupancy would exceed refuge capacity %s (booked %s, requested %s)"
                    % (capacity, current, occupants)
                )
        return next_status, patch, virtual_status
