import argparse
import sys
from pathlib import Path
from langchain_core.runnables import RunnableLambda
from dprovenancekit import SQLiteTraceStore
from dprovenancekit.integrations.langchain import DProvenanceTracer, LangChainTraceEvent

def retrieve_context(query: str) -> dict:
    return {"query": query, "context": "The capital of France is Paris."}

def verify_facts(data: dict) -> dict:
    data["verified"] = True
    return data

def generate_answer(data: dict) -> str:
    return f"Answer based on {data['context']} (Verified: {data.get('verified', False)})"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--buggy", action="store_true", help="Run the buggy version of the agent")
    parser.add_argument("--db", type=str, required=True, help="Path to SQLite database")
    args = parser.parse_args()

    store = SQLiteTraceStore(LangChainTraceEvent, args.db)
    tracer = DProvenanceTracer(store)

    if args.buggy:
        # BUG: A developer accidentally drops the verification step in this branch!
        chain = (
            RunnableLambda(retrieve_context).with_config({"run_name": "retrieve_context"})
            | RunnableLambda(generate_answer).with_config({"run_name": "generate_answer"})
        )
    else:
        # GOLDEN: The intended full reasoning pipeline
        chain = (
            RunnableLambda(retrieve_context).with_config({"run_name": "retrieve_context"})
            | RunnableLambda(verify_facts).with_config({"run_name": "verify_facts"})
            | RunnableLambda(generate_answer).with_config({"run_name": "generate_answer"})
        )

    with tracer.trace(context_id="agent_run") as cb:
        result = chain.invoke("What is the capital of France?", config={"callbacks": [cb]})
        print(f"Agent Output: {result}")
        print(f"Trace Run ID: {cb.run_id}")
        
        # Write out the run ID so our bash script can easily pick it up
        with open(f"run_id_{'buggy' if args.buggy else 'golden'}.txt", "w") as f:
            f.write(str(cb.run_id))
            
    # Ensure background writes complete and database is cleanly closed
    store.close()

if __name__ == "__main__":
    main()
