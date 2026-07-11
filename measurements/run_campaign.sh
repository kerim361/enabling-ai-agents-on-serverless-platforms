#!/bin/bash
# =============================================================================
# Volle Messkampagne für die Evaluation (Experimente 1-6, beide Systeme,
# plus 1-GB-Lambda-Vergleichslauf). Sequenziell von einem einzelnen Client.
#
# Voraussetzungen: endpoints.json aktuell, AWS CLI konfiguriert (für exp2
# Cold-Start-Erzwingung und die Memory-Umschaltung des 1-GB-Laufs).
# =============================================================================
cd "$(dirname "$0")"
PY=python3
REGION="eu-central-1"
FN="chatbot-handler"

run() {
    echo ""
    echo "############ $* ############"
    "$@" || echo "!!!!!! FAILED: $*"
}

echo "======================================================"
echo " KAMPAGNE START: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "======================================================"

echo ""
echo "=================== LAMBDA (256 MB) ==================="
run $PY measure.py --system lambda exp1 --n 20
run $PY measure.py --system lambda exp3 --lengths 10 50 100 140 --reps 5
run $PY measure.py --system lambda exp4
run $PY measure.py --system lambda exp5 --reps 5
run $PY measure.py --system lambda exp6 --levels 1 2 5 10 20 --turns 4
# exp2 zuletzt: die Cold-Start-Erzwingung zerstört warme Container
run $PY measure.py --system lambda exp2 --n 20

echo ""
echo "======================= EC2 ==========================="
run $PY measure.py --system ec2 exp1 --n 20
run $PY measure.py --system ec2 exp3 --lengths 10 50 100 140 --reps 5
run $PY measure.py --system ec2 exp4
run $PY measure.py --system ec2 exp5 --reps 5
run $PY measure.py --system ec2 exp6 --levels 1 2 5 10 20 --turns 4
run $PY measure.py --system ec2 exp2 --n 20

echo ""
echo "============== LAMBDA 1-GB-VARIANTE ===================="
aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
    --memory-size 1024 --query "MemorySize" --output text
aws lambda wait function-updated --function-name "$FN" --region "$REGION"
run $PY measure.py --system lambda --tag 1gb exp1 --n 20
run $PY measure.py --system lambda --tag 1gb exp2 --n 10
echo "--- Memory zurueck auf 256 MB ---"
aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
    --memory-size 256 --query "MemorySize" --output text
aws lambda wait function-updated --function-name "$FN" --region "$REGION"

echo ""
echo "========= CLOUDWATCH REPORT-DATEN (Kostenbasis) ========"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
aws logs filter-log-events \
    --log-group-name "/aws/lambda/$FN" --region "$REGION" \
    --start-time $(( ($(date +%s) - 10800) * 1000 )) \
    --filter-pattern "REPORT" \
    --query "events[].message" --output text \
    > "data/lambda_report_${STAMP}.log"
echo "REPORT-Zeilen: $(wc -l < data/lambda_report_${STAMP}.log)"

echo ""
echo "======================================================"
echo " KAMPAGNE KOMPLETT: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "======================================================"
