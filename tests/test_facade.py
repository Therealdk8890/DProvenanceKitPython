import os
from pathlib import Path
from dprovenancekit import trace

def test_facade_basic_workflow(tmp_path):
    with trace("Step 1: Init"):
        pass

    with trace("Step 2: Processing"):
        with trace("Step 2a: Inner"):
            pass
            
    with trace("Step 3: End"):
        pass

    db_path = tmp_path / "run.sqlite"
    trace.save(db_path)
    
    assert db_path.exists()
    
    # We can diff against itself and it should be identical
    # To capture output we would redirect stdout but for now just call it
    trace.explain()
    trace.diff(str(db_path))
