#!/bin/bash
# =============================================================================
# Vollständiges Deployment-Skript
# Stateful AI Agent auf AWS Lambda + DynamoDB
# =============================================================================
# Voraussetzungen:
#   - AWS CLI installiert und konfiguriert (aws configure)
#   - Python 3.12 installiert
#   - zip installiert
#
# Verwendung:
#   chmod +x deploy.sh
#   export GROQ_API_KEY="gsk_..."
#   export GROQ_MODEL="llama-3.3-70b-versatile"   # optional
#   ./deploy.sh
# =============================================================================

set -e  # Abbruch bei Fehler

# --------------------------------------------------------------------------
# KONFIGURATION - hier anpassen
# --------------------------------------------------------------------------
REGION="eu-central-1"           # AWS Region (Frankfurt)
TABLE_NAME="chat_sessions"       # DynamoDB Tabellenname
FUNCTION_NAME="chatbot-handler"  # Lambda Funktionsname
ROLE_NAME="lambda-chatbot-role"  # IAM Rollenname
RUNTIME="python3.12"
LAMBDA_DIR="../lambda"

# --------------------------------------------------------------------------
# Schritt 0: Voraussetzungen prüfen
# --------------------------------------------------------------------------
echo ""
echo "=== Schritt 0: Voraussetzungen prüfen ==="

if [ -z "$GROQ_API_KEY" ]; then
    echo "FEHLER: GROQ_API_KEY ist nicht gesetzt!"
    echo "  export GROQ_API_KEY='gsk_...'"
    exit 1
fi

GROQ_MODEL="${GROQ_MODEL:-llama-3.3-70b-versatile}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "AWS Account ID: $ACCOUNT_ID"
echo "Region: $REGION"

# --------------------------------------------------------------------------
# Schritt 1: DynamoDB Tabelle erstellen
# --------------------------------------------------------------------------
echo ""
echo "=== Schritt 1: DynamoDB Tabelle erstellen ==="

aws dynamodb create-table \
    --table-name "$TABLE_NAME" \
    --attribute-definitions AttributeName=session_id,AttributeType=S \
    --key-schema AttributeName=session_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$REGION" 2>/dev/null && echo "Tabelle $TABLE_NAME erstellt." \
    || echo "Tabelle $TABLE_NAME existiert bereits, überspringe."

# TTL aktivieren (DynamoDB löscht abgelaufene Sessions automatisch)
aws dynamodb update-time-to-live \
    --table-name "$TABLE_NAME" \
    --time-to-live-specification "Enabled=true, AttributeName=ttl" \
    --region "$REGION" 2>/dev/null || true

echo "TTL auf Attribut 'ttl' aktiviert."

# Warten bis Tabelle aktiv ist
echo "Warte bis Tabelle aktiv ist..."
aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$REGION"
echo "Tabelle ist aktiv."

# --------------------------------------------------------------------------
# Schritt 2: IAM Rolle für Lambda erstellen
# --------------------------------------------------------------------------
echo ""
echo "=== Schritt 2: IAM Rolle erstellen ==="

ROLE_ARN=$(aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document file://iam_trust_policy.json \
    --query "Role.Arn" --output text 2>/dev/null) \
    && echo "Rolle $ROLE_NAME erstellt." \
    || ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query "Role.Arn" --output text)

echo "Role ARN: $ROLE_ARN"

# Berechtigungen anhängen
aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "chatbot-dynamodb-logs" \
    --policy-document file://iam_permissions_policy.json
echo "Berechtigungen (DynamoDB + CloudWatch Logs) hinzugefügt."

# Kurz warten bis IAM propagiert ist
echo "Warte 10 Sekunden auf IAM Propagierung..."
sleep 10

# --------------------------------------------------------------------------
# Schritt 3: Lambda Deployment-Paket erstellen
# --------------------------------------------------------------------------
echo ""
echo "=== Schritt 3: Lambda Deployment-Paket bauen ==="

# Temporäres Build-Verzeichnis
BUILD_DIR=$(mktemp -d)
cp "$LAMBDA_DIR/lambda_function.py" "$BUILD_DIR/"

# Keine externen Abhängigkeiten – Groq wird über urllib (Python stdlib) aufgerufen

# ZIP-Paket erstellen
ZIP_FILE="lambda_package.zip"
(cd "$BUILD_DIR" && zip -r9 "$OLDPWD/$ZIP_FILE" . -q)
rm -rf "$BUILD_DIR"
echo "Deployment-Paket erstellt: $ZIP_FILE ($(du -sh $ZIP_FILE | cut -f1))"

# --------------------------------------------------------------------------
# Schritt 4: Lambda Funktion erstellen oder aktualisieren
# --------------------------------------------------------------------------
echo ""
echo "=== Schritt 4: Lambda Funktion deployen ==="

# Prüfen ob Funktion bereits existiert
FUNCTION_EXISTS=$(aws lambda get-function \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --query "Configuration.FunctionName" --output text 2>/dev/null || echo "NICHT_GEFUNDEN")

