#!/usr/bin/env python3
"""Quick script to terminate stuck workflows."""
import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from temporalio.client import Client

STUCK_WORKFLOWS = [
    "doc-a62eb95d7e84",
    "doc-44286a8816ab",
    "doc-b0e013d0090b",
    "doc-8ef60293cf2d",
    "doc-18cde2914ad3",
]

async def main():
    client = await Client.connect("localhost:7233")
    
    for workflow_id in STUCK_WORKFLOWS:
        try:
            handle = client.get_workflow_handle(workflow_id)
            await asyncio.wait_for(
                handle.terminate("Cleaning up stuck workflow - files now available"),
                timeout=5.0
            )
            print(f"✓ Terminated {workflow_id}")
        except asyncio.TimeoutError:
            print(f"⚠ Timeout terminating {workflow_id}")
        except Exception as e:
            print(f"✗ Failed to terminate {workflow_id}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
