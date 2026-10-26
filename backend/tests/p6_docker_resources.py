from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence
from backend.tests.p6_resource_registry import ResourceNotFound, ResourceRecord, ResourceRegistry

@dataclass(frozen=True)
class DockerResource:
	kind: str
	name: str
	resource_id: str
	owner: str
	depends_on: tuple[str, ...] = ()

class DockerResourceAdapter:
	def __init__(self, run_id: str, owner: str, docker: Callable[..., tuple[int, str]]) -> None:
		self.owner = owner
		self._docker = docker
		self._registry = ResourceRegistry(run_id, self._inspect, self._remove)
	def register_created(self, *, kind: str, name: str, create_output: str, depends_on: Sequence[str] = ()) -> DockerResource:
		rid = create_output.strip().splitlines()[-1] if create_output.strip() else ""
		if not rid or any(ch.isspace() for ch in rid):
			raise ValueError("Docker create did not return a single resource ID")
		record = ResourceRecord(f"{self.owner}/{kind}/{rid}", self.owner, name, tuple(depends_on))
		self._inspect(record)
		self._registry.add(record)
		return DockerResource(kind, name, record.resource_id, self.owner, tuple(depends_on))
	def teardown(self) -> tuple[str, ...]:
		return self._registry.teardown()
	def _inspect(self, record: ResourceRecord) -> Mapping[str, str]:
		rid = record.resource_id.rsplit("/",1)[-1]
		fmt = ("{{.Id}}|network|{{index .Labels \"com.graphinsight.p6.owner\"}}" if record.name == "network" else "{{.Id}}|container|{{index .Config.Labels \"com.graphinsight.p6.owner\"}}")
		args = ("network", "inspect", "--format", fmt, rid) if record.name == "network" else ("inspect", "--format", fmt, rid)
		rc, output = self._docker(*args, timeout=60, label=f"inspect {rid}")
		if rc !=0:
			lowered = output.lower()
			if "not found" in lowered or "no such" in lowered:
				raise ResourceNotFound(record.resource_id)
			raise RuntimeError(output.strip() or "Docker inspect failed")
		parts = output.strip().split("|",2)
		if len(parts) !=3:
			raise RuntimeError("Docker inspect returned incomplete identity")
		observed_id, observed_name, observed_owner = parts
		if observed_owner != self.owner or observed_name != record.name or not observed_id.startswith(rid):
			raise RuntimeError("Docker resource identity mismatch")
		return {"owner": record.owner, "name": record.name, "id": record.resource_id}
	def _remove(self, record: ResourceRecord) -> None:
		rid = record.resource_id.rsplit("/",1)[-1]
		args = ("network", "rm", rid) if record.name == "network" else ("rm", "-f", rid)
		rc, output = self._docker(*args, timeout=120, label=f"remove {rid}")
		if rc !=0:
			raise RuntimeError(output.strip() or f"failed to remove {rid}")
