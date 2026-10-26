from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

class RegistryError(RuntimeError):
 """Base error for fail-closed resource lifecycle handling."""

class ResourceNotFound(RegistryError):
 """Resource is absent; removal is idempotently complete."""

class RunConflict(RegistryError):
 """Run id is already owned by another live registry."""

def parse_resource_id(value: str) -> tuple[str, str, str]:
 if not isinstance(value, str):
  raise RegistryError("resource ID must be a string")
 parts = value.split("/")
 if len(parts) !=3 or any(not part or part in {".", ".."} for part in parts):
  raise RegistryError("resource ID must be owner/name/id")
 return parts[0], parts[1], parts[2]

@dataclass(frozen=True)
class ResourceRecord:
 resource_id: str
 owner: str
 name: str
 depends_on: tuple[str, ...] = ()

 def __post_init__(self) -> None:
  owner, name, ident = parse_resource_id(self.resource_id)
  if (owner, name) != (self.owner, self.name) or not ident:
   raise RegistryError("record identity mismatch")
  if len(set(self.depends_on)) != len(self.depends_on):
   raise RegistryError("duplicate dependencies")

def validate_inspection(record: ResourceRecord, observed: Mapping[str, Any]) -> None:
 if not isinstance(observed, Mapping):
  raise RegistryError("inspect returned non-mapping")
 owner = observed.get("owner")
 name = observed.get("name", observed.get("resource_name"))
 resource_id = observed.get("id", observed.get("resource_id"))
 if owner is None or name is None or resource_id is None:
  raise RegistryError("inspect result is missing identity")
 if (owner, name, resource_id) != (record.owner, record.name, record.resource_id):
  raise RegistryError("inspect identity mismatch")

class ResourceRegistry:
 _active_runs: set[str] = set()

 def __init__(self, run_id: str, inspect: Callable[[ResourceRecord], Mapping[str, Any]], remove: Callable[[ResourceRecord], None]) -> None:
  if not run_id:
   raise RegistryError("run_id is required")
  if run_id in self._active_runs:
   raise RunConflict("run_id is already active")
  self._active_runs.add(run_id)
  self.run_id = run_id
  self._inspect = inspect
  self._remove = remove
  self._records: dict[str, ResourceRecord] = {}
  self._done: set[str] = set()
  self._finished = False

 def add(self, record: ResourceRecord) -> ResourceRecord:
  if self._finished:
   raise RegistryError("registry is already finished")
  if record.resource_id in self._records:
   raise RegistryError("resource is already registered")
  unknown = set(record.depends_on) - self._records.keys()
  if unknown:
   raise RegistryError(f"unregistered dependency: {sorted(unknown)}")
  self._records[record.resource_id] = record
  return record

 def teardown(self) -> tuple[str, ...]:
  if self._finished:
   return ()
  removed: list[str] = []
  pending = set(self._records) - self._done
  try:
   while pending:
    ready = [rid for rid in pending if not any(rid in self._records[other].depends_on for other in pending if other != rid)]
    if not ready:
     raise RegistryError("dependency cycle")
    for rid in sorted(ready):
     record = self._records[rid]
     try:
      observed = self._inspect(record)
     except ResourceNotFound:
      self._done.add(rid)
     else:
      validate_inspection(record, observed)
      self._remove(record)
      self._done.add(rid)
     pending.remove(rid)
     removed.append(rid)
   self._finished = True
   return tuple(removed)
  finally:
   self._active_runs.discard(self.run_id)

 def __enter__(self) -> "ResourceRegistry":
  return self

 def __exit__(self, *_exc: object) -> bool:
  self.teardown()
  return False
