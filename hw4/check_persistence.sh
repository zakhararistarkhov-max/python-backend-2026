#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
BASE_URL="${BASE_URL:-http://localhost:8080}"

json_id() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])'
}

wait_shop() {
  for _ in $(seq 1 60); do
    if curl -sf "$BASE_URL/item?limit=1" > /dev/null; then
      return 0
    fi
    sleep 2
  done
  docker compose logs shop postgres
  return 1
}

snapshot() {
  curl -sf "$BASE_URL/item/$ITEM_ID"
  echo
  curl -sf "$BASE_URL/cart/$CART_ID"
  echo
}

verify() {
  wait_shop
  actual="$(snapshot)"
  if [ "$actual" != "$EXPECTED" ]; then
    echo "data changed after $1:"
    echo "$actual"
    exit 1
  fi
  echo "ok after $1"
}

docker compose up -d --build --wait shop
wait_shop

ITEM_ID="$(curl -sf -X POST "$BASE_URL/item" -H 'content-type: application/json' \
  -d '{"name": "persistent item", "price": 12.5}' | json_id)"
CART_ID="$(curl -sf -X POST "$BASE_URL/cart" | json_id)"
curl -sf -X POST "$BASE_URL/cart/$CART_ID/add/$ITEM_ID" > /dev/null
curl -sf -X POST "$BASE_URL/cart/$CART_ID/add/$ITEM_ID" > /dev/null
EXPECTED="$(snapshot)"
echo "$EXPECTED"

docker compose down
docker compose up -d --wait shop
verify "docker compose down && docker compose up"

docker compose restart shop postgres
verify "docker compose restart shop postgres"

echo "persistence check passed"
