"""Backport the Qdrant adapter onto a copied live pipeline, without DB migrations.

Both paths must be staging copies. The original image and server checkout stay
intact. This intentionally excludes unrelated fingerprint/dedup schema changes.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path

FILES = ('embedding.py', 'vector_store.py', 'vector_store_qdrant.py',
         'routers/search.py', 'routers/tenants.py', 'services/access.py',
         'services/indexes.py', 'temporal/document_tasks.py')
DB_FUNCTIONS = {'ensure_tenant_default_index', 'resolve_ingest_index_name',
                '_seed_tenant_indexes', 'get_search_settings',
                'get_index_by_backend_name', '_persist_search_index_name',
                'update_search_settings'}


def prepare(runtime: Path, candidate: Path):
    original = (runtime / 'db.py').read_text()
    wanted = (candidate / 'db.py').read_text()
    old_nodes = ast.parse(original).body
    new_nodes = ast.parse(wanted).body
    lines = original.splitlines(keepends=True)
    candidate_lines = wanted.splitlines(keepends=True)
    replacements = []
    for node in old_nodes:
        if isinstance(node, ast.FunctionDef) and node.name in DB_FUNCTIONS:
            other = next(n for n in new_nodes if isinstance(n, ast.FunctionDef) and n.name == node.name)
        elif isinstance(node, ast.ImportFrom) and node.module == 'vector_store':
            other = next(n for n in new_nodes if isinstance(n, ast.ImportFrom) and n.module == 'vector_store')
        else:
            continue
        replacements.append((node.lineno - 1, node.end_lineno,
                             ''.join(candidate_lines[other.lineno-1:other.end_lineno])))
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement]
    result = ''.join(lines)
    existing = {n.name for n in old_nodes if isinstance(n, ast.FunctionDef)}
    for node in new_nodes:
        if isinstance(node, ast.FunctionDef) and node.name in DB_FUNCTIONS - existing:
            result += '\n\n' + ''.join(candidate_lines[node.lineno-1:node.end_lineno])
    ast.parse(result)
    (runtime / 'db.py').write_text(result)
    for name in FILES:
        (runtime / name).write_text((candidate / name).read_text())
    return {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
            for name in ('db.py', *FILES)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.runtime, args.candidate), indent=2))
