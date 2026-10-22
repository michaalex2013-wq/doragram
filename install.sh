#!/usr/bin/env bash
# Запуск на сервере (root):
#   BOT_TOKEN='...' ADMIN_PASSWORD='...' bash /opt/doragram/install.sh
set -eu
: "${BOT_TOKEN:?нужна переменная BOT_TOKEN}" "${ADMIN_PASSWORD:?нужна переменная ADMIN_PASSWORD}"
APP=/opt/doragram
export DEBIAN_FRONTEND=noninteractive

echo "== 1/6 пакеты и файрвол"
apt-get update -y
apt-get install -y python3-venv python3-pip postgresql redis-server nginx git unzip ufw openssl curl
ufw allow 22 >/dev/null; ufw allow 80 >/dev/null; ufw allow 443 >/dev/null
ufw --force enable
systemctl enable --now postgresql redis-server

echo "== 2/6 база данных"
cd /tmp
DBPASS=$(openssl rand -hex 12)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='dora'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE USER dora WITH PASSWORD '$DBPASS';"
sudo -u postgres psql -c "ALTER USER dora WITH PASSWORD '$DBPASS';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='doragram'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE DATABASE doragram OWNER dora;"

echo "== 3/6 python-зависимости"
cd "$APP"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q -r "$APP/server/requirements.txt"
mkdir -p "$APP/media"

echo "== 4/6 настройки"
{
  echo "DB_URL=postgresql+asyncpg://dora:$DBPASS@localhost/doragram"
  echo "REDIS_URL=redis://localhost:6379/0"
  printf 'ADMIN_PASSWORD="%s"\n' "$ADMIN_PASSWORD"
  echo "BOT_TOKEN=$BOT_TOKEN"
  echo "BEHIND_PROXY=1"
  echo "MEDIA_DIR=$APP/media"
} > "$APP/server/.env"
chmod 600 "$APP/server/.env"

echo "== 5/6 службы и nginx"
cat > /etc/systemd/system/doragram-api.service <<'UNIT'
[Unit]
Description=DoraGram API
After=network.target postgresql.service redis-server.service
[Service]
WorkingDirectory=/opt/doragram/server
EnvironmentFile=/opt/doragram/server/.env
ExecStart=/opt/doragram/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/doragram-bot.service <<'UNIT'
[Unit]
Description=DoraGram bot
After=doragram-api.service
[Service]
WorkingDirectory=/opt/doragram/server
EnvironmentFile=/opt/doragram/server/.env
ExecStart=/opt/doragram/venv/bin/python bot.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/nginx/sites-available/doragram <<'NGX'
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 60M;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 3600;
    }
}
NGX
ln -sf /etc/nginx/sites-available/doragram /etc/nginx/sites-enabled/doragram
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx || systemctl restart nginx

echo "== 6/6 запуск"
systemctl daemon-reload
systemctl enable doragram-api doragram-bot
systemctl restart doragram-api
for i in $(seq 1 40); do
  curl -s -o /dev/null http://127.0.0.1:8000/openapi.json && break
  sleep 1
done
systemctl restart doragram-bot
sleep 3
echo
echo "api: $(systemctl is-active doragram-api || true)   bot: $(systemctl is-active doragram-bot || true)"
echo "http-код API через nginx (ждём 401): $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/api/gifts)"
if ! systemctl is-active --quiet doragram-api; then journalctl -u doragram-api -n 25 --no-pager; fi
if ! systemctl is-active --quiet doragram-bot; then journalctl -u doragram-bot -n 25 --no-pager; fi
