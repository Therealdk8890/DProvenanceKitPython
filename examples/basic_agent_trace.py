import uuid
from dprovenancekit import (
    DProvenanceKit,
    SQLiteTraceStore,
    TracePriority,
    AnyTraceableEvent
)

def run_agent():
    # 1. Initialize a store and the kit
    store = SQLiteTraceStore(AnyTraceableEvent, path="trace.sqlite", start_writer=True)
    kit = DProvenanceKit(AnyTraceableEvent)

    # 2. Instrument the agent logic within a run context
    with kit.run(context_id="demo-agent", store=store) as run:
        print("Agent is reasoning...")
        
        # Record a thought event
        thought_payload = AnyTraceableEvent("AGENT_THOUGHT", TracePriority.STRUCTURAL.value, '{"thought": "I should retrieve documents."}')
        kit.record(thought_payload)
        
        print("Agent is retrieving...")
        
        # Record a tool execution
        tool_payload = AnyTraceableEvent("TOOL_EXECUTION", TracePriority.STRUCTURAL.value, '{"tool": "search", "query": "dprovenancekit python"}')
        kit.record(tool_payload)
        
        print("Agent is responding...")
        
        # Record the final answer
        answer_payload = AnyTraceableEvent("AGENT_ANSWER", TracePriority.STRUCTURAL.value, '{"answer": "DProvenanceKit Python is installed!"}')
        kit.record(answer_payload)

    print(f"\nFinished! Trace saved with Run ID: {run.run_id}")
    print("You can view this trace using: dprovenancekit ui --db trace.sqlite")

if __name__ == "__main__":
    run_agent()

# git-blob-rewrite
