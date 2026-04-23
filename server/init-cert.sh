#!/bin/bash
# Запускать ОДИН РАЗ для получения Let's Encrypt сертификата
# Использование: ./init-cert.sh <domain> <email>
# Пример: ./init-cert.sh pg-mcp.duckdns.org admin@company.com
set -e

DOMAIN=${1}
EMAIL=${2}

if [ -z "$DOMAIN" ] || [ -z "$EMAIL" ]; then
    echo "Использование: ./init-cert.sh <domain> <email>"
    echo "Пример: ./init-cert.sh pg-mcp.duckdns.org admin@company.com"
    exit 1
fi

echo "▶ Остановить nginx если запущен..."
docker compose stop nginx 2>/dev/null || true

echo "▶ Получить сертификат Let's Encrypt для $DOMAIN..."
mkdir -p certbot/conf certbot/www

docker run --rm \
    -p 80:80 \
    -v "$(pwd)/certbot/conf:/etc/letsencrypt" \
    certbot/certbot certonly \
    --standalone \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    -d "$DOMAIN"

echo "▶ Подставить домен в nginx.conf..."
sed -i "s/YOUR_DOMAIN/$DOMAIN/g" nginx.conf

echo "▶ Запустить все сервисы..."
docker compose up -d

echo ""
echo "✓ Готово! Сервер доступен по адресу: https://$DOMAIN:19000"
echo "  Проверка: curl https://$DOMAIN:19000/health"
