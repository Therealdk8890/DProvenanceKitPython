#!/bin/bash
set -e

# Change to the script's directory so it runs properly
cd "$(dirname "$0")"

echo "=========================================================="
echo " DProvenanceKit - PR Regression Gate Demo"
echo "=========================================================="
echo ""
echo "This demo simulates a CI pipeline that uses 'dprovenancekit gate'"
echo "to block a Pull Request if an AI agent's reasoning drifts."
echo ""

# Ensure langchain dependencies are installed
# Normally this would be done via your project's pip/poetry setup
echo "[1] Ensuring test dependencies are installed..."
if ! python3 -c "import langchain_core" 2>/dev/null; then
    echo "    Installing langchain-core..."
    python3 -m pip install -q "langchain-core"
fi

DB_FILE="trace.sqlite"
# Clean up previous runs
rm -f "$DB_FILE" run_id_golden.txt run_id_buggy.txt

echo "[2] Running Golden Agent (representing the 'main' branch)..."
python3 agent.py --db "$DB_FILE"
GOLDEN_ID=$(cat run_id_golden.txt)
echo "    -> Golden Run ID: $GOLDEN_ID"
echo ""

echo "[3] Running Buggy Agent (representing the Pull Request)..."
echo "    (A developer accidentally dropped the 'verify_facts' step!)"
python3 agent.py --buggy --db "$DB_FILE"
BUGGY_ID=$(cat run_id_buggy.txt)
echo "    -> Candidate Run ID: $BUGGY_ID"
echo ""

echo "[4] Running dprovenancekit gate..."
echo "    Running: dprovenancekit gate --db $DB_FILE --golden $GOLDEN_ID --candidate $BUGGY_ID"
echo "----------------------------------------------------------"
set +e
dprovenancekit gate --db "$DB_FILE" --golden "$GOLDEN_ID" --candidate "$BUGGY_ID"
EXIT_CODE=$?
set -e
echo "----------------------------------------------------------"

if [ $EXIT_CODE -ne 0 ]; then
    echo "✅ SUCCESS: The PR gate successfully caught the reasoning regression"
    echo "             (the dropped 'verify_facts' step) and failed the build (Exit Code $EXIT_CODE)!"
else
    echo "❌ FAILURE: The PR gate incorrectly allowed the regression to pass!"
    exit 1
fi
