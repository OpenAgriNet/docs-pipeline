#!/usr/bin/env python3
"""
Query failed workflows and write their IDs and error messages to a file.

This script:
1. Queries SQLite database for all failed workflows
2. Extracts workflow_id, filename, and error_message
3. Optionally queries Temporal for additional error details
4. Writes results to a CSV file
"""

import asyncio
import csv
import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from temporalio.client import Client, WorkflowFailureError

# Database path - try environment variable first, then default
DB_PATH = os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db")

# Configuration
TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
OUTPUT_FILE = Path("failed_workflows.csv")
INCLUDE_TEMPORAL_DETAILS = True  # Set to False to skip Temporal queries


async def get_temporal_error_details(workflow_id: str, client: Client) -> dict:
    """Get detailed error information from Temporal for a failed workflow."""
    try:
        handle = client.get_workflow_handle(workflow_id)
        description = await handle.describe()
        
        result = {
            "temporal_status": description.status.name,
            "temporal_error_message": None,
            "temporal_error_type": None,
            "temporal_stack_trace": None,
        }
        
        # If workflow is failed, try to get detailed error information
        if description.status.name == "FAILED":
            try:
                # Try to get error from workflow result
                await handle.result()
            except WorkflowFailureError as wf_err:
                result["temporal_error_message"] = str(wf_err)
                result["temporal_error_type"] = type(wf_err).__name__
                
                # Try to get the underlying cause
                if hasattr(wf_err, 'cause') and wf_err.cause:
                    cause = wf_err.cause
                    result["temporal_error_message"] = str(cause)
                    result["temporal_error_type"] = type(cause).__name__
                    
                    # Get stack trace if available
                    if hasattr(cause, '__traceback__') and cause.__traceback__:
                        import traceback
                        result["temporal_stack_trace"] = ''.join(traceback.format_tb(cause.__traceback__))
                
                # Also try to get failure details from the exception itself
                if hasattr(wf_err, 'failure') and wf_err.failure:
                    failure = wf_err.failure
                    if hasattr(failure, 'message') and failure.message:
                        result["temporal_error_message"] = failure.message
                    if hasattr(failure, 'stack_trace') and failure.stack_trace:
                        result["temporal_stack_trace"] = failure.stack_trace
            except Exception as e:
                result["temporal_error_message"] = f"Could not retrieve error details: {str(e)}"
        
        # Also try to get error from workflow state query (fallback)
        if not result["temporal_error_message"]:
            try:
                from pipeline.workflows import DocumentPipelineWorkflow
                state = await handle.query(DocumentPipelineWorkflow.get_state)
                if state and state.get("error_message"):
                    result["temporal_error_message"] = state.get("error_message")
            except Exception:
                pass  # Workflow might not support queries or be in wrong state
        
        return result
    except Exception as e:
        return {
            "temporal_status": "ERROR",
            "temporal_error_message": f"Could not query Temporal: {str(e)}",
            "temporal_error_type": None,
            "temporal_stack_trace": None,
        }


def query_failed_workflows_from_db(db_path: str) -> list:
    """Query SQLite database directly for failed workflows."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    try:
        cursor = conn.execute("""
            SELECT workflow_id, document_id, filename, error_message, created_at
            FROM documents
            WHERE stage = 'failed'
            ORDER BY created_at DESC
        """)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


async def main():
    """Main function to query failed workflows and write to file."""
    print(f"Querying failed workflows from SQLite database at {DB_PATH}...")
    
    # Check if database exists
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        print("Make sure you're running this script inside the Docker container or")
        print("that the database path is correct.")
        sys.exit(1)
    
    # Query SQLite for failed workflows
    failed_docs = query_failed_workflows_from_db(DB_PATH)
    
    print(f"Found {len(failed_docs)} failed workflows")
    
    # Prepare data for CSV
    rows = []
    temporal_client = None
    
    if INCLUDE_TEMPORAL_DETAILS:
        print(f"Connecting to Temporal at {TEMPORAL_HOST}...")
        try:
            temporal_client = await Client.connect(TEMPORAL_HOST)
            print("Connected to Temporal")
        except Exception as e:
            print(f"Warning: Could not connect to Temporal: {e}")
            print("Will only use SQLite error messages")
            temporal_client = None
    
    # Process each failed workflow
    for i, doc in enumerate(failed_docs, 1):
        workflow_id = doc.get("workflow_id", "")
        filename = doc.get("filename", "")
        error_message = doc.get("error_message", "")
        document_id = doc.get("document_id", "")
        created_at = doc.get("created_at", "")
        
        row = {
            "workflow_id": workflow_id,
            "document_id": document_id,
            "filename": filename,
            "sqlite_error_message": error_message,
            "created_at": created_at,
        }
        
        # Get Temporal details if available
        if temporal_client and INCLUDE_TEMPORAL_DETAILS:
            print(f"[{i}/{len(failed_docs)}] Querying Temporal for {workflow_id}...")
            temporal_details = await get_temporal_error_details(workflow_id, temporal_client)
            row.update(temporal_details)
        else:
            row.update({
                "temporal_status": None,
                "temporal_error_message": None,
                "temporal_error_type": None,
                "temporal_stack_trace": None,
            })
        
        rows.append(row)
    
    # Write to CSV file
    if rows:
        fieldnames = [
            "workflow_id",
            "document_id",
            "filename",
            "created_at",
            "sqlite_error_message",
            "temporal_status",
            "temporal_error_message",
            "temporal_error_type",
            "temporal_stack_trace",
        ]
        
        print(f"\nWriting {len(rows)} failed workflows to {OUTPUT_FILE}...")
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        
        print(f"✓ Successfully wrote {len(rows)} failed workflows to {OUTPUT_FILE}")
        print(f"\nSummary:")
        print(f"  - Total failed workflows: {len(rows)}")
        print(f"  - With SQLite error messages: {sum(1 for r in rows if r.get('sqlite_error_message'))}")
        if temporal_client:
            print(f"  - With Temporal error messages: {sum(1 for r in rows if r.get('temporal_error_message'))}")
    else:
        print("No failed workflows found.")


if __name__ == "__main__":
    asyncio.run(main())
