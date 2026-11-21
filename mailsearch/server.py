import argparse
import json
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter_ns
from urllib.parse import parse_qs, urlsplit

from .data import DEMO_CORPUS, ROOT, check_day, load_dataset, load_letters, locate_dataset
from .engine import SearchEngine
from .semantic import E5Encoder, add_model_args


PATHS = {'/': ('index.html', 'text/html; charset=utf-8'), '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
         '/app.js': ('app.js', 'text/javascript; charset=utf-8')}


def summary(letter):
    return {'doc_id': letter.doc_id, 'mailbox_id': letter.mailbox_id, 'subject': letter.subject,
            'sender': letter.sender, 'date': letter.date, 'preview': ' '.join(letter.body[:550].split())[:240]}


class MailServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, engine, demo, assets=None):
        super().__init__(address, MailHandler)
        self.engine = engine
        self.demo = demo
        self.reference_day = '2026-11-21' if demo else max(engine.dates, default='') or date.today().isoformat()
        self.assets = assets if assets is not None else {
            path: ((ROOT / 'web' / name).read_bytes(), mime) for path, (name, mime) in PATHS.items()}


class MailHandler(BaseHTTPRequestHandler):
    def write_response(self, status, body, mime, started=None):
        elapsed = (perf_counter_ns() - started) / 1e6 if started else None
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        if elapsed is not None:
            self.send_header('Server-Timing', f'backend;dur={elapsed:.3f}')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_response(self, data, status=HTTPStatus.OK, started=None):
        payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
        self.write_response(status, payload, 'application/json; charset=utf-8', started)

    def do_GET(self):
        started = perf_counter_ns()
        engine = self.server.engine
        try:
            url = urlsplit(self.path)
            params = parse_qs(url.query, max_num_fields=16)
            def get(key, default=''):
                return params.get(key, [default])[-1].strip()

            if url.path in self.server.assets:
                data, mime = self.server.assets[url.path]
                return self.write_response(HTTPStatus.OK, data, mime)
            if url.path == '/favicon.ico':
                return self.write_response(HTTPStatus.NO_CONTENT, b'', 'image/x-icon')
            if url.path == '/api/meta':
                return self.json_response({'mailboxes': [{'id': box, 'count': len(indexes)} for box, indexes in sorted(engine.boxes.items())],
                                           'count': len(engine.letters), 'semantic': bool(engine.dense), 'demo': self.server.demo,
                                           'reference_day': self.server.reference_day})
            if url.path == '/api/letter':
                index = engine.by_id.get(get('id'))
                if index is not None:
                    return self.json_response(engine.letters[index].serializable())
                return self.json_response({'error': 'Письмо не найдено.'}, HTTPStatus.NOT_FOUND)
            mailbox = get('mailbox', get('synthetic_user'))
            after = check_day(get('after'))
            before = check_day(get('before'))
            if after and before and after > before:
                raise ValueError('Начало периода позже окончания.')
            limit = min(100, max(1, int(get('limit', '25'))))
            if url.path == '/api/letters':
                offset = max(0, int(get('offset', '0')))
                letters, total = engine.browse(mailbox, after, before, offset, limit, check_day(get('as_of')))
                return self.json_response({'items': [summary(letter) for letter in letters], 'total': total})
            if url.path == '/api/search':
                query = get('q')
                if not query or len(query) > 600:
                    raise ValueError('Введите запрос длиной до 600 символов.')
                mode = get('mode', 'smart')
                if mode not in {'smart', 'semantic', 'lexical'}:
                    raise ValueError('Неизвестный режим поиска.')
                as_of = check_day(get('as_of', get('query_date', date.today().isoformat())))
                hits = engine.search(query, mode, limit, mailbox, after, before, as_of)
                output = [dict(summary(engine.letters[hit.index]), evidence=hit.evidence, signals=hit.signals) for hit in hits]
                return self.json_response({'items': output, 'mode': mode}, started=started)
        except ValueError as exc:
            return self.json_response({'error': str(exc)}, HTTPStatus.BAD_REQUEST)
        self.json_response({'error': 'Страница не найдена.'}, HTTPStatus.NOT_FOUND)

    def log_request(self, code='-', size='-'):
        # Avoid putting search terms and synthetic mailbox identifiers in logs.
        if isinstance(code, int) and code >= 500:
            self.log_error('HTTP %s', code)


def main():
    parser = argparse.ArgumentParser(description='Run the mail search web service')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dataset', type=Path, help='Organizer ZIP or extracted directory')
    group.add_argument('--corpus', type=Path, help='Standalone mail file or directory')
    group.add_argument('--demo', action='store_true')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    add_model_args(parser)
    args = parser.parse_args()
    try:
        dataset = None if args.demo or args.corpus else args.dataset or locate_dataset()
        corpus = load_dataset(dataset, args.cache_dir) / 'documents.jsonl' if dataset else args.corpus or DEMO_CORPUS
        encoder = E5Encoder(args.model_dir) if args.model_dir else None
        engine = SearchEngine(load_letters(corpus), encoder, args.cache_dir)
        with MailServer((args.host, args.port), engine, corpus == DEMO_CORPUS) as server:
            print(f'http://{args.host}:{server.server_port} · {len(engine.letters):,} letters · {"E5" if engine.dense else "local search"}', flush=True)
            server.serve_forever()
    except (OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
