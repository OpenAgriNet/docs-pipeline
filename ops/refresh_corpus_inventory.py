"""Publish exact, timestamped final-collection counts for the pipeline dashboard."""
import argparse
import time
from datetime import datetime, timezone


def refresh(target, instance):
    from pipeline import db, vector_store
    if vector_store.vector_store_backend() != 'qdrant' or vector_store.default_physical_index() != target:
        raise RuntimeError('Explicit Qdrant target configuration required')
    client = vector_store.get_vector_store().client()
    facets = client.facet(target, key='doc_id', limit=10000, exact=True)
    if len(facets.hits) >= 10000:
        raise RuntimeError('Inventory facet limit reached; refusing a partial document count')
    # Qdrant can return facet terms with count=0 after deletes. Do not count them.
    live = [hit for hit in facets.hits if hit.count > 0]
    count = client.count(target, exact=True).count
    if sum(hit.count for hit in live) != count:
        raise RuntimeError('Not every point has one document ID; inventory needs investigation')
    inventory = {'collection':target,'unique_files':len(live),'chunks':count,
                 'verified_at':datetime.now(timezone.utc).isoformat()}
    db.save_corpus_inventory(instance, inventory)
    print(inventory, flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--target', required=True)
    p.add_argument('--instance', default='amul')
    p.add_argument('--interval', type=int, default=0)
    args = p.parse_args()
    while True:
        refresh(args.target,args.instance)
        if not args.interval:
            break
        time.sleep(max(10,args.interval))
