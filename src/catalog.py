"""UTF-8 JSON loading and dataset path resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, TypeVar

from .schemas import (
    CATALOG_KINDS,
    CatalogDocument,
    ManualPlan,
    Problem,
    Scenario,
    SchemaError,
    parse_catalog,
    parse_plan,
    parse_scenario,
)


T = TypeVar("T")


class DataLoadError(Exception):
    """Expected input error with a stable diagnostic."""

    def __init__(self, problem: Problem) -> None:
        self.problem = problem
        super().__init__(problem.message)


def _display_path(path: Path, data_dir: Path | None = None) -> str:
    if data_dir is not None:
        try:
            return str(path.resolve().relative_to(data_dir.resolve())).replace("\\", "/")
        except (OSError, ValueError):
            pass
    return str(path).replace("\\", "/")


def _read_json(path: Path, *, data_dir: Path | None = None) -> Any:
    display = _display_path(path, data_dir)
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError as exc:
        raise DataLoadError(
            Problem(code="FILE_NOT_FOUND", message=f"Файл не найден: {display}", file=display, path="$")
        ) from exc
    except PermissionError as exc:
        raise DataLoadError(
            Problem(code="FILE_UNREADABLE", message=f"Нет доступа к файлу: {display}", file=display, path="$")
        ) from exc
    except OSError as exc:
        raise DataLoadError(
            Problem(code="FILE_UNREADABLE", message=f"Не удалось прочитать {display}: {exc}", file=display, path="$")
        ) from exc
    except UnicodeDecodeError as exc:
        raise DataLoadError(
            Problem(code="INVALID_UTF8", message=f"Файл {display} не является корректным UTF-8", file=display, path="$")
        ) from exc
    except json.JSONDecodeError as exc:
        raise DataLoadError(
            Problem(
                code="INVALID_JSON",
                message=f"Повреждённый JSON в {display}, строка {exc.lineno}, столбец {exc.colno}: {exc.msg}",
                file=display,
                path="$",
            )
        ) from exc


def _load_with_parser(
    path: Path,
    parser: Callable[[Any, str], T],
    *,
    data_dir: Path | None = None,
) -> T:
    display = _display_path(path, data_dir)
    raw = _read_json(path, data_dir=data_dir)
    try:
        return parser(raw, "$")
    except SchemaError as exc:
        code = "UNSUPPORTED_SCHEMA_VERSION" if exc.path.endswith(".schema_version") and "неподдерживаемая" in exc.message else "SCHEMA_ERROR"
        raise DataLoadError(
            Problem(
                code=code,
                message=f"Ошибка схемы в {display}, поле {exc.path}: {exc.message}",
                file=display,
                path=exc.path,
            )
        ) from exc


def load_catalog(path: str | Path, kind: str, *, data_dir: str | Path | None = None) -> CatalogDocument:
    """Load and strictly validate one catalog file."""

    source = Path(path)
    base = Path(data_dir) if data_dir is not None else None
    return _load_with_parser(source, lambda raw, root: parse_catalog(raw, kind, root), data_dir=base)


def load_scenario(path: str | Path, *, data_dir: str | Path | None = None) -> Scenario:
    """Load and strictly validate one scenario file."""
    source = Path(path)
    raw = _read_json(source, data_dir=Path(data_dir) if data_dir else None)
    if isinstance(raw, dict) and set(raw) == {"redirect"}:
        if not isinstance(raw["redirect"], str) or not raw["redirect"]:
            raise DataLoadError(Problem("INVALID_SCENARIO_ALIAS", "Alias target must be a relative path", file=str(source)))
        target = (source.parent / raw["redirect"]).resolve()
        if not target.is_relative_to(source.parent.parent.resolve()):
            raise DataLoadError(Problem("INVALID_SCENARIO_ALIAS", "Alias escapes data root", file=str(source)))
        source = target
    return _load_with_parser(
        source, parse_scenario, data_dir=Path(data_dir) if data_dir is not None else None
    )


def load_plan(path: str | Path, *, data_dir: str | Path | None = None) -> ManualPlan:
    """Load and strictly validate one manual plan file."""

    return _load_with_parser(Path(path), parse_plan, data_dir=Path(data_dir) if data_dir is not None else None)


@dataclass
class LoadedDataset:
    data_dir: Path
    scenario_name: str
    catalogs: dict[str, CatalogDocument | None] = field(default_factory=dict)
    scenario: Scenario | None = None
    plan: ManualPlan | None = None
    input_errors: list[Problem] = field(default_factory=list)

    @property
    def paths(self) -> dict[str, str]:
        result = {kind: f"snapshot/{kind}.json" for kind in CATALOG_KINDS}
        result["scenario"] = f"scenarios/dev/{self.scenario_name}.json"
        result["plan"] = f"reference_plans/{self.scenario_name}.json"
        return result


def load_dataset(data_dir: str | Path, scenario_name: str) -> LoadedDataset:
    """Load all seven inputs, accumulating independent input diagnostics."""

    base = Path(data_dir)
    loaded = LoadedDataset(data_dir=base, scenario_name=scenario_name)
    for kind in CATALOG_KINDS:
        path = base / "snapshot" / f"{kind}.json"
        try:
            loaded.catalogs[kind] = load_catalog(path, kind, data_dir=base)
        except DataLoadError as exc:
            loaded.catalogs[kind] = None
            loaded.input_errors.append(exc.problem)

    scenario_path = base / "scenarios" / "dev" / f"{scenario_name}.json"
    try:
        loaded.scenario = load_scenario(scenario_path, data_dir=base)
    except DataLoadError as exc:
        loaded.input_errors.append(exc.problem)

    plan_path = base / "reference_plans" / f"{scenario_name}.json"
    try:
        loaded.plan = load_plan(plan_path, data_dir=base)
    except DataLoadError as exc:
        loaded.input_errors.append(exc.problem)

    return loaded


def load_catalogs(snapshot_dir: str | Path) -> dict[str, CatalogDocument]:
    """Load facts only: no reference plans or private profiles."""
    root = Path(snapshot_dir)
    return {kind: load_catalog(root / f"{kind}.json", kind) for kind in CATALOG_KINDS}


def load_public_scenario(path: str | Path) -> Scenario:
    return load_scenario(path)
