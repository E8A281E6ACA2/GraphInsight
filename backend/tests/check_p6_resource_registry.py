from __future__ import annotations
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.tests.p6_resource_registry import (RegistryError, ResourceNotFound, ResourceRecord, ResourceRegistry, RunConflict)

def run(name, fn):
 try:
  fn()
 except Exception as exc:
  print(f"FAIL {name}: {type(exc).__name__}: {exc}")
  return False
 print(f"PASS {name}")
 return True

def main():
 results = []
 def record(name, deps=()):
  return ResourceRecord(f"owner/{name}/id-{name}", "owner", name, deps)
 def inspect(record):
  return {"owner": record.owner, "name": record.name, "id": record.resource_id}
 deleted = []
 def remove(record):
  deleted.append(record.resource_id)
 def order():
  registry = ResourceRegistry("order", inspect, remove)
  registry.add(record("network"))
  registry.add(record("postgres", ("owner/network/id-network",)))
  assert registry.teardown() == ("owner/postgres/id-postgres", "owner/network/id-network")
 results.append(run("reverse dependency order", order))
 def conflict():
  registry = ResourceRegistry("conflict", inspect, remove)
  try:
   ResourceRegistry("conflict", inspect, remove)
   raise AssertionError("conflict accepted")
  except RunConflict:
   pass
  registry.teardown()
 results.append(run("run conflict rejected", conflict))
 def mismatch():
  deleted.clear()
  registry = ResourceRegistry("mismatch", lambda _: {"owner": "other", "name": "network", "id": "other/network/id"}, remove)
  registry.add(record("network"))
  try:
   registry.teardown()
   raise AssertionError("mismatch accepted")
  except RegistryError:
   assert deleted == []
 results.append(run("identity mismatch fail closed", mismatch))
 def not_found():
  registry = ResourceRegistry("not-found", lambda _: (_ for _ in ()).throw(ResourceNotFound()), remove)
  registry.add(record("network"))
  assert registry.teardown() == ("owner/network/id-network",)
 results.append(run("not found is idempotent", not_found))
 def retry_after_failure():
  calls = []
  fail = [True]
  def flaky_remove(item):
   calls.append(item.resource_id)
   if fail[0] and item.name == "postgres":
    raise RuntimeError("injected remove failure")
  registry = ResourceRegistry("retry", inspect, flaky_remove)
  registry.add(record("network"))
  registry.add(record("postgres", ("owner/network/id-network",)))
  try:
   registry.teardown()
  except RuntimeError:
   pass
  fail[0] = False
  assert registry.teardown() == ("owner/postgres/id-postgres", "owner/network/id-network")
  assert calls == ["owner/postgres/id-postgres", "owner/postgres/id-postgres", "owner/network/id-network"]
 results.append(run("partial cleanup retry", retry_after_failure))
 def releases_lock():
  registry = ResourceRegistry("release", lambda _: (_ for _ in ()).throw(RuntimeError("inspect failure")), remove)
  registry.add(record("network"))
  try:
   registry.teardown()
  except RuntimeError:
   pass
  replacement = ResourceRegistry("release", inspect, remove)
  replacement.teardown()
 results.append(run("exception releases run lock", releases_lock))
 def cycle():
  registry = ResourceRegistry("cycle", inspect, remove)
  a_record = record("a", ("owner/b/id-b",))
  b_record = record("b", ("owner/a/id-a",))
  registry._records[a_record.resource_id] = a_record
  registry._records[b_record.resource_id] = b_record
  try:
   registry.teardown()
   raise AssertionError("cycle accepted")
  except RegistryError:
   pass
 results.append(run("dependency cycle fail closed", cycle))
 print(f"PASS total={sum(results)} failed={len(results)-sum(results)}")
 return 0 if all(results) else 1

if __name__ == "__main__":
 raise SystemExit(main())
