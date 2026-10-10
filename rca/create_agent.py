"""Explicitly create one version of the managed evidence-only RCA agent."""

import hashlib
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from rca.instructions import RCA_INSTRUCTIONS


@contextmanager
def _agent_creation_lock(endpoint: str, agent_name: str) -> Iterator[None]:
    """Serialize agent-version preflight and creation on this machine."""
    lock_key = hashlib.sha256(f"{endpoint}\0{agent_name}".encode("utf-8")).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / f"foundry-agent-create-{lock_key}.lock"
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.25)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _create_version_once(
    project: AIProjectClient, endpoint: str, agent_name: str, model: str
) -> object | None:
    """Create version 2 only while version 1 is the sole existing version."""
    with _agent_creation_lock(endpoint, agent_name):
        existing_versions = [
            str(item.version)
            for item in project.agents.list_versions(agent_name=agent_name)
        ]
        if len(existing_versions) != 1 or set(existing_versions) != {"1"}:
            return None
        return project.agents.create_version(
            agent_name=agent_name,
            definition=PromptAgentDefinition(model=model, instructions=RCA_INSTRUCTIONS),
        )


def main() -> int:
    """Create a version once when this command is explicitly run."""
    credential = project = None
    try:
        load_dotenv(".env")
        endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError("Set FOUNDRY_PROJECT_ENDPOINT to the Foundry project endpoint.")
        agent_name = os.environ.get("FOUNDRY_AGENT_NAME", "").strip() or "aiops-rca-agent"
        model = os.environ.get("FOUNDRY_MODEL", "").strip() or "gpt-5-mini"
        credential = DefaultAzureCredential()
        project = AIProjectClient(endpoint=endpoint, credential=credential)
        version = _create_version_once(project, endpoint, agent_name, model)
        if version is None:
            print(
                "Agent creation stopped; expected exactly the preserved version 1.",
                file=sys.stderr,
            )
            return 1
        print(f"Created agent {version.name}, version {version.version}")
        return 0
    except AzureError:
        print("Agent creation failed. Check Azure sign-in and project access.", file=sys.stderr)
        return 1
    except ValueError:
        print("Agent creation failed. Check FOUNDRY_PROJECT_ENDPOINT and configuration.", file=sys.stderr)
        return 1
    finally:
        try:
            if project is not None:
                project.close()
        finally:
            if credential is not None:
                credential.close()


if __name__ == "__main__":
    raise SystemExit(main())
