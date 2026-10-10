"""Explicitly create one version of the managed evidence-only RCA agent."""

import os
import sys

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from rca.instructions import RCA_INSTRUCTIONS


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
        version = project.agents.create_version(
            agent_name=agent_name,
            definition=PromptAgentDefinition(model=model, instructions=RCA_INSTRUCTIONS),
        )
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
