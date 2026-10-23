"""Explicit, hash-bound synthetic conditions. Never evidence of real-world facts."""
from dataclasses import dataclass, replace, fields, is_dataclass
from types import MappingProxyType
from datetime import date, datetime
from hashlib import sha256
import json
from .contracts import digest, read_json, jsonable


def condition_digest(value):
    """Same canonical digest as contracts.digest without dataclass deep copies."""
    def default(item):
        if is_dataclass(item):
            return {f.name: getattr(item, f.name) for f in fields(item)}
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError(type(item).__name__)
    return sha256(json.dumps(value, default=default, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class AssumptionContext:
    snapshot_id: str
    registry_hash: str
    rows: object

    @classmethod
    def load(cls, catalogs, registry):
        if registry.get("mode") != "benchmark_assumptions":
            raise ValueError("Explicit benchmark_assumptions registry required")
        entries = registry["assumptions"]
        if len({e["id"] for e in entries}) != len(entries):
            raise ValueError("Duplicate assumption ID")
        rows = {}
        for entry in entries:
            if entry.get("status") != "synthetic_assumption":
                raise ValueError("Assumption is not explicitly synthetic")
            key = (entry["catalog"], entry["object_id"])
            if key in rows:
                raise ValueError("Duplicate assumption object")
            rows[key] = (entry["id"], entry["row_hash"], frozenset(entry["fields"]))
            item = next((i for i in catalogs[entry["catalog"]].items if i.id == entry["object_id"]), None)
            values = jsonable(item) if item else {}
            values.pop("source", None)
            if values != entry.get("values") or not item or item.source.features != entry.get("features"):
                raise ValueError("Assumption values differ from catalog: " + entry["object_id"])
        context = cls(registry["snapshot_id"], digest(registry), MappingProxyType(rows))
        context.verify(catalogs)
        return context

    def verify(self, catalogs):
        actual = {(kind, item.id) for kind, doc in catalogs.items() for item in doc.items}
        if actual != set(self.rows):
            raise ValueError("Catalog objects differ from assumption registry")
        for kind, doc in catalogs.items():
            if doc.snapshot_id != self.snapshot_id:
                raise ValueError("Assumptions belong to another snapshot")
            for item in doc.items:
                entry = self.rows[(kind, item.id)]
                metadata = item.source.original_fields.get("synthetic_assumption", {})
                if (metadata.get("id") != entry[0] or condition_digest(item) != entry[1] or
                    item.price.price_kind != "synthetic_assumption" or item.source.source_url is not None or
                    item.source.checked_at is not None or item.source.verified_fields):
                    raise ValueError("Unregistered or changed assumption: " + item.id)

    def supports(self, item, field):
        for (kind, key), (identifier, row_hash, fields) in self.rows.items():
            if key == item.id:
                return field in fields and condition_digest(item) == row_hash
        return False

    def changed(self, before, after, change):
        """Only a public unavailability intervention can modify registered conditions."""
        self.verify(before)
        if change.get("type") != "unavailable":
            raise ValueError("Unsupported assumption intervention")
        rows = dict(self.rows)
        found = 0
        for kind, doc in before.items():
            new = {i.id: i for i in after[kind].items}
            for item in doc.items:
                expected = replace(item, availability_status="unavailable") if item.id == change["reference_id"] else item
                if jsonable(new.get(item.id)) != jsonable(expected):
                    raise ValueError("Intervention modified unrelated conditions")
                if item.id == change["reference_id"]:
                    identifier, _, fields = rows[(kind, item.id)]
                    rows[(kind, item.id)] = (identifier, digest(expected), fields)
                    found += 1
        if found != 1:
            raise ValueError("Intervention needs one unique object")
        result = replace(self, rows=MappingProxyType(rows))
        result.verify(after)
        return result


def load_context(root, catalogs, mode="strict"):
    if mode == "strict":
        return None
    if mode != "benchmark_assumptions":
        raise ValueError("Unknown evaluation mode")
    from pathlib import Path
    registry = read_json(Path(root) / "assumptions.json")
    manifest = read_json(Path(root) / "manifest.json")
    if manifest.get("evaluation_mode") != mode or manifest.get("assumptions_hash") != digest(registry):
        raise ValueError("Assumption registry differs from manifest")
    return AssumptionContext.load(catalogs, registry)
