#!/usr/bin/env bash
# PH Store — HTTPS on api.novaraca.com through the Traefik that already owns :80/:443.
# Runs ON THE SERVER. Touches only /opt/phstore. Does not edit Traefik, tana or any other app.
# Re-runnable. Rollback:  bash phstore-tls.sh --rollback
set -uo pipefail

DOMAIN="${DOMAIN:-api.novaraca.com}"
DIR="/opt/phstore"
TLS_FILE="$DIR/docker-compose.tls.yml"
ENV_FILE="$DIR/.env"
cd "$DIR" || { echo "no $DIR"; exit 1; }

say(){ printf '\n== %s\n' "$*"; }

if [[ "${1:-}" == "--rollback" ]]; then
  say "rollback"
  sed -i '/^COMPOSE_FILE=/d' "$ENV_FILE"
  rm -f "$TLS_FILE"
  docker compose up -d api
  echo "done — back to http on :8099 only"
  exit 0
fi

say "1/6 find Traefik"
TRAEFIK=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /traefik/ {print $1; exit}')
[[ -n "$TRAEFIK" ]] || { echo "no running Traefik container found — stop"; exit 1; }
echo "traefik container: $TRAEFIK"

say "2/6 learn its settings from the apps already behind it"
ALL_LABELS=$(docker ps -q | xargs docker inspect --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}')
RESOLVER=$(echo "$ALL_LABELS" | grep -E '^traefik\.http\.routers\.[^.]+\.tls\.certresolver=' | cut -d= -f2 | sort | uniq -c | sort -rn | awk 'NR==1{print $2}')
ENTRY=$(echo "$ALL_LABELS" | grep -E '^traefik\.http\.routers\.[^.]+\.entrypoints=' | cut -d= -f2 | grep -iv '^web$\|^http$' | sort | uniq -c | sort -rn | awk 'NR==1{print $2}')
NET=$(echo "$ALL_LABELS" | grep -E '^traefik\.docker\.network=' | cut -d= -f2 | sort | uniq -c | sort -rn | awk 'NR==1{print $2}')
TMODE=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$TRAEFIK")
TNETS=$(docker inspect "$TRAEFIK" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}')
echo "traefik network mode: $TMODE   traefik networks: $TNETS"
API_CID=$(docker compose ps -q api | head -1)
[[ -n "$API_CID" ]] || { echo "phstore api container not running — stop"; exit 1; }
API_NET=$(docker inspect "$API_CID" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -1)
HOSTMODE=0
if [[ "$TMODE" == "host" ]]; then
  HOSTMODE=1; NET="$API_NET"          # Traefik on the host network reaches the api on its own network
elif [[ -z "$NET" ]]; then
  NET=$(echo "$TNETS" | tr ' ' '\n' | grep -v '^bridge$' | grep -v '^$' | head -1)
fi
if [[ -z "$NET" && " $TNETS " == *" bridge "* ]]; then
  HOSTMODE=1; NET="$API_NET"          # Traefik only on the default bridge: route by container IP
fi
echo "certresolver: ${RESOLVER:-?}   entrypoint: ${ENTRY:-?}   network: ${NET:-?}   host-routing: $HOSTMODE"
[[ -n "$RESOLVER" && -n "$ENTRY" && -n "$NET" ]] || { echo "could not detect all three — stop, nothing changed"; exit 1; }
docker network inspect "$NET" >/dev/null || { echo "network $NET missing — stop"; exit 1; }

say "3/6 DNS check"
IP_SRV=$(curl -s4 --max-time 5 https://ifconfig.me || true)
IP_DNS=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')
echo "server ip: $IP_SRV   $DOMAIN -> $IP_DNS"
[[ -z "$IP_SRV" || "$IP_SRV" == "$IP_DNS" ]] || { echo "DNS does not point here — stop"; exit 1; }

say "4/6 write $TLS_FILE"
LABELS="      - traefik.enable=true
      - traefik.docker.network=$NET
      - traefik.http.routers.phstore-api.rule=Host(\`$DOMAIN\`)
      - traefik.http.routers.phstore-api.entrypoints=$ENTRY
      - traefik.http.routers.phstore-api.tls=true
      - traefik.http.routers.phstore-api.tls.certresolver=$RESOLVER
      - traefik.http.services.phstore-api.loadbalancer.server.port=8099"
if [[ "$HOSTMODE" == "1" ]]; then
cat > "$TLS_FILE" <<YAML
# Added by phstore-tls.sh — HTTPS through the shared Traefik.
services:
  api:
    labels:
$LABELS
YAML
else
cat > "$TLS_FILE" <<YAML
# Added by phstore-tls.sh — HTTPS through the shared Traefik.
services:
  api:
    labels:
$LABELS
    networks: [phstore, traefik_public]
networks:
  traefik_public:
    external: true
    name: $NET
YAML
fi

FILES="docker-compose.yml"
[[ -f docker-compose.override.yml ]] && FILES="$FILES:docker-compose.override.yml"
FILES="$FILES:docker-compose.tls.yml"
touch "$ENV_FILE"
cp "$ENV_FILE" "$ENV_FILE.bak-tls-$(date +%F-%H%M)"
sed -i '/^COMPOSE_FILE=/d' "$ENV_FILE"
echo "COMPOSE_FILE=$FILES" >> "$ENV_FILE"
docker compose config -q || { echo "compose config invalid — rolling back"; bash "$0" --rollback; exit 1; }

say "5/6 recreate the api container only (db untouched)"
docker compose up -d --no-deps api || { echo "up failed — rolling back"; bash "$0" --rollback; exit 1; }

say "6/6 wait for the certificate"
for i in $(seq 1 24); do
  if curl -fsS --max-time 5 "https://$DOMAIN/healthz" >/dev/null 2>&1; then
    echo "OK  https://$DOMAIN/healthz"
    curl -sS "https://$DOMAIN/healthz"; echo
    echo; echo "http on :8099 still works too. Close it later once every phone uses https."
    exit 0
  fi
  sleep 5
done
echo "no valid https yet after 2 min. Traefik log:"
docker logs --tail 30 "$TRAEFIK" 2>&1 | grep -i -E "novaraca|acme|error" || true
echo "nothing broken: http :8099 still serves. Re-run later, or: bash $0 --rollback"
exit 1
