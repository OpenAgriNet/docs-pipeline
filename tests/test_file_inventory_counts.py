"""File counts distinguish duplicate workflows while respecting tenant visibility."""
def test_unique_files_and_workflow_records(db_connection):
    db = db_connection
    for wf, did, tenant in [('one','same','a'),('rerun','same','a'),('other','other','a'),('cross','same','b')]:
        db.upsert_document(wf,did,wf+'.pdf','/tmp/'+wf+'.pdf',instance=tenant,canonical_document_id=did)
    a = db.get_document_summary_counts(instances=['a'])
    assert a['total_documents'] == 3
    assert a['unique_files'] == 2
    assert db.get_document_summary_counts()['unique_files'] == 3
    assert db.get_document_summary_counts(instances=[])['unique_files'] == 0
    db.set_document_disabled('other', True)
    assert db.get_document_summary_counts(instances=['a'])['unique_files'] == 1
    assert db.get_document_summary_counts(instances=['a'],include_disabled=True)['unique_files'] == 2


def test_corpus_inventories_are_separate_and_tenant_scoped(db_connection):
    db = db_connection
    db.save_corpus_inventory('a', {'collection':'final-a','unique_files':10,'chunks':200})
    db.save_corpus_inventory('b', {'collection':'final-b','unique_files':20,'chunks':300})
    assert [r['collection'] for r in db.list_corpus_inventories(['a'])] == ['final-a']
    assert db.list_corpus_inventories([]) == []
    assert len(db.list_corpus_inventories()) == 2
    db.save_corpus_inventory('a', {'collection':'final-a','unique_files':11,'chunks':210})
    assert db.list_corpus_inventories(['a'])[0]['unique_files'] == 11
