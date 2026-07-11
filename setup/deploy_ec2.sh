#!/bin/bash
# =============================================================================
# Deployment der EC2-Baseline (t3.micro, Amazon Linux 2023)
# =============================================================================
# Startet eine t3.micro-Instanz, installiert Python 3.12, kopiert server.py
# per user-data auf die Instanz und startet den FastAPI-Server als
# systemd-Service (Port 8000). Der Zugriff wird per Security Group auf die
# öffentliche IP des aufrufenden Rechners beschränkt.
#
# Voraussetzungen:
#   - AWS CLI konfiguriert
#   - export GROQ_API_KEY="gsk_..."
#
# Verwendung:
#   ./deploy_ec2.sh            # startet die Instanz, gibt die /chat-URL aus
#
# WICHTIG: Die Instanz läuft bis sie manuell terminiert wird:
#   aws ec2 terminate-instances --instance-ids <id> --region eu-central-1
# =============================================================================

set -e

REGION="eu-central-1"
INSTANCE_TYPE="t3.micro"
SG_NAME="chatbot-baseline-sg"
GROQ_MODEL="${GROQ_MODEL:-llama-3.3-70b-versatile}"
SERVER_PY="$(dirname "$0")/../ec2-chatbot/server.py"

if [ -z "$GROQ_API_KEY" ]; then
    echo "FEHLER: GROQ_API_KEY ist nicht gesetzt!"
    exit 1
fi
if [ ! -f "$SERVER_PY" ]; then
    echo "FEHLER: $SERVER_PY nicht gefunden."
    exit 1
fi

echo "=== Schritt 1: Security Group (Port 8000, nur eigene IP) ==="
MY_IP=$(curl -s https://checkip.amazonaws.com | tr -d '[:space:]')
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query "Vpcs[0].VpcId" --output text --region "$REGION")

SG_ID=$(aws ec2 describe-security-groups \
    --filters Name=group-name,Values="$SG_NAME" Name=vpc-id,Values="$VPC_ID" \
    --query "SecurityGroups[0].GroupId" --output text --region "$REGION" 2>/dev/null || echo "None")
if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    SG_ID=$(aws ec2 create-security-group --group-name "$SG_NAME" \
        --description "chatbot baseline: port 8000 from measuring host" \
        --vpc-id "$VPC_ID" --query "GroupId" --output text --region "$REGION")
    echo "Security Group $SG_NAME erstellt: $SG_ID"
else
    echo "Security Group existiert: $SG_ID"
fi
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 8000 --cidr "$MY_IP/32" --region "$REGION" 2>/dev/null \
    && echo "Ingress 8000 von $MY_IP/32 erlaubt." \
    || echo "Ingress-Regel existiert bereits."

echo ""
echo "=== Schritt 2: Aktuelles Amazon-Linux-2023-AMI ermitteln ==="
AMI_ID=$(aws ssm get-parameter \
    --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
    --query "Parameter.Value" --output text --region "$REGION")
echo "AMI: $AMI_ID"

echo ""
echo "=== Schritt 3: user-data (Bootstrap-Skript) bauen ==="
USERDATA_FILE=$(mktemp)
{
    cat <<HEAD
#!/bin/bash
set -ex
dnf install -y python3.12 python3.12-pip
mkdir -p /opt/chatbot/data
cat > /opt/chatbot/server.py <<'PYEOF'
HEAD
    cat "$SERVER_PY"
    cat <<TAIL
PYEOF
python3.12 -m pip install --quiet fastapi uvicorn requests
cat > /etc/systemd/system/chatbot.service <<EOF
[Unit]
Description=EC2 Stateful AI Chatbot (baseline)
After=network.target

[Service]
Environment=GROQ_API_KEY=$GROQ_API_KEY
Environment=GROQ_MODEL=$GROQ_MODEL
WorkingDirectory=/opt/chatbot
ExecStart=/usr/bin/python3.12 -m uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now chatbot
TAIL
} > "$USERDATA_FILE"
echo "user-data: $(wc -c < "$USERDATA_FILE") Bytes (Limit 16384)"

echo ""
echo "=== Schritt 4: Instanz starten ==="
INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type "$INSTANCE_TYPE" \
    --security-group-ids "$SG_ID" \
    --user-data "file://$USERDATA_FILE" \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=chatbot-baseline}]' \
    --query "Instances[0].InstanceId" --output text --region "$REGION")
rm -f "$USERDATA_FILE"
echo "Instanz: $INSTANCE_ID"

aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"
PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query "Reservations[0].Instances[0].PublicIpAddress" --output text --region "$REGION")

echo ""
echo "=== Schritt 5: Warten bis der Service antwortet (Bootstrap ~2-4 min) ==="
URL="http://$PUBLIC_IP:8000"
for i in $(seq 1 60); do
    if curl -s --max-time 3 "$URL/health" | grep -q '"ok"'; then
        echo ""
        echo "=========================================="
        echo "  EC2-BASELINE LÄUFT"
        echo "=========================================="
        echo "Instance ID: $INSTANCE_ID"
        echo "Health:      $URL/health"
        echo "Chat-URL:    $URL/chat"
        echo ""
        echo "Terminieren nach der Messung:"
        echo "  aws ec2 terminate-instances --instance-ids $INSTANCE_ID --region $REGION"
        exit 0
    fi
    sleep 5
done
echo "FEHLER: Service nach 5 Minuten nicht erreichbar. Console-Log prüfen:"
echo "  aws ec2 get-console-output --instance-id $INSTANCE_ID --region $REGION"
exit 1
