"""Standalone checks for native Windows process and reparse-point behavior.

Run from the repository root with Python on Windows:
    python tests/transport/windows_native_checks.py

These checks are intentionally excluded from normal discovery. Linux tests use
fake Win32 providers; this file exercises the real Job Object and filesystem
APIs when a native Windows host is available.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4


if os.name != "nt":
    print("SKIP: native Windows process and reparse checks require Windows")
    raise SystemExit(0)

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from provider_transport.cwd_authority import ChildLedger, SpawnAdmission
from provider_transport.directory_authority import CwdDenied, create_cwd_authority
from provider_transport.windows_filesystem import open_directory
from provider_transport.windows_process import (
    WindowsJob,
    WindowsJobCell,
    process_identity,
)
from provider_transport.jsonl_rpc import JsonlRpcClient


def check_job_owns_descendants() -> None:
    job = WindowsJob()
    command = (
        sys.executable,
        "-c",
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "time.sleep(0.15)",
    )
    try:
        root = job.spawn(command, cwd=os.getcwd(), options={})
        root.wait(timeout=10)
        if job.active_processes < 1:
            raise AssertionError("descendant escaped the Job Object")
        job.terminate()
        if not job.wait_empty(5):
            raise AssertionError("Job Object did not become empty after termination")
    finally:
        try:
            if job.active_processes:
                job.terminate()
                job.wait_empty(5)
        finally:
            job.close(require_empty=False)


def _parent_death_child(supervisor_name: str, ready_path: Path, marker_path: Path) -> None:
    job = WindowsJob(name=supervisor_name)
    try:
        child = job.spawn(
            (
                sys.executable,
                "-c",
                f"import pathlib,time; time.sleep(3); pathlib.Path({str(marker_path)!r}).write_text('escaped')",
            ),
            cwd=os.getcwd(),
            options={},
        )
        ready_path.write_text(json.dumps({"pid": child.pid, "identity": process_identity(child.pid)}))
        time.sleep(60)
    finally:
        job.close(require_empty=False)


def check_parent_death_closes_job() -> None:
    with tempfile.TemporaryDirectory(prefix="loop-engine-job-death-") as temporary:
        root = Path(temporary)
        ready = root / "ready.json"
        marker = root / "escaped.txt"
        job_name = f"Local\\loop-engine-native-check-{uuid4().hex}"
        supervisor = subprocess.Popen(
            (
                sys.executable, str(Path(__file__).resolve()), "--parent-death",
                job_name, str(ready), str(marker),
            ),
            cwd=os.getcwd(),
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                if supervisor.poll() is not None:
                    raise AssertionError("parent-death supervisor exited before readiness")
                time.sleep(0.02)
            if not ready.exists():
                raise AssertionError("parent-death supervisor did not become ready")
            row = json.loads(ready.read_text(encoding="utf-8"))
            pid = int(row["pid"])
            identity = row["identity"]
            if not identity or process_identity(pid) != identity:
                raise AssertionError("child process identity was not captured")
            supervisor.terminate()
            supervisor.wait(timeout=5)
            deadline = time.monotonic() + 5
            while process_identity(pid) == identity and time.monotonic() < deadline:
                time.sleep(0.02)
            if process_identity(pid) == identity:
                raise AssertionError("child survived parent death")
            time.sleep(3.2)
            if marker.exists():
                raise AssertionError("descendant executed after parent death")
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=5)


def check_native_jsonl_spawn_is_protocol_clean() -> None:
    with tempfile.TemporaryDirectory(prefix="loop-engine-jsonl-native-") as temporary:
        cwd = Path(temporary)
        cell_id = "native-jsonl-check"
        membership = WindowsJobCell()
        authority = create_cwd_authority(cwd)
        cwd_lease = authority.admit(".")
        renamed = cwd.with_name(cwd.name + "-renamed")
        try:
            cwd.rename(renamed)
        except OSError:
            pass
        else:
            renamed.rename(cwd)
            raise AssertionError("cwd authority allowed a swap before native spawn")
        ledger = ChildLedger(cell_id=cell_id, cell=membership)
        admission = SpawnAdmission(
            reservation=ledger.reserve(),
            cwd_lease=cwd_lease,
            cell_id=cell_id,
            epoch=1,
            fence_check=lambda epoch: epoch == 1,
            cell_membership=membership,
        )
        peer = (
            "import json,sys; "
            "[print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'serverInfo':{'name':'native-check','version':'1'}}}),flush=True) "
            "for line in sys.stdin if (m:=json.loads(line)).get('method')=='initialize']"
        )
        client = None
        try:
            client = JsonlRpcClient.launch(
                command=(sys.executable, "-c", peer),
                spawn_admission=admission,
                host_cell_id=cell_id,
            )
            if client.initialize_result is None:
                raise AssertionError("native JSON-RPC initialize returned no result")
            server_info = client.initialize_result.get("serverInfo")
            if not isinstance(server_info, dict) or server_info.get("name") != "native-check":
                raise AssertionError("native JSON-RPC response was not parsed")
            if client.stderr_text():
                raise AssertionError(f"native JSON-RPC wrote diagnostics to stderr: {client.stderr_text()!r}")
        finally:
            if client is not None:
                client.close()
            if not membership.empty:
                membership.terminate_all()
                if not membership.wait_empty(5):
                    raise AssertionError("native JSON-RPC cell did not become empty")
            if ledger.used:
                raise AssertionError("native JSON-RPC ownership receipt was not released")
            membership.close()
            authority.close()


def check_unicode_environment_block_is_explicit_and_complete() -> None:
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise AssertionError("native Windows process has no SystemRoot")
    sentinel = "LOOP_ENGINE_NATIVE_PARENT_SENTINEL"
    previous = os.environ.get(sentinel)
    os.environ[sentinel] = "must-not-inherit"
    job = WindowsJob()
    try:
        child = job.spawn(
            (
                sys.executable, "-X", "utf8", "-c",
                "import json,os; print(json.dumps([os.environ.get('LOOP_ENGINE_NATIVE_UNICODE'), os.environ.get('LOOP_ENGINE_NATIVE_PARENT_SENTINEL')], ensure_ascii=False))",
            ),
            cwd=os.getcwd(),
            options={
                "env": {
                    "SystemRoot": system_root,
                    "LOOP_ENGINE_NATIVE_UNICODE": "雪だるま-☃",
                },
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
            },
        )
        output = child.stdout.read()
        error = child.stderr.read()
        if child.wait(timeout=10) != 0:
            raise AssertionError(f"native Unicode environment child failed: {error}")
        if json.loads(output) != ["雪だるま-☃", None]:
            raise AssertionError(f"native environment block mismatch: {output!r}")
        if not job.wait_empty(5):
            raise AssertionError("environment check left a Job Object descendant")
    finally:
        if previous is None:
            os.environ.pop(sentinel, None)
        else:
            os.environ[sentinel] = previous
        if job.active_processes:
            job.terminate()
            job.wait_empty(5)
        job.close()


def read_authorized_file(root: Path, relative: str) -> tuple[bytes, tuple[int, int]]:
    lease = open_directory(root)
    try:
        return lease.read_file(relative)
    finally:
        lease.close()


def check_junction_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="loop-engine-junction-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        (root / "payload.txt").write_text("authorized", encoding="utf-8")
        (source / "payload.txt").write_text("junction target", encoding="utf-8")
        junction = root / "junction"
        command_interpreter = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
        command = f'mklink /J "{junction}" "{source}"'
        result = subprocess.run(
            f'"{command_interpreter}" /d /s /c "{command}"',
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not junction.exists():
            raise AssertionError(f"could not create junction fixture: {result.stderr.strip()}")
        payload, _identity = read_authorized_file(root, "payload.txt")
        if payload != b"authorized":
            raise AssertionError("authorized file read returned different bytes")
        try:
            read_authorized_file(root, "junction\\payload.txt")
        except OSError:
            pass
        else:
            raise AssertionError("file read followed a junction")
        authority = create_cwd_authority(root)
        try:
            lease = authority.admit("source")
            try:
                try:
                    source.rename(root / "renamed-source")
                except OSError:
                    pass
                else:
                    (root / "renamed-source").rename(source)
                    raise AssertionError("held directory lease allowed an alias rename")
            finally:
                lease.close()
            try:
                authority.admit("junction")
            except CwdDenied:
                pass
            else:
                raise AssertionError("directory junction was admitted as a real child")
        finally:
            authority.close()


def _replace_succeeds(directory: Path) -> bool:
    source = directory / f".authority-probe-{uuid4().hex}"
    target = directory / f".authority-probe-{uuid4().hex}"
    try:
        source.write_text("new", encoding="utf-8")
        target.write_text("old", encoding="utf-8")
        os.replace(source, target)
        return target.read_text(encoding="utf-8") == "new"
    except OSError:
        return False
    finally:
        source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def check_authority_does_not_block_writes_around_it() -> None:
    with tempfile.TemporaryDirectory(prefix="loop-engine-authority-writes-") as temporary:
        parent = Path(temporary)
        origin = parent / "origin"
        (origin / "child").mkdir(parents=True)
        authority = create_cwd_authority(origin, allow_tmp=True)
        try:
            lease = authority.admit("child")
            try:
                for directory in (parent, origin, origin / "child", Path(tempfile.gettempdir())):
                    if not _replace_succeeds(directory):
                        raise AssertionError(f"cwd authority blocked replacing a file in {directory}")
                for held in (origin, origin / "child"):
                    try:
                        held.rename(held.with_name(held.name + "-renamed"))
                    except OSError:
                        pass
                    else:
                        raise AssertionError(f"cwd authority allowed renaming held directory {held}")
            finally:
                lease.close()
        finally:
            authority.close()


def main() -> None:
    check_authority_does_not_block_writes_around_it()
    check_job_owns_descendants()
    check_parent_death_closes_job()
    check_native_jsonl_spawn_is_protocol_clean()
    check_unicode_environment_block_is_explicit_and_complete()
    check_junction_is_rejected()
    print("native Windows process and reparse checks passed")


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--parent-death":
        _parent_death_child(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
    else:
        main()