if [ "$FUNCTION_EXISTS" = "NICHT_GEFUNDEN" ]; then
    aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime "$RUNTIME" \
        --role "$ROLE_ARN" \
        --handler "lambda_function.lambda_handler" \
        --zip-file "fileb://$ZIP_FILE" \
        --timeout 60 \
        --memory-size 256 \
        --environment "Variables={GROQ_API_KEY=$GROQ_API_KEY,GROQ_MODEL=$GROQ_MODEL,DYNAMODB_TABLE=$TABLE_NAME}" \
        --region "$REGION" \
        --output text > /dev/null
    echo "Lambda Funktion $FUNCTION_NAME erstellt."
else
    # Code aktualisieren
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file "fileb://$ZIP_FILE" \
        --region "$REGION" \
        --output text > /dev/null
    # Warten bis das Code-Update abgeschlossen ist (sonst ResourceConflict)
    aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$REGION"
    # Umgebungsvariablen aktualisieren
    aws lambda update-function-configuration \
        --function-name "$FUNCTION_NAME" \
        --environment "Variables={GROQ_API_KEY=$GROQ_API_KEY,GROQ_MODEL=$GROQ_MODEL,DYNAMODB_TABLE=$TABLE_NAME}" \
        --region "$REGION" \
        --output text > /dev/null
    echo "Lambda Funktion $FUNCTION_NAME aktualisiert."
fi

# Warten bis Funktion bereit ist
aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$REGION" 2>/dev/null || true

rm -f "$ZIP_FILE"

# --------------------------------------------------------------------------
# Schritt 5: API Gateway (HTTP API) erstellen
# --------------------------------------------------------------------------
echo ""
echo "=== Schritt 5: API Gateway erstellen ==="

LAMBDA_ARN="arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$FUNCTION_NAME"

# Prüfen ob API bereits existiert
API_ID=$(aws apigatewayv2 get-apis \
    --region "$REGION" \
    --query "Items[?Name=='chatbot-api'].ApiId" \
    --output text 2>/dev/null)

if [ -z "$API_ID" ]; then
    API_ID=$(aws apigatewayv2 create-api \
        --name "chatbot-api" \
        --protocol-type HTTP \
        --cors-configuration \
            AllowOrigins='["*"]',AllowMethods='["POST","OPTIONS"]',AllowHeaders='["Content-Type"]' \
        --region "$REGION" \
        --query "ApiId" --output text)
    echo "API Gateway erstellt: $API_ID"
else
    echo "API Gateway existiert bereits: $API_ID"
fi

# Lambda-Integration erstellen
INTEGRATION_ID=$(aws apigatewayv2 create-integration \
    --api-id "$API_ID" \
    --integration-type AWS_PROXY \
    --integration-uri "$LAMBDA_ARN" \
    --payload-format-version "2.0" \
    --region "$REGION" \
    --query "IntegrationId" --output text 2>/dev/null || true)

if [ -n "$INTEGRATION_ID" ]; then
    # Route erstellen: POST /chat
    aws apigatewayv2 create-route \
        --api-id "$API_ID" \
        --route-key "POST /chat" \
        --target "integrations/$INTEGRATION_ID" \
        --region "$REGION" > /dev/null 2>&1 || true

    # Stage deployen
    aws apigatewayv2 create-stage \
        --api-id "$API_ID" \
        --stage-name '$default' \
        --auto-deploy \
        --region "$REGION" > /dev/null 2>&1 || true

    # Lambda Berechtigung für API Gateway
    aws lambda add-permission \
        --function-name "$FUNCTION_NAME" \
        --statement-id "apigateway-invoke" \
        --action "lambda:InvokeFunction" \
        --principal "apigateway.amazonaws.com" \
        --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT_ID:$API_ID/*/*" \
        --region "$REGION" > /dev/null 2>&1 || true
fi

API_URL="https://$API_ID.execute-api.$REGION.amazonaws.com/chat"

# --------------------------------------------------------------------------
# Fertig
# --------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "  DEPLOYMENT ERFOLGREICH ABGESCHLOSSEN"
echo "=========================================="
echo ""
echo "API URL: $API_URL"
echo ""
echo "Client starten:"
echo "  cd ../client"
echo "  pip install -r requirements.txt"
echo "  export API_URL=\"$API_URL\""
echo "  python client.py"
echo ""
echo "Groq Modell: $GROQ_MODEL"
echo ""
echo "Lambda Funktion testen (direkt):"
echo "  aws lambda invoke --function-name $FUNCTION_NAME \\"
echo "    --payload '{\"body\":\"{\\\"session_id\\\":\\\"test-123\\\",\\\"message\\\":\\\"Hallo!\\\"}\"}' \\"
echo "    --cli-binary-format raw-in-base64-out \\"
echo "    --region $REGION output.json && cat output.json"
echo ""
