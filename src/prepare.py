"""Explicit, hash-bound normalization; no guessing source facts."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import hashlib
import math
import json

from .contracts import read_json, write_json
from .schemas import parse_catalog


def encoded(document):
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def metadata_numbers(value):
    """Money was converted with Decimal above; retain JSON numeric metadata types."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Nonfinite metadata number")
        result = int(value) if value == value.to_integral_value() else float(value)
        if isinstance(result, float) and not math.isfinite(result):
            raise ValueError("Metadata number exceeds JSON numeric range")
        return result
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Nonfinite metadata number")
    if isinstance(value, dict):
        return {k: metadata_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [metadata_numbers(v) for v in value]
    return value


def prepare_snapshot(manifest_path, check=False):
    manifest_path = Path(manifest_path).resolve()
    manifest = read_json(manifest_path)
    if manifest.get("evaluation_mode") == "benchmark_assumptions":
        from .synthetic_benchmark import prepare
        return prepare(manifest_path, check=check)
    if not check and manifest.get("status") == "verified":
        raise ValueError("A verified snapshot is immutable; use a new snapshot directory")
    workspace = manifest_path.parents[2]
    outputs, problems = {}, []
    from .schemas import CATALOG_KINDS
    if sorted(spec["kind"] for spec in manifest["sources"]) != sorted(CATALOG_KINDS):
        raise ValueError("Manifest must specify each of the five catalog kinds exactly once")
    for spec in manifest["sources"]:
        source = (workspace / spec["path"]).resolve()
        if not source.is_relative_to(workspace):
            raise ValueError("Source path outside workspace")
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != spec["sha256"]:
            problems.append({"file": spec["path"], "error": "Source hash changed; review transformations again"})
            continue
        text = content.decode("utf-8")
        try:
            for replacement in spec.get("text_replacements", []):
                if text.count(replacement["old"]) != 1:
                    raise ValueError("Text correction must match exactly once")
                text = text.replace(replacement["old"], replacement["new"], 1)
            raw = json.loads(text, parse_float=Decimal)
            rows = raw if isinstance(raw, list) else raw["items"]
            rows = deepcopy(rows)
            rows = [row for row in rows if row["id"] not in spec.get("quarantine_ids", [])]
            for row in rows:
                for key in spec.get("omit_fields", []):
                    row.pop(key, None)
                price = row["price"]
                if "amount" in price:
                    value = price.pop("amount")
                    if price["currency"] not in {"RUB", "rubles"}:
                        raise ValueError("Unrecognized source currency")
                    if value is None:
                        price["amount_kopecks"] = None
                    else:
                        if type(value) is bool:
                            raise ValueError("Boolean money is invalid")
                        amount = Decimal(str(value)) * 100
                        if not amount.is_finite() or amount != amount.to_integral_value():
                            raise ValueError("Nonfinite or fractional kopeck amount")
                        price["amount_kopecks"] = int(amount)
                if price["currency"] == "rubles":
                    price["currency"] = "RUB"
                if price["price_kind"] == "indicative_test_value":
                    # Preserve the original synthetic amount in the immutable raw source,
                    # but never promote it into a usable benchmark price.
                    price["price_kind"], price["amount_kopecks"] = "unknown", None
                if row.get("availability_status") == "not_checked":
                    row["availability_status"] = "unknown"
            doc = {"schema_version": "0.2.0", "snapshot_id": manifest["snapshot_id"],
                   "is_template": manifest.get("output_is_template", True), "timezone": manifest["timezone"], "items": rows}
            doc = metadata_numbers(doc)
            parse_catalog(doc, spec["kind"])
            outputs[spec["kind"]] = doc
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            problems.append({"file": spec["path"], "error": str(exc)})
    if not problems:
        from .catalog import LoadedDataset
        from .validator import (_build_indexes, _identifier_check, _station_check,
                                _transfer_endpoints_check, _snapshot_check)
        dataset = LoadedDataset(manifest_path.parent, "", catalogs={
            k: parse_catalog(v, k) for k, v in outputs.items()})
        index = _build_indexes(dataset)
        checks = [_identifier_check(dataset, index), _station_check(dataset, index),
                  _transfer_endpoints_check(dataset, index), _snapshot_check(dataset)]
        problems.extend(p.to_dict() for c in checks for p in c.violations)
    if not problems and check:
        for kind, doc in outputs.items():
            target = manifest_path.parent / "snapshot" / f"{kind}.json"
            if not target.exists() or target.read_bytes() != encoded(doc):
                problems.append({"file": str(target), "error": "Derived snapshot differs from deterministic preparation"})
    if not problems and not check:
        for kind, doc in outputs.items():
            target = manifest_path.parent / "snapshot" / f"{kind}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded(doc))
    return {"status": "blocked" if problems else "normalized_unverified", "problems": problems,
            "files": sorted(outputs), "experiment_ready": False}
