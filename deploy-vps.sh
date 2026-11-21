#!/usr/bin/env bash
# Update the existing Context Mail systemd service at Play2Go.
set -euo pipefail

commit=${1:?Pass the full GitHub commit SHA}
[[ $commit =~ ^[0-9a-f]{40}$ ]] || exit 2
service=context-mail.service
release="/opt/context-mail-releases/$commit"
dropin="/etc/systemd/system/$service.d"
override="$dropin/30-full-corpus.conf"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

systemctl is-active --quiet "$service"
test ! -e "$release"
curl --fail --silent --show-error --location --max-time 90 \
  "https://codeload.github.com/vli1rus/context-mail-search/tar.gz/$commit" -o "$work/code.tgz"

python3 - "$work/corpus.zip" <<'PY'
import hashlib
import json
import sys
from pathlib import Path
from urllib.request import urlopen

with urlopen('https://cloud.mail.ru/api/v2/dispatcher', timeout=30) as response:
    base = json.load(response)['body']['weblink_get'][0]['url']
with urlopen(base.rstrip('/') + '/8S62/oQDJV17v6', timeout=60) as response:
    data = response.read(10_000_000)
if hashlib.sha256(data).hexdigest() != '2df885f77be8f8aa750b484439fc99dfc2a401fce53306160c03ad95cae0de03':
    raise SystemExit('Unexpected corpus digest; existing service untouched.')
Path(sys.argv[1]).write_bytes(data)
PY

install -d "$release"
tar -xzf "$work/code.tgz" --strip-components=1 -C "$release"
install -m 0644 "$work/corpus.zip" "$release/corpus.zip"
cd "$release"
python3 -B -m unittest discover -s tests -q
python3 -B -m mailsearch.server --dataset "$release/corpus.zip" \
  --host 127.0.0.1 --port 18765 --cache-dir "$work/cache" >"$work/preflight.log" 2>&1 &
candidate=$!
trap 'kill "$candidate" 2>/dev/null || true; rm -rf -- "$work"' EXIT

check_mail() {
  python3 - "$1" <<'PY'
import json
import sys
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

origin = f'http://127.0.0.1:{sys.argv[1]}'
for attempt in range(60):
    try:
        with urlopen(origin + '/api/meta', timeout=3) as response:
            meta = json.load(response)
        break
    except URLError:
        if attempt == 59:
            raise
        time.sleep(1)
assert meta['count'] == 16376 and len(meta['mailboxes']) == 12 and not meta['demo'], meta
with urlopen(origin + '/', timeout=5) as response:
    assert b'mail-mark' in response.read()
for query in ('аптека', 'курьер'):
    params = urlencode(dict(q=query, mode='smart', limit=10))
    with urlopen(origin + '/api/search?' + params, timeout=5) as response:
        assert json.load(response)['items'], query
print('Verified: 16,376 letters, 12 mailboxes, homepage and two HTTP searches.')
PY
}

if ! check_mail 18765; then
  cat "$work/preflight.log"
  exit 1
fi
kill "$candidate"
wait "$candidate" || true
trap 'rm -rf -- "$work"' EXIT

install -d "$dropin"
if test -f "$override"; then cp "$override" "$work/previous.conf"; fi
cat >"$override" <<EOF
[Service]
WorkingDirectory=$release
ExecStart=
ExecStart=/usr/bin/python3 -B -m mailsearch.server --dataset $release/corpus.zip --host 0.0.0.0 --port 8765 --cache-dir /var/cache/context-mail
EOF
if systemctl daemon-reload && systemctl restart "$service" && check_mail 8765; then
  printf 'Deployed %s\n' "$commit"
else
  if test -f "$work/previous.conf"; then
    cp "$work/previous.conf" "$override"
  else
    rm -f "$override"
  fi
  systemctl daemon-reload
  systemctl restart "$service"
  exit 1
fi
