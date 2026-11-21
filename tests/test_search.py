import json
import io
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener
from zipfile import ZipFile

from mailsearch.data import DEMO_CORPUS, Letter, load_dataset, load_letters
from mailsearch.engine import SearchEngine, anchors, split_passages
from mailsearch.evaluate import metrics, percentile_nearest_rank, read_queries
from mailsearch.server import MailHandler, MailServer
from mailsearch.temporal import received_period, relative_range


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = SearchEngine(load_letters(DEMO_CORPUS))

    def search(self, query, **options):
        return [(self.engine.letters[hit.index].doc_id, hit) for hit in self.engine.search(query, **options)]

    def test_paraphrase_finds_the_letter_and_quotes_real_text(self):
        settings = {'mailbox': 'Поездки', 'as_of': '2026-11-21'}
        query = 'Где писали, что самолёт улетит позже?'
        doc_id, hit = self.search(query, **settings)[0]
        self.assertEqual(doc_id, 'f-401')
        self.assertIn('18:40', hit.evidence)
        self.assertIn(hit.evidence, self.engine.letters[hit.index].body)
        lexical = [item for item, _ in self.search(query, mode='lexical', **settings)]
        self.assertNotIn(doc_id, lexical)

    def test_identifier_and_temporal_collision(self):
        self.assertEqual(anchors('SN‑401 / SN-401 / SN–401'), {'sn401'})
        hits = self.search('Вчерашнее уведомление о переносе рейса SN-401', mailbox='Поездки', as_of='2026-11-21')
        self.assertEqual(hits[0][0], 'f-401')
        self.assertIn('Совпала дата', hits[0][1].signals)
        self.assertIn('Совпал номер или адрес', hits[0][1].signals)
        self.assertNotIn('f-future', [item[0] for item in hits])
        self.assertNotIn('f-other-flight', [item[0] for item in hits])

    def test_unknown_dates_are_eligible_as_of_but_not_in_a_date_range(self):
        values = {'mailbox': 'Покупки', 'as_of': '2026-11-19'}
        query = 'Ремонт 893160'
        self.assertEqual(self.search(query, **values)[0][0], 'shop-undated')
        hits = self.search(query, after='2026-11-01', **values)
        self.assertNotIn('shop-undated', [item[0] for item in hits])

    def test_regex_does_not_turn_common_amount_into_a_number(self):
        self.assertEqual(anchors('Оплачено 299 рублей 20 ноября в 16:30'), set())
        self.assertEqual(anchors('где номер 00201 от support@example.com'), {'00201', 'support@example.com'})

    def test_passage_at_end_of_long_letter_survives(self):
        text = ' '.join(f'раздел{i}' for i in range(150)) + ' Уникальный номер AB-85128.'
        self.assertTrue(split_passages(text)[-1].endswith('AB-85128.'))
        engine = SearchEngine([Letter('long', 'box', '', '', '', text)])
        self.assertIn('AB-85128', engine.search('Где номер AB-85128')[0].evidence)

    def test_time_and_metrics(self):
        self.assertEqual(relative_range('вчера', '2024-03-01'), ('2024-02-29', '2024-02-29'))
        self.assertEqual(relative_range('в прошлом месяце', '2026-01-03'), ('2025-12-01', '2025-12-31'))
        self.assertEqual(relative_range('за последние 3 дня', '2026-06-02'), ('2026-05-31', '2026-06-02'))
        recall, ndcg, rr = metrics(['wrong', 'b', 'a'], {'a': 1, 'b': 1}, 2)
        self.assertEqual(recall, .5)
        self.assertAlmostEqual(ndcg, .3868528)
        self.assertEqual(rr, .5)
        self.assertEqual(percentile_nearest_rank(list(range(1, 101)), .99), 99)

    def test_official_zip_layout_manifest_fields_and_scope(self):
        import hashlib

        corpus = (json.dumps({'doc_id': '007', 'synthetic_user': 'synthetic_03', 'doc': 'Тема: Сообщение\n\nПисьмо.', 'doc_dt': None}, ensure_ascii=False) + '\n').encode()
        query = (json.dumps({'query_id': '0001', 'synthetic_user': 'synthetic_03', 'query': 'письмо', 'query_date': '2026-11-21'}, ensure_ascii=False) + '\n').encode()
        members = {'documents.jsonl': corpus, 'queries_dev.jsonl': query}
        manifest = json.dumps({'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in members.items()}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'data.zip'
            with ZipFile(archive, 'w') as bundle:
                for name, data in dict(members, **{'manifest.json': manifest}).items():
                    bundle.writestr(f'dataset/{name}', data)
                bundle.writestr('../not-allowed.txt', 'ignored')
            extracted = load_dataset(archive, root / 'cache')
            self.assertFalse(list(root.rglob('not-allowed.txt')))
            self.assertEqual(load_letters(extracted / 'documents.jsonl')[0], Letter('007', 'synthetic_03', 'Сообщение', '', '', 'Тема: Сообщение\n\nПисьмо.'))
            self.assertEqual(next(read_queries(extracted / 'queries_dev.jsonl')), ('0001', 'письмо', 'synthetic_03', '2026-11-21'))
            (extracted / 'documents.jsonl').write_bytes(b' ' * len(corpus))
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                load_dataset(archive, root / 'cache')

    def test_dense_cache_and_actual_winning_passage(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('NumPy needed to test the optional dense index')

        class StubEncoder:
            cache_key = 'test-encoder'
            indexed = 0

            def encode(self, values, prefix):
                if prefix == 'passage: ':
                    self.indexed += len(values)
                return np.asarray([[1, 0] if 'возмещение' in text or prefix == 'query: ' else [0, 1]
                                   for text in values], dtype=np.float32)

        letters = [Letter('wrong', 'b', '', '', '', 'Сегодня проведено списание.'),
                   Letter('right', 'b', '', '', '', 'Первая часть письма.\n\nПроизведено возмещение. Сумма 600 ₽.')]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            encoder = StubEncoder()
            engine = SearchEngine(letters, encoder, cache)
            hit = engine.search('когда мне вернут деньги', mode='semantic')[0]
            self.assertEqual(engine.letters[hit.index].doc_id, 'right')
            self.assertIn('600 ₽', hit.evidence)
            self.assertEqual(encoder.indexed, 3)
            reused = StubEncoder()
            self.assertEqual(SearchEngine(letters, reused, cache).search('деньги', mode='semantic')[0].index, 1)
            self.assertEqual(reused.indexed, 0)
            updated = StubEncoder()
            SearchEngine([letters[0], Letter('changed', 'b', '', '', '', 'Другой текст.')], updated, cache)
            self.assertEqual(updated.indexed, 2)


class EdgeCaseTests(unittest.TestCase):
    def test_receipt_date_and_event_date_have_different_meanings(self):
        self.assertEqual(received_period('письма, полученные 29 февраля 2024 года', '2024-03-01')[:2],
                         ('2024-02-29', '2024-02-29'))
        self.assertIsNone(received_period('вылет 29 февраля 2024 года', '2024-03-01'))
        self.assertIsNone(received_period('полученные 31 февраля 2024 года', '2024-03-01'))
        docs = [Letter('late','b','Чек','','2024-03-01','Квитанция за 29 февраля.'),
                Letter('unknown','b','Чек','','','Дата неизвестна.'),
                Letter('right','b','Оплата','','2024-02-29','Фискальная квитанция.')]
        engine = SearchEngine(docs)
        for query in ('Чеки, полученные вчера', 'Чеки, полученные 29.02.2024'):
            self.assertEqual([h.index for h in engine.search(query, mailbox='b', as_of='2024-03-01')], [2])
        self.assertFalse(engine.search('Чеки, полученные вчера', after='2024-03-01', as_of='2024-03-01'))
        self.assertEqual(engine.search('полученные с 28.02.2024 по 29.02.2024', as_of='2024-03-01')[0].index, 2)

    def test_smart_statistics_exclude_other_mailboxes_and_future_mail(self):
        visible = [Letter('a','b','','','2024-01-01','alpha'), Letter('b','b','','','2024-01-01','beta')]
        hidden = [Letter('future','b','','','2025-01-01','alpha'), Letter('other','x','','','2024-01-01','alpha')]
        def ranked(docs):
            e = SearchEngine(docs)
            return [(e.letters[h.index].doc_id,h.score) for h in e.search('alpha beta',mailbox='b',as_of='2024-02-01')]
        self.assertEqual(ranked(visible), ranked(visible + hidden))

    def test_empty_optional_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'documents.jsonl'
            path.write_text(json.dumps({'doc_id': 'empty', 'doc': None, 'subject': None,
                                        'sender': None, 'doc_dt': None}), encoding='utf-8')
            letter, = load_letters(path)
        self.assertEqual((letter.subject, letter.body, letter.sender, letter.day), ('', '', '', ''))
        engine = SearchEngine([letter])
        self.assertEqual(engine.search('  '), [])
        self.assertEqual(engine.search('несуществующее слово'), [])

    def test_typo_and_mixed_scripts(self):
        engine = SearchEngine(load_letters(DEMO_CORPUS))
        for query in ('компенсцаия', 'кoмпенсация', 'компенсация PK-61820'):
            with self.subTest(query=query):
                hits = engine.search(query, as_of='2026-11-21')
                self.assertEqual(engine.letters[hits[0].index].doc_id, 'shop-39')

    def test_evidence_at_end_of_very_long_message(self):
        body = ('Обычный текст о доставке и расписании. ' * 2500) + 'Итоговый номер ZX-987654 подтверждён.'
        engine = SearchEngine([Letter('long', 'b', '', '', '', body)])
        hit, = engine.search('ZX-987654')
        self.assertIn('ZX-987654', hit.evidence)
        self.assertLessEqual(len(hit.evidence), 332)


class HttpTests(unittest.TestCase):
    def test_user_flow_and_server_timing(self):
        engine = SearchEngine(load_letters(DEMO_CORPUS))
        with MailServer(('127.0.0.1', 0), engine, True) as server:
            def request(path):
                connection = MemoryConnection(path)
                MailHandler(connection, ('127.0.0.1', 1234), server)
                head, body = connection.output.getvalue().split(b'\r\n\r\n', 1)
                return head.decode(), body

            head, body = request('/')
            self.assertIn('Контекст', body.decode())
            self.assertIn('Content-Security-Policy: default-src', head)
            for file in ('/styles.css', '/app.js'):
                self.assertGreater(len(request(file)[1]), 500)
            self.assertTrue(json.loads(request('/api/meta')[1])['demo'])
            params = urlencode({'q': 'Самолёт улетит позже', 'synthetic_user': 'Поездки', 'query_date': '2026-11-21'})
            head, body = request('/api/search?' + params)
            first = json.loads(body)['items'][0]
            self.assertEqual(first['doc_id'], 'f-401')
            self.assertRegex(head, r'Server-Timing: backend;dur=\d+\.\d+')
            self.assertIn(first['evidence'], json.loads(request('/api/letter?id=' + first['doc_id'])[1])['body'])
            self.assertIn('400 Bad Request', request('/api/search?q=x&as_of=2026-02-30')[0])
            self.assertIn('400 Bad Request', request('/api/search?q=%20')[0])


class MemoryConnection:
    """Pass HTTP request and response bytes through the live handler without sockets."""

    def __init__(self, path):
        self.input = io.BytesIO(f'GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'.encode())
        self.output = io.BytesIO()

    def makefile(self, mode, size):
        return self.input

    def sendall(self, data):
        self.output.write(data)


if __name__ == '__main__':
    unittest.main()
