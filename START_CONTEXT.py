"""Почта · Контекст. Python 3.11+. Run directly to open the inbox."""


# data.py


import csv
import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent
DEMO_CORPUS = ROOT / 'fixtures' / 'letters.jsonl'
DEMO_QUERIES = ROOT / 'fixtures' / 'queries.jsonl'
DEMO_QRELS = ROOT / 'fixtures' / 'qrels.jsonl'


class PlainText(HTMLParser):
    blocks = {'p', 'br', 'div', 'li', 'tr', 'td', 'blockquote', 'hr', 'h1', 'h2', 'h3', 'section'}
    hidden_tags = {'script', 'style', 'head', 'svg'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden.append(tag)
        elif not self.hidden and tag in self.blocks:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        elif not self.hidden and tag in self.blocks:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_body(value, html=False):
    if html or re.search(r'<(?:html|body|p|div|br|table)\b', value, re.I):
        parser = PlainText()
        parser.feed(value)
        parser.close()
        value = ''.join(parser.parts)
    value = value.replace('\r\n', '\n').replace('\r', '\n').replace('\u00a0', ' ')
    value = re.sub(r'[^\S\n]+', ' ', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def field(row, *names):
    for name in names:
        value = row.get(name)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
        if isinstance(value, dict):
            address = str(value.get('email') or value.get('address') or '').strip()
            name = str(value.get('name') or '').strip()
            if address or name:
                return f'{name} <{address}>' if address and name else name or address
    return ''


def iso_day(value):
    for parser in (datetime.fromisoformat, lambda s: datetime.strptime(s, '%d.%m.%Y')):
        try:
            return parser(value).date().isoformat()
        except ValueError:
            pass
    return ''


@dataclass(frozen=True, slots=True)
class Letter:
    doc_id: str
    mailbox_id: str
    subject: str
    sender: str
    date: str
    body: str

    @property
    def day(self):
        return iso_day(self.date)

    def serializable(self):
        return asdict(self)


def records(path, wrappers=()):
    """Read object records with a location on errors. IDs from CSV stay strings."""
    with path.open(encoding='utf-8-sig', newline='') as handle:
        if path.suffix.lower() in {'.csv', '.tsv'}:
            rows = csv.DictReader(handle, delimiter='\t' if path.suffix.lower() == '.tsv' else ',')
            for line, row in enumerate(rows, 2):
                if None in row:
                    raise ValueError(f'{path}:{line}: more columns than in the header')
                yield line, row
            return
        if path.suffix.lower() in {'.jsonl', '.ndjson'}:
            for line, value in enumerate(handle, 1):
                if value.strip():
                    try:
                        yield line, json.loads(value)
                    except json.JSONDecodeError as err:
                        raise ValueError(f'{path}:{line}: {err.msg}') from err
            return
        value = json.load(handle)
        if isinstance(value, dict):
            value = next((value[key] for key in wrappers if key in value), value)
        if not isinstance(value, list):
            raise ValueError(f'{path}: expected an array of records')
        yield from enumerate(value, 1)


def load_letters(path):
    formats = {'.jsonl', '.ndjson', '.json', '.csv', '.tsv'}
    paths = sorted(p for p in path.rglob('*') if p.is_file() and p.suffix.lower() in formats) if path.is_dir() else [path]
    letters = []
    locations = {}
    for file in paths:
        if file.suffix.lower() not in formats:
            raise ValueError(f'{file}: expected JSONL, JSON, CSV or TSV')
        for row_num, row in records(file, ('messages', 'letters', 'documents', 'emails')):
            at = f'{file}:{row_num}'
            if not isinstance(row, dict):
                raise ValueError(f'{at}: expected an object')
            doc_id = field(row, 'doc_id', 'message_id', 'email_id', 'id')
            subject_keys = ('subject', 'title', 'topic')
            body_keys = ('doc', 'body', 'body_text', 'body_plain', 'text', 'plain_text', 'content')
            html_keys = ('body_html', 'html')
            if not doc_id or not any(key in row for key in (*subject_keys, *body_keys, *html_keys)):
                raise ValueError(f'{at}: expected doc_id and a subject or body column')
            if doc_id in locations:
                raise ValueError(f'{at}: duplicate doc_id {doc_id!r} (first at {locations[doc_id]})')
            locations[doc_id] = at
            body = field(row, *body_keys)
            html = field(row, *html_keys)
            body = clean_body(body or html, html=bool(html and not body))
            subject = field(row, *subject_keys)
            sender = field(row, 'sender', 'from', 'from_address', 'from_email', 'sender_email')
            if 'doc' in row:
                header, _, _ = body.partition('\n\n')
                subject_match = re.search(r'^(?:тема|subject)\s*:\s*(.+)$', header[:1000], re.M | re.I)
                sender_match = re.search(r'^(?:от|отправитель|from)\s*:\s*(.+)$', header[:1000], re.M | re.I)
                subject = subject or (subject_match[1].strip() if subject_match else '')
                sender = sender or (sender_match[1].strip() if sender_match else '')
            letters.append(Letter(
                doc_id=doc_id,
                mailbox_id=field(row, 'synthetic_user', 'mailbox_id', 'mailbox', 'account_id', 'user_id', 'box_id') or 'Входящие',
                subject=subject,
                sender=sender,
                date=field(row, 'doc_dt', 'date', 'sent_at', 'received_at', 'timestamp', 'datetime'),
                body=body,
            ))
    if not letters:
        raise ValueError(f'{path}: no letters found')
    return letters


def check_day(value):
    if value and date.fromisoformat(value).isoformat() != value:
        raise ValueError('Date must have the format YYYY-MM-DD')
    return value


DATASET_FILES = ('documents.jsonl', 'queries_dev.jsonl', 'qrels_dev.jsonl', 'manifest.json', 'README.md')


def locate_dataset():
    for candidate in (ROOT / 'data', ROOT / 'mipt_mail_search_participants.zip', ROOT / 'data' / 'mipt_mail_search_participants.zip'):
        if candidate.is_file() or (candidate / 'documents.jsonl').is_file():
            return candidate
    return None


def load_dataset(path, cache):
    if path.is_dir():
        folder = path
    else:
        with path.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        folder = cache / 'corpora' / digest
        folder.mkdir(parents=True, exist_ok=True)
        with ZipFile(path) as archive:
            entries = {}
            for member in archive.infolist():
                name = Path(member.filename).name
                if name in DATASET_FILES and not member.is_dir():
                    if name in entries:
                        raise ValueError(f'{path}: duplicate archive file {name}')
                    if member.file_size > 100 * 1024**2:
                        raise ValueError(f'{path}: unexpectedly large file {name}')
                    entries[name] = member
            for name, member in entries.items():
                dest = folder / name
                if dest.is_file() and dest.stat().st_size == member.file_size:
                    continue
                temp = None
                try:
                    with archive.open(member) as source, tempfile.NamedTemporaryFile(dir=folder, delete=False) as output:
                        temp = Path(output.name)
                        shutil.copyfileobj(source, output)
                    temp.replace(dest)
                finally:
                    if temp is not None:
                        temp.unlink(missing_ok=True)
    if not (folder / 'documents.jsonl').is_file():
        raise ValueError(f'{path}: documents.jsonl is missing')
    manifest_path = folder / 'manifest.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        for name, expected in manifest.get('sha256', {}).items():
            if name not in DATASET_FILES or not (folder / name).is_file():
                raise ValueError(f'{path}: manifest entry {name!r} is missing or invalid')
            with (folder / name).open('rb') as handle:
                actual = hashlib.file_digest(handle, 'sha256').hexdigest()
            if actual != expected:
                raise ValueError(f'{path}: SHA-256 mismatch for {name}')
    return folder


# temporal.py


import re
from datetime import date, timedelta

MONTHS = 'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
DAY = r'(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}\s+(?:' + '|'.join(MONTHS) + r')\s+\d{4}(?:\s+года)?)'
# Only bind a date to delivery when the wording describes the message itself.
RECEIVED = re.compile(
    r'\b(?:получен(?:ы|ные|ное|ный|ных|а|о)?|пришедш\w*|прислан\w*|пришл[оиа]|'
    r'прислали|получил[аи]?|приходили)\s+(?:мне\s+)?(?:за\s+)?(?:период\s+)?(?:с\s+|в\s+)?'
    r'(?P<start>' + DAY + r'|позавчера|вчера|сегодня)(?:\s*(?:по|до|[–—])\s*(?P<end>' + DAY + r'))?\b',
    re.I)


def received_period(query, as_of):
    match = RECEIVED.search(query)
    if not match:
        return None

    def parse(value):
        value = value.lower()
        if value in ('сегодня', 'вчера', 'позавчера'):
            return relative_range(value, as_of)[0]
        if '.' in value:
            day, month, year = map(int, value.split('.'))
        elif '-' in value:
            return date.fromisoformat(value).isoformat()
        else:
            day, month_name, year, *_ = value.split()
            day, month, year = int(day), MONTHS.index(month_name) + 1, int(year)
        return date(year, month, day).isoformat()

    try:
        start = parse(match['start'])
        end = parse(match['end']) if match['end'] else start
    except ValueError:
        return None
    if not start or end < start:
        return None
    return start, end, (query[:match.start()] + ' ' + query[match.end():]).strip(' ,.')


def relative_range(query, as_of):
    """Infer likely received dates. The caller uses them as soft evidence."""
    if not as_of:
        return '', ''
    current = date.fromisoformat(as_of)
    query = query.lower().replace('ё', 'е')
    def span(first, last):
        return first.isoformat(), last.isoformat()

    if re.search(r'\b(?:позавчера|позавчерашн\w*)\b', query):
        target = current - timedelta(days=2)
        return span(target, target)
    if re.search(r'\b(?:вчера|вчерашн\w*)\b', query):
        target = current - timedelta(days=1)
        return span(target, target)
    if re.search(r'\b(?:сегодня|сегодняшн\w*)\b', query):
        return span(current, current)
    recent = re.search(r'\b(?:за\s+)?последн\w*\s+(\d{1,3})\s+(день|дня|дней|недел\w*)\b', query)
    if recent and int(recent[1]):
        days = int(recent[1]) * (7 if recent[2].startswith('недел') else 1)
        return span(current - timedelta(days=days - 1), current)
    if re.search(r'\bна\s+(?:прошлой|этой)\s+неделе\b', query):
        monday = current - timedelta(days=current.weekday())
        return span(monday - timedelta(days=7), monday - timedelta(days=1)) if 'прошлой' in query else span(monday, current)
    if re.search(r'\bв\s+(?:прошлом|этом)\s+месяце\b', query):
        first = current.replace(day=1)
        if 'прошлом' in query:
            last = first - timedelta(days=1)
            return span(last.replace(day=1), last)
        return span(first, current)
    return '', ''


# semantic.py


import argparse
import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path


REVISION = '761b726'
MODEL_URL = f'https://huggingface.co/Xenova/multilingual-e5-small/resolve/{REVISION}'
FILES = {
    'tokenizer.json': 'tokenizer.json',
    'model_quantized.onnx': 'onnx/model_quantized.onnx',
}
MODEL_HASH = 'f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193'


def digest_file(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def download(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for name, remote in FILES.items():
        target = folder / name
        if target.is_file() and (name != 'model_quantized.onnx' or digest_file(target) == MODEL_HASH):
            continue
        print(f'Downloading {name}', flush=True)
        temp_path = None
        try:
            with urllib.request.urlopen(f'{MODEL_URL}/{remote}', timeout=45) as response:
                with tempfile.NamedTemporaryFile(dir=folder, suffix='.part', delete=False) as output:
                    temp_path = Path(output.name)
                    shutil.copyfileobj(response, output)
            if name == 'model_quantized.onnx' and digest_file(temp_path) != MODEL_HASH:
                raise ValueError('Unexpected SHA-256 for the ONNX model')
            temp_path.replace(target)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


class E5Encoder:
    def __init__(self, folder):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ValueError('Install requirements-semantic.txt to enable E5') from exc

        paths = [folder / name for name in FILES]
        if not all(path.is_file() for path in paths):
            raise ValueError(f'Model not found in {folder}. Run python -m mailsearch.semantic download')
        self.np = np
        self.tokenizer = Tokenizer.from_file(str(paths[0]))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id('<pad>'), pad_token='<pad>')
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = min(4, os.cpu_count() or 1)
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(paths[1]), sess_options=opts, providers=['CPUExecutionProvider'])
        self.inputs = {entry.name: np.int32 if entry.type == 'tensor(int32)' else np.int64 for entry in self.session.get_inputs()}
        self.cache_key = ('E5 quantized masked mean 256 v1', *(digest_file(path) for path in paths))

    def encode(self, texts, prefix):
        pieces = self.tokenizer.encode_batch([prefix + text for text in texts])
        arrays = {'input_ids': [value.ids for value in pieces],
                  'attention_mask': [value.attention_mask for value in pieces],
                  'token_type_ids': [value.type_ids for value in pieces]}
        inputs = {name: self.np.array(arrays[name], dtype=dtype) for name, dtype in self.inputs.items()}
        output = self.session.run(None, inputs)[0]
        mask = self.np.asarray(arrays['attention_mask'], dtype=self.np.float32)[..., None]
        vectors = (output * mask).sum(axis=1) / mask.sum(axis=1).clip(1)
        norms = self.np.linalg.norm(vectors, axis=1, keepdims=True).clip(1e-8)
        return (vectors / norms).astype(self.np.float32, copy=False)


class DenseIndex:
    def __init__(self, letters, passages, encoder, cache):
        import numpy as np

        self.np = np
        self.encoder = encoder
        self.starts = np.cumsum([0] + [len(values) for values in passages[:-1]])
        passages_by_letter = [[f'{letter.subject.strip()}\n{value}'.strip() or '(пустое письмо)' for value in values]
                              for letter, values in zip(letters, passages)]
        texts = [value for values in passages_by_letter for value in values]
        fingerprint = hashlib.sha256(json.dumps([encoder.cache_key, texts], ensure_ascii=False).encode()).hexdigest()
        path = cache / f'vectors-{fingerprint}.npy' if cache is not None else None
        self.vectors = None
        if path is not None and path.is_file():
            try:
                vectors = np.load(path, allow_pickle=False)
                if vectors.ndim == 2 and len(vectors) == len(texts) and vectors.dtype == np.float32:
                    self.vectors = vectors
            except (OSError, ValueError, EOFError):
                pass
        if self.vectors is None:
            print(f'Encoding {len(texts):,} passages once; the index is reused on the next launch.', flush=True)
            batches = []
            for start in range(0, len(texts), 32):
                batches.append(encoder.encode(texts[start:start + 32], 'passage: '))
                if start and start % 1024 == 0:
                    print(f'  {start:,} / {len(texts):,}', flush=True)
            self.vectors = np.ascontiguousarray(np.concatenate(batches), dtype=np.float32)
            if path is not None:
                self._write_cache(path)

    def _write_cache(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.part', delete=False) as handle:
                temp_path = Path(handle.name)
                self.np.save(handle, self.vectors, allow_pickle=False)
            temp_path.replace(path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def rank(self, query, scope, limit):
        np = self.np
        vector = self.encoder.encode([query], 'query: ')[0]
        similarities = self.vectors @ vector
        best = np.maximum.reduceat(similarities, self.starts)
        scope_ids = np.arange(len(best)) if scope is None else np.fromiter(sorted(scope), dtype=int)
        order = scope_ids[np.argsort(-best[scope_ids], kind='stable')[:limit]].tolist()
        evidence = {}
        for i in order:
            start = self.starts[i]
            end = self.starts[i + 1] if i + 1 < len(self.starts) else len(similarities)
            evidence[i] = int(np.argmax(similarities[start:end]))
        return order, evidence


def add_model_args(parser):
    parser.add_argument('--model-dir', type=Path, metavar='PATH', help='Enable local E5 from this directory')
    parser.add_argument('--cache-dir', type=Path, default=Path('.cache'))


# engine.py


import heapq
import math
import re
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from threading import RLock


KEYBOARD_TO_RUSSIAN = str.maketrans("qwertyuiop[]asdfghjkl;'zxcvbnm,.", 'йцукенгшщзхъфывапролджэячсмитьбю')
LOOKALIKE_TO_RUSSIAN = str.maketrans('acekopxy', 'асекорху')
RUSSIAN_ALPHABET = 'абвгдежзийклмнопрстуфхцчшщъыьэюя'
ROMAN = str.maketrans(dict(zip(RUSSIAN_ALPHABET, (
    'a','b','v','g','d','e','zh','z','i','y','k','l','m','n','o','p','r',
    's','t','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya'
), strict=True)))


def roman_key(word):
    return word.replace('kh', 'h').replace('yi', 'yy')


def russian_correction(word, vocabulary):
    if not any('а' <= char <= 'я' for char in word) or not word.isalpha() or len(word) > 32:
        return None
    mapped = word.translate(LOOKALIKE_TO_RUSSIAN)
    if any(char not in RUSSIAN_ALPHABET for char in mapped):
        return None
    if mapped != word:
        candidate = stem(mapped)
        return candidate if candidate in vocabulary else None
    if len(word) < 5:
        return None
    candidates = set()
    for i, char in enumerate(word):
        left, right = word[:i], word[i + 1:]
        candidates.add(left + right)
        if right:
            candidates.add(left + right[0] + char + right[1:])
        for letter in RUSSIAN_ALPHABET:
            candidates.add(left + letter + right)
            candidates.add(left + letter + char + right)
    candidates.update(word + letter for letter in RUSSIAN_ALPHABET)
    matches = {key for candidate in candidates if (key := stem(candidate)) in vocabulary}
    return next(iter(matches)) if len(matches) == 1 else None


WORD = re.compile(r'[^\W_]+', re.UNICODE)
ANCHOR = re.compile(
    r'(?<![\w@])(?:[\w.+%-]+@[\w.-]+\.[a-zа-я]{2,}|[a-zа-я]{1,12}[-_]?\d{2,}[a-zа-я\d-]*|'
    r'\d{2,}[-_]?[a-zа-я]{1,12}[a-zа-я\d-]*|\d{5,})(?!\w)', re.I,
)
EXPLICIT_NUMBER = re.compile(r'(?:№\s*|\b(?:номер|заказ)\s*#?\s*)(\d{3,})\b', re.I)
SUFFIX = re.compile(r'(?:иями|ями|ами|ого|ему|ому|ыми|ими|оев|ой|ей|ия|ию|ии|ие|ые|ых|ую|ое|ая|яя|ам|ям|ах|ях|ов|ев|ом|ем|ть|а|я|ы|и|у|ю|е|о)$')
STOP = set('и в во на по за с со из о об к у до для от а но не что это как где когда ли или про там был было были есть письме писали письмо мне меня меняю мой моё мои моего который которая найти покажи все вот по the an of to for is'.split())

# Cross-reference common language in transactional mail. An event also matches
# across inflections via the stems. This is a transparent offline retrieval mode.
EVENTS = {
    'air_departure': 'вылет отправление авиа авиакомпания самолёт самолет борт рейс улетит авиарейс аэропорт',
    'schedule_change': 'перенос перенесено перенесли смещение изменилось изменение задержка отложен позже опаздывает сдвиг передвинули',
    'refund': 'возврат возмещение компенсируют компенсация вернут вернули возвращены reimbursement refund',
    'payment': 'оплата платёж платеж списание списали оплачено транзакция перечисление списано',
    'delivery': 'доставка курьер посылка отправление привезут пункт выдача доставят привезли получение',
    'pickup': 'постамат пвз самовывоз выдача забирать забрать ячейка',
    'access': 'аккаунт доступ профиль безопасность',
    'login': 'войти вход вошли авторизация авторизоваться логин сеанс',
    'credential': 'пароль пароля логин учётка',
    'reset': 'сброс восстановление восстановить сбросить забытый восстановлен',
    'meeting': 'встреча звонок созвон совещание конференция консультация интервью собеседование',
    'receipt': 'чек квитанция счёт счет инвойс invoice receipt кассовый фискальный подтверждение',
    'subscription': 'подписка абонемент тариф автопродление продление подписку списывают ежемесячный',
    'ticket': 'билет билеты бронирование бронь заказ поездка путешествие',
    'application': 'заявка заявление обращение запрос регистрация форма рассмотрение',
    'approval': 'одобрение одобрили принята прошёл подтверждена подтвердили согласовали принято',
    'survey': 'опрос анкетирование анкета отзыв обратная связь оценка мнение',
    'attachment': 'файл документ акт pdf',
    'attached': 'вложение приложен приложили прикрепили прикреплён вложен прилагается',
    'deadline': 'срок истекает дедлайн крайний осталось последний окончание',
    'cancellation': 'отмена отменили аннулирование аннулирован отказ отменён',
    'security_code': 'код otp кодом верификация одноразовый подтверждение 2fa',
    'money': 'деньги средства сумма кошелёк карта карточка рублей рубли',
}
CONTEXT_EVENTS = {'access', 'attachment', 'credential', 'money', 'ticket'}
HYPHENS = str.maketrans({char: '-' for char in '‐‑‒–—―−'})


def normalized(value):
    return unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')


@lru_cache(maxsize=65536)
def stem(word):
    if len(word) >= 5 and 'а' <= word[0] <= 'я' and word.isalpha():
        shortened = SUFFIX.sub('', word)
        if len(shortened) >= 3:
            return shortened
    return word


def words(text):
    return WORD.findall(normalized(text))


def terms(text):
    return [stem(word) for word in words(text) if word not in STOP and len(word) > 1]


def anchor_key(value):
    value = normalized(value)
    return value if '@' in value else re.sub(r'[\s_-]+', '', value)


def anchors(text):
    # Date, time, and ordinary quantities are not automatic order numbers.
    value = unicodedata.normalize('NFKC', text).translate(HYPHENS)
    numbered = {anchor_key(match[1]) for match in EXPLICIT_NUMBER.finditer(text)}
    return numbered | {anchor_key(match[0]) for match in ANCHOR.finditer(value)}


INTENTS = defaultdict(set)
for event, variants in EVENTS.items():
    for key in terms(variants):
        INTENTS[key].add(event)
    # Short masculine nouns do not pass the normal five-character stem gate
    # (e.g. "чек" and "чеки"). Index their regular short case forms as aliases.
    for word in words(variants):
        if len(word) == 3 and all(c in RUSSIAN_ALPHABET for c in word) and word[-1] in 'бвгджзклмнпрстфхцчшщ':
            for ending in ('а', 'у', 'е', 'и', 'ы', 'ов', 'ам', 'ами', 'ах'):
                INTENTS[stem(word + ending)].add(event)


def events(keys):
    return set().union(*(INTENTS.get(key, set()) for key in keys))


def split_passages(text, size=64, overlap=16):
    if not text:
        return ['']
    passages = []
    for block in re.split(r'\n{2,}', text):
        block = ' '.join(block.split())
        if not block:
            continue
        parts = list(re.finditer(r'\S+', block))
        for start in range(0, len(parts), size - overlap):
            end = min(start + size, len(parts))
            passages.append(block[parts[start].start():parts[end - 1].end()])
            if end == len(parts):
                break
    return passages or ['']


def clipped_evidence(passage, query_terms, query_anchors):
    limit = 330
    if len(passage) <= limit:
        return passage
    candidates = list(re.finditer(r'(?<=[.!?])\s+|[,;]\s+', passage))
    starts = [0, *(match.end() for match in candidates)]
    for match in re.finditer(r'\S+', passage):
        token = match[0].strip('.,!?:;()[]«»')
        if anchors(token) & query_anchors or set(terms(token)) & query_terms:
            starts.append(max(0, passage.rfind(' ', 0, max(0, match.start() - 100)) + 1))
    def excerpt(start):
        end = start + limit
        if end >= len(passage):
            return passage[start:]
        boundary = passage.rfind(' ', start, end + 1)
        return passage[start:boundary if boundary > start else end]

    excerpts = (excerpt(start) for start in set(starts) if start < len(passage) - 55 or start == 0)
    ranked = max(excerpts, key=lambda value: (len(anchors(value) & query_anchors), len(set(terms(value)) & query_terms), len(value), -passage.find(value)), default=passage[:limit])
    return ('…' if not passage.startswith(ranked) else '') + ranked + ('…' if not passage.endswith(ranked) else '')


@dataclass(frozen=True, slots=True)
class Hit:
    index: int
    score: float
    evidence: str
    signals: tuple[str, ...]


def analyze_letter(letter):
    texts = (letter.subject, letter.sender, letter.body)
    aliases = {}
    fields = []
    for text in texts:
        counts = Counter()
        for word in words(text):
            if word in STOP or len(word) < 2:
                continue
            key = stem(word)
            counts[key] += 1
            if len(word) >= 3 and all(char in RUSSIAN_ALPHABET for char in word):
                alias = roman_key(word.translate(ROMAN))
                if aliases.setdefault(alias, key) != key:
                    aliases[alias] = None
        fields.append(counts)
    return (fields, tuple(map(lambda field: sum(field.values()), fields)), aliases,
            anchors('\n'.join(texts)), events(set().union(*fields)), split_passages(letter.body))


class SearchEngine:
    def __init__(self, letters, encoder=None, cache=None, *, analysis=None):
        self.letters = tuple(letters)
        self.by_id = {letter.doc_id: i for i, letter in enumerate(letters)}
        self.dates = tuple(letter.day for letter in letters)
        self.boxes = defaultdict(list)
        for i, letter in enumerate(letters):
            self.boxes[letter.mailbox_id].append(i)
        self.inbox = tuple(sorted(range(len(letters)), key=lambda i: (self.dates[i], letters[i].date, -i), reverse=True))
        self.analysis = analysis if analysis is not None else tuple(map(analyze_letter, letters))
        self.passages = []
        self.search_terms = defaultdict(list)
        self.doc_anchors = []
        self.anchor_index = defaultdict(set)
        self.doc_events = []
        self.event_index = defaultdict(set)

        self.roman_terms = {}
        total_lengths = [0, 0, 0]
        for i, (_, lengths, aliases, keys, doc_events, passages) in enumerate(self.analysis):
            for alias, key in aliases.items():
                if self.roman_terms.setdefault(alias, key) != key:
                    self.roman_terms[alias] = None
            total_lengths = [total + length for total, length in zip(total_lengths, lengths)]
            self.doc_anchors.append(keys)
            for anchor in keys:
                self.anchor_index[anchor].add(i)
            self.doc_events.append(doc_events)
            for event in doc_events:
                self.event_index[event].add(i)
            self.passages.append(passages)

        averages = [max(1, length / max(1, len(letters))) for length in total_lengths]
        weights = (2.6, 1.35, 1.0)
        for i, (fields, lengths, *_) in enumerate(self.analysis):
            effective = defaultdict(float)
            for field, length, average, weight in zip(fields, lengths, averages, weights):
                scale = weight / (0.35 + 0.65 * length / average)
                for term, count in field.items():
                    effective[term] += scale * count
            for term, tf in effective.items():
                self.search_terms[term].append((i, 2.2 * tf / (1.2 + tf)))
        self.idf = {term: math.log1p((len(letters) - len(postings) + 0.5) / (len(postings) + 0.5)) for term, postings in self.search_terms.items()}
        self.snapshots = OrderedDict()
        self.snapshot_lock = RLock()
        self.dense = None
        if encoder is not None:

            self.dense = DenseIndex(letters, self.passages, encoder, cache)

    def eligible(self, mailbox='', after='', before='', as_of=''):
        if not mailbox and not after and not before and not as_of:
            return None
        scope = self.boxes.get(mailbox, []) if mailbox else range(len(self.letters))
        return {i for i in scope if (not after or self.dates[i] >= after)
                and (not before or self.dates[i] and self.dates[i] <= before)
                and (not as_of or not self.dates[i] or self.dates[i] <= as_of)}

    def browse(self, mailbox='', after='', before='', offset=0, limit=25, as_of=''):
        scope = self.eligible(mailbox, after, before, as_of)
        order = self.inbox if scope is None else [i for i in self.inbox if i in scope]
        return [self.letters[i] for i in order[offset:offset + limit]], len(order)

    def _visible_index(self, key, scope):
        # Statistics, spelling vocabulary and topic frequencies must only see
        # messages available in the selected mailbox and time window.
        with self.snapshot_lock:
            if key in self.snapshots:
                self.snapshots.move_to_end(key)
                return self.snapshots[key]
            indices = sorted(scope)
            view = SearchEngine([self.letters[i] for i in indices],
                                analysis=tuple(self.analysis[i] for i in indices))
            self.snapshots[key] = (indices, view)
            while len(self.snapshots) > 1 and (len(self.snapshots) > 16 or
                    sum(len(v[0]) for v in self.snapshots.values()) > 8000):
                self.snapshots.popitem(last=False)
            return indices, view

    def search(self, query, mode='smart', limit=25, mailbox='', after='', before='', as_of=''):
        if not query.strip() or limit <= 0:
            return []
        period = received_period(query, as_of) if mode == 'smart' else None
        if period:
            start, end, query = period
            after = max(after, start)
            before = min(before, end) if before else end
        scope = self.eligible(mailbox, after, before, as_of)
        if scope is not None and not scope:
            return []
        engine, indices = self, None
        if mode == 'smart' and self.dense is None and scope is not None and len(scope) < len(self.letters):
            indices, engine = self._visible_index((mailbox, after, before, as_of), scope)
            scope = None
        hits = engine._rank(query, mode, limit, scope, as_of, period)
        if indices is None:
            return hits
        return [Hit(indices[h.index], h.score, h.evidence, h.signals) for h in hits]

    def _rank(self, query, mode, limit, scope, as_of, period):
        if mode == 'smart' and query.isascii() and not any(char.isdigit() for char in query) and '@' not in query:
            original = set(terms(query))
            if original.isdisjoint(self.search_terms):
                corrected = query.lower().translate(KEYBOARD_TO_RUSSIAN)
                corrected_terms = set(terms(corrected))
                if corrected_terms and corrected_terms.issubset(self.search_terms):
                    query = corrected
        query_terms = set(terms(query))
        if mode == 'smart':
            for word in set(words(query)) - STOP:
                original = stem(word)
                if original not in self.search_terms:
                    if word.isascii() and word.isalpha():
                        corrected = self.roman_terms.get(roman_key(word))
                    else:
                        corrected = russian_correction(word, self.search_terms)
                    if corrected:
                        query_terms.discard(original)
                        query_terms.add(corrected)
        query_anchors = anchors(query)
        if period and not (query_terms - {'письм', 'письма', 'письмам', 'уведомлен'}):
            return [Hit(i, 1.0, self.letters[i].body[:330], ('Совпала дата',))
                    for i in self.inbox if scope is None or i in scope][:limit]
        lexical = defaultdict(float)
        for term in sorted(query_terms):
            idf = self.idf.get(term, 0)
            for i, saturation in self.search_terms.get(term, ()):
                if scope is None or i in scope:
                    lexical[i] += idf * saturation

        depth = max(limit, 100)
        lex_order = heapq.nlargest(depth, lexical, key=lambda i: (lexical[i], -i)) if mode != 'semantic' else []
        sem_order = []
        sem_passages = {}
        query_events = events(query_terms)
        if mode != 'lexical':
            if self.dense is not None:
                sem_order, sem_passages = self.dense.rank(query, scope, depth)
            elif query_events:
                candidates = set().union(*(self.event_index[event] for event in query_events))
                if scope is not None:
                    candidates &= scope
                def intent_score(i):
                    overlap = self.doc_events[i] & query_events
                    return (sum(1 / math.sqrt(len(self.event_index[event])) for event in sorted(overlap))
                            * (0.8 + 0.2 * len(overlap) / len(query_events)))
                sem_order = heapq.nlargest(depth, candidates, key=lambda i: (intent_score(i), lexical.get(i, 0), -i))

        scores = defaultdict(float)
        signals = defaultdict(set)
        semantic_weight = 1.15 if not query_anchors else 0.70
        for order, weight, label in ((lex_order, 1.0, 'По словам'), (sem_order, semantic_weight, 'По смыслу' if self.dense else 'По теме')):
            for rank, i in enumerate(order, 1):
                scores[i] += weight / (24 + rank)
                if rank <= max(12, min(limit, 30)):
                    signals[i].add(label)

        if query_anchors and mode == 'smart':
            exact = set().union(*(self.anchor_index[key] for key in query_anchors if key in self.anchor_index))
            for i in exact if scope is None else exact & scope:
                matching = query_anchors & self.doc_anchors[i]
                if matching:
                    scores[i] += 0.12 * len(matching)
                    signals[i].add('Совпал номер или адрес')

        actions = query_events - CONTEXT_EVENTS
        if mode == 'smart' and actions and self.dense is None:
            for i in sem_order:
                overlap = len(actions & self.doc_events[i])
                if overlap:
                    scores[i] += 0.054 * overlap / len(actions)

        if mode == 'smart' and self.dense is None:
            # Preserve BM25's score margins: a weak generic match should not
            # receive almost the same vote as a very specific subject match.
            peak = max(lexical.values(), default=0)
            if peak:
                for i in lex_order:
                    scores[i] += 0.09 * lexical[i] / peak

        since, until = relative_range(query, as_of) if mode == 'smart' else ('', '')
        if since:
            for i in scores:
                if since <= self.dates[i] <= until:
                    scores[i] *= 1.55
                    signals[i].add('Совпала дата')

        hits = []
        for i in heapq.nlargest(limit, scores, key=lambda i: (scores[i], -i)):
            if period:
                signals[i].add('Совпала дата')
            if 'Совпал номер или адрес' in signals[i] or i not in sem_passages:
                def passage_score(text):
                    keys = set(terms(text))
                    return (len(anchors(text) & query_anchors),
                            sum(self.idf.get(key, 0.0) for key in sorted(keys & query_terms)),
                            len(events(keys) & query_events))
                passage = max(self.passages[i], key=passage_score)
            else:
                passage = self.passages[i][sem_passages[i]]
            evidence = clipped_evidence(passage or self.letters[i].subject, query_terms, query_anchors)
            hits.append(Hit(i, scores[i], evidence, tuple(sorted(signals[i]))))
        return hits


# evaluate.py


import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from time import perf_counter_ns



def read_queries(path):
    seen = set()
    for line, row in records(path, ('queries',)):
        query_id = field(row, 'query_id', 'id')
        query = field(row, 'query', 'text', 'question')
        if not query_id or not query or query_id in seen:
            raise ValueError(f'{path}:{line}: expected a unique query_id and query')
        seen.add(query_id)
        yield (query_id, query, field(row, 'synthetic_user', 'mailbox_id', 'mailbox'), check_day(field(row, 'query_date', 'as_of')))


def read_qrels(path):
    result = defaultdict(dict)
    seen = set()
    for line, row in records(path, ('qrels',)):
        query_id = field(row, 'query_id')
        doc_id = field(row, 'doc_id')
        relevance = int(row.get('relevance', 1))
        if not query_id or not doc_id or relevance < 0 or relevance > 10 or (query_id, doc_id) in seen:
            raise ValueError(f'{path}:{line}: invalid or duplicate relevance judgment')
        seen.add((query_id, doc_id))
        judgments = result[query_id]
        if relevance:
            judgments[doc_id] = relevance
    return result


def metrics(ranked, gold, k):
    if len(set(ranked)) != len(ranked) or not gold or k < 1:
        raise ValueError('Expected unique ranked IDs, positive judgments and positive K')
    gains = [gold.get(doc_id, 0) for doc_id in ranked[:k]]
    def dcg(values):
        return sum((2**value - 1) / math.log2(rank + 2) for rank, value in enumerate(values))

    recall = sum(bool(gain) for gain in gains) / len(gold)
    ndcg = dcg(gains) / dcg(sorted(gold.values(), reverse=True)[:k])
    rr = next((1 / rank for rank, gain in enumerate(gains, 1) if gain), 0.0)
    return recall, ndcg, rr


def percentile_nearest_rank(values, quantile):
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)]


def evaluate(engine, queries, qrels, modes, cutoffs, repeat, export_k):
    if qrels is not None:
        if set(qrels) != {q[0] for q in queries}:
            raise ValueError('Queries and judgment groups differ; use unscored export for unlabeled queries')
        for qid, _, box, day in queries:
            for doc in qrels[qid]:
                i = engine.by_id.get(doc)
                if i is None or (box and engine.letters[i].mailbox_id != box) or (day and engine.dates[i] > day):
                    raise ValueError(f'{qid}: missing or ineligible relevant document {doc}')
    qrels = qrels or {}
    results = {}
    details = {}
    depth = max(*cutoffs, export_k)
    for mode in modes:
        times = []
        run = []
        for trial in range(repeat):
            for query_id, query, mailbox, as_of in queries:
                started = perf_counter_ns()
                hits = engine.search(query, mode=mode, limit=depth, mailbox=mailbox, as_of=as_of)
                doc_ids = [engine.letters[hit.index].doc_id for hit in hits]
                times.append((perf_counter_ns() - started) / 1e6)
                if trial == 0:
                    run.append({'query_id': query_id, 'doc_ids': doc_ids})
        details[mode] = run
        report = {'p50_ms': round(percentile_nearest_rank(times, .5), 3),
                  'p95_ms': round(percentile_nearest_rank(times, .95), 3),
                  'p99_ms': round(percentile_nearest_rank(times, .99), 3), 'timed_queries': len(times),
                  'scored_queries': sum(bool(x) for x in qrels.values()),
                  'judged_without_positives': sum(not x for x in qrels.values())}
        for k in cutoffs:
            values = [(metrics(row['doc_ids'], qrels[row['query_id']], k), len(qrels[row['query_id']]))
                      for row in run if qrels.get(row['query_id'])]
            if values:
                report[f'Recall@{k}'] = round(fmean(value[0] for value, _ in values), 5)
                report[f'nDCG@{k}'] = round(fmean(value[1] for value, _ in values), 5)
                report[f'MRR@{k}'] = round(fmean(value[2] for value, _ in values), 5)
                single = [value[2] for value, count in values if count == 1]
                if single:
                    report[f'MRR-single@{k}'] = round(fmean(single), 5)
        results[mode] = report
    return results, details


# server.py


import argparse
import json
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter_ns
from urllib.parse import parse_qs, urlsplit



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


DEMO_LETTERS = [
    {'doc_id': 'f-401', 'mailbox_id': 'Поездки', 'subject': 'Изменение времени вылета', 'sender': 'Север Авиа', 'date': '2026-11-20', 'body': 'Здравствуйте!\n\nОтправление рейса SN‑401 в Казань перенесено с 15:20 на 18:40. Посадка начнётся в 18:05, терминал B.\n\nЕсли новый график вам не подходит, свяжитесь со службой поддержки.'},
    {'doc_id': 'f-2085', 'mailbox_id': 'Поездки', 'subject': 'Бронирование FL-2085 подтверждено', 'sender': 'Билетный сервис', 'date': '2026-11-17', 'body': 'Маршрут: Москва — Казань, рейс SN‑401. Код брони FL-2085.\n\nМинимальное время на пересадку: 40 минут. Сохраните электронный билет до конца поездки.'},
    {'doc_id': 'f-refund', 'mailbox_id': 'Поездки', 'subject': 'Возмещение по бронированию FL‑2090', 'sender': 'Билетный сервис', 'date': '2026-11-19', 'body': 'Средства в размере 8 450 ₽ перечислены на исходную карту. Возмещение по бронированию FL‑2090 выполнено полностью.\n\nЗачисление зависит от банка и обычно занимает до пяти рабочих дней.'},
    {'doc_id': 'f-receipt', 'mailbox_id': 'Поездки', 'subject': 'Кассовый чек за бронирование FL‑2090', 'sender': 'Билетный сервис', 'date': '2026-11-18', 'body': 'Оплата бронирования прошла. Списано 8 450 ₽. Фискальный документ доступен в личном кабинете.'},
    {'doc_id': 'f-hotel', 'mailbox_id': 'Поездки', 'subject': 'Данные для заселения', 'sender': 'Отель «Берег»', 'date': '2026-11-21', 'body': 'С 14:00 вход через правые двери. Ключ от номера на стойке администратора. Выезд до 11:00.'},
    {'doc_id': 'f-earlier', 'mailbox_id': 'Поездки', 'subject': 'Рейс SN-401 · ноябрьский график', 'sender': 'Север Авиа', 'date': '2026-11-10', 'body': 'По первоначальному расписанию рейс вылетит в 15:20 из терминала B. Отправление в Москву — в 08:10.'},
    {'doc_id': 'f-delivery', 'mailbox_id': 'Поездки', 'subject': 'Задержка доставки билетов', 'sender': 'Билетный сервис', 'date': '2026-11-20', 'body': 'Бумажные билеты отправлены курьером. Из-за задержки он привезёт конверт завтра.'},
    {'doc_id': 'f-competition', 'mailbox_id': 'Поездки', 'subject': 'Подтверждение заявки', 'sender': 'Оргкомитет', 'date': '2026-11-19', 'body': 'Заявка на участие рассмотрена и принята. Список финалистов появится в пятницу.'},
    {'doc_id': 'f-cancel', 'mailbox_id': 'Поездки', 'subject': 'Аннулирование брони FL-2002', 'sender': 'Билетный сервис', 'date': '2026-11-11', 'body': 'Бронь аннулирована по вашему запросу. Заявление зарегистрировано под номером 53618.'},
    {'doc_id': 'f-other-flight', 'mailbox_id': 'Работа', 'subject': 'Изменение времени вылета', 'sender': 'Деловые поездки', 'date': '2026-11-20', 'body': 'Ваш самолёт улетит позже. Отправление рейса KB-930 в Екатеринбург перенесено на 22:10.'},
    {'doc_id': 'f-future', 'mailbox_id': 'Поездки', 'subject': 'Дополнительное изменение отправления рейса SN-401', 'sender': 'Север Авиа', 'date': '2026-11-22', 'body': 'Отправление перенесено на 23:20. Это обновление отменяет предыдущие уведомления.'},
    {'doc_id': 'work-110', 'mailbox_id': 'Работа', 'subject': 'Перенос созвона с командой', 'sender': 'Марина Лазарева', 'date': '2026-11-20', 'body': 'Совещание начнётся в 16:30 вместо запланированных 15:00. Ссылка на конференцию осталась прежней.'},
    {'doc_id': 'work-111', 'mailbox_id': 'Работа', 'subject': 'Созвон с командой в 11:00', 'sender': 'Марина Лазарева', 'date': '2026-11-19', 'body': 'Коллеги, встречаемся в 11:00. Повестка — обновление документов релиза.'},
    {'doc_id': 'work-112', 'mailbox_id': 'Работа', 'subject': 'Итоги интервью', 'sender': 'Команда найма', 'date': '2026-11-19', 'body': 'Вы прошли первичный этап. Следующее собеседование состоится в понедельник. Выберите время в календаре.'},
    {'doc_id': 'work-113', 'mailbox_id': 'Работа', 'subject': 'Акт за ноябрь', 'sender': 'Документооборот', 'date': '2026-11-20', 'body': 'Файл с закрывающими документами приложен к письму: act-63155.pdf. Отдельная подпись не требуется.'},
    {'doc_id': 'work-114', 'mailbox_id': 'Работа', 'subject': 'Действие ссылки заканчивается', 'sender': 'Документооборот', 'date': '2026-11-18', 'body': 'Срок подачи формы истекает 25 ноября в 18:00. После окончания приём заявок будет остановлен.'},
    {'doc_id': 'work-115', 'mailbox_id': 'Работа', 'subject': 'Новости офиса', 'sender': 'Офис-менеджер', 'date': '2026-11-21', 'body': 'В понедельник обновят гостевые пропуска. У стойки регистрации можно забрать временный бейдж.'},
    {'doc_id': 'shop-31', 'mailbox_id': 'Покупки', 'subject': 'Отправление доступно в ячейке', 'sender': 'Пункт выдачи', 'date': '2026-11-21', 'body': 'Заказ PK-62159 размещён в постамате на Лесной, 6. Заберите посылку до 24 ноября. Код доступа к ячейке — 471628.'},
    {'doc_id': 'shop-32', 'mailbox_id': 'Покупки', 'subject': 'Перенос времени доставки', 'sender': 'Логистика', 'date': '2026-11-18', 'body': 'Курьер приедет вечером, с 19:00 до 21:00. Посылка PK-61917 остаётся у курьера.'},
    {'doc_id': 'shop-33', 'mailbox_id': 'Покупки', 'subject': 'Автопродление тарифа', 'sender': 'Музыкальный сервис', 'date': '2026-11-20', 'body': 'Следующее списание 299 ₽ запланировано на 27 ноября. Управлять подпиской можно в настройках аккаунта.'},
    {'doc_id': 'shop-34', 'mailbox_id': 'Покупки', 'subject': 'Восстановление доступа', 'sender': 'Служба безопасности', 'date': '2026-11-16', 'body': 'Для сброса пароля пройдите по ссылке в личном кабинете. Ссылка действительна сорок минут.'},
    {'doc_id': 'shop-35', 'mailbox_id': 'Покупки', 'subject': 'Оцените недавнюю покупку', 'sender': 'Команда сервиса', 'date': '2026-11-19', 'body': 'Заполните короткую анкету о заказе PK-62159. Ваш отзыв поможет нам улучшить обслуживание.'},
    {'doc_id': 'shop-36', 'mailbox_id': 'Покупки', 'subject': 'Новый вход в аккаунт', 'sender': 'Служба безопасности', 'date': '2026-11-21', 'body': 'Вход выполнен в 07:12. Если это были не вы, завершите другие сеансы и смените пароль.'},
    {'doc_id': 'shop-37', 'mailbox_id': 'Покупки', 'subject': 'Ответ на обращение № 62159', 'sender': 'Помощь', 'date': '2026-11-19', 'body': 'Ваше обращение № 62159 принято. Ответ на технический вопрос будет направлен по электронной почте.'},
    {'doc_id': 'shop-38', 'mailbox_id': 'Покупки', 'subject': 'Вход · одноразовый пароль', 'sender': 'Служба безопасности', 'date': '2026-11-18', 'body': 'Одноразовый код 024681 действует пять минут. Никому не сообщайте его.'},
    {'doc_id': 'shop-39', 'mailbox_id': 'Покупки', 'subject': 'Компенсация за отмену заказа', 'sender': 'Магазин', 'date': '2026-11-17', 'body': 'Возврат на карточку завершён. Зачислены 1 130 рублей по заказу PK-61820.'},
    {'doc_id': 'shop-undated', 'mailbox_id': 'Покупки', 'subject': 'Квитанция за ремонт', 'sender': 'Сервисная мастерская', 'date': '', 'body': 'Работы выполнены. Фискальный чек с номером 893160 приложен к письму.'},
    {'doc_id': 'f-duplicates', 'mailbox_id': 'Поездки', 'subject': 'Городской трансфер в аэропорт', 'sender': 'Служба автобусов', 'date': '2026-11-20', 'body': 'Автобус опаздывает на десять минут. Новый график опубликован на остановке возле терминала.'},
    {'doc_id': 'work-116', 'mailbox_id': 'Работа', 'subject': 'Подтверждение участия', 'sender': 'Мастерская', 'date': '2026-11-19', 'body': 'Заявление на участие в мастерской принято. Для входа покажите приглашение.'},
    {'doc_id': 'work-117', 'mailbox_id': 'Работа', 'subject': 'Приглашение на обучение', 'sender': 'Мастерская', 'date': '2026-11-14', 'body': 'Онлайн-конференция начнётся в четверг. Анкета для регистрации закрывается за два дня.'},
]


HTML = r'''<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Почта · Контекст</title>
  <link rel="stylesheet" href="/styles.css"><script src="/app.js" defer></script>
</head>
<body>
  <svg class="symbols" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <symbol id="inbox-icon" viewBox="0 0 24 24"><path d="m3 14 3-9h12l3 9v5H3z M3 14h5l2 3h4l2-3h5"/></symbol>
    <symbol id="star-icon" viewBox="0 0 24 24"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z"/></symbol>
    <symbol id="search-icon" viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/></symbol>
    <symbol id="tune-icon" viewBox="0 0 24 24"><path d="M3 7h5m5 0h8M3 17h9m5 0h4"/><circle cx="10.5" cy="7" r="2.5"/><circle cx="14.5" cy="17" r="2.5"/></symbol>
    <symbol id="menu-icon" viewBox="0 0 24 24"><path d="M3 6h18M3 12h18M3 18h18"/></symbol>
    <symbol id="mail-icon" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m4 7 8 6 8-6"/></symbol>
  </svg>
  <header class="topbar">
    <div class="identity"><button class="icon-button menu" id="menu" aria-label="Ящики и папки" aria-expanded="false" aria-controls="folders"><svg><use href="#menu-icon"/></svg></button>
      <button class="brand" id="home" aria-label="Почта — ко входящим"><svg aria-hidden="true"><use href="#mail-icon"/></svg>Почта</button></div>
    <form class="search" id="search-form" role="search">
      <button class="icon-button search-submit" type="submit" aria-label="Найти"><svg><use href="#search-icon"/></svg></button>
      <input id="query" type="search" aria-label="Поиск писем" maxlength="600" placeholder="Поиск по почте" autocomplete="off">
      <button class="icon-button" id="filters-toggle" type="button" aria-label="Фильтры поиска" title="Фильтры поиска" aria-expanded="false" aria-controls="filters"><svg><use href="#tune-icon"/></svg></button>
    </form>
  </header>
  <section class="filters" id="filters" aria-label="Фильтры поиска" hidden>
    <label>Получено с<input type="date" id="after"></label><label>По<input type="date" id="before"></label>
    <label>Дата поиска<input type="date" id="as-of"></label>
    <button type="button" id="reset-filters" class="text-action">Сбросить</button>
  </section>
  <div class="layout" id="layout">
    <button id="scrim" class="scrim" aria-label="Закрыть список ящиков" hidden></button>
    <aside class="sidebar" id="folders" aria-label="Папки и ящики">
      <nav aria-label="Папки">
        <button type="button" class="nav-row is-selected" id="all-mail" aria-pressed="true"><svg><use href="#inbox-icon"/></svg>Входящие <span class="nav-count" id="mail-total"></span></button>
        <button type="button" class="nav-row" id="saved-mail" aria-pressed="false"><svg><use href="#star-icon"/></svg>Избранное <span class="nav-count" id="saved-total"></span></button>
      </nav>
      <h2>Почтовые ящики</h2><button type="button" class="text-action" id="add-box" hidden>Добавить ящик</button>
      <nav id="mailboxes" class="mailboxes" aria-label="Ящики"></nav>
      <h2>Ярлыки</h2><nav id="labels" aria-label="Мои ярлыки"></nav>
      <button class="nav-row" id="preferences">Настроить почту</button>
      <small id="source-label">Загрузка…</small>
    </aside>
    <main class="surface">
      <section class="results" aria-labelledby="result-title">
        <div class="results-head"><div><h1 id="result-title">Входящие</h1><p id="result-detail" role="status">Загружаем письма…</p></div>
          <div class="pagination" id="pagination" hidden><span id="pages"></span><button id="prev" aria-label="Предыдущая страница">‹</button><button id="next" aria-label="Следующая страница">›</button></div>
        </div>
        <div id="modes" class="modes" role="group" aria-label="Режим поиска" hidden>
          <button type="button" data-mode="smart" aria-pressed="true">Умный поиск</button>
          <button type="button" data-mode="lexical" aria-pressed="false">По словам</button>
          <button type="button" data-mode="semantic" id="semantic-mode" aria-pressed="false">По теме</button>
          <span class="timing" id="timing" hidden></span>
        </div>
        <div class="items" id="items"></div>
      </section>
      <article class="letter" id="letter" hidden aria-labelledby="letter-subject">
        <div class="letter-tools"><button class="text-action" id="back">← К списку</button><button type="button" class="text-action" id="save" aria-pressed="false">В избранное</button></div>
        <h1 id="letter-subject"></h1>
        <div class="letter-meta"><span class="avatar" id="letter-avatar" aria-hidden="true"></span><div><strong id="letter-sender"></strong><div id="letter-box"></div></div><time id="letter-date"></time></div>
        <section class="evidence" id="evidence" hidden><h2>Найденный фрагмент</h2><p id="evidence-text"></p><small id="signals"></small></section>
        <div class="letter-body" id="letter-body"></div>
        <div class="personalize"><label>Ярлык <select id="message-label"></select></label>
          <label>Моя тема письма <input id="custom-subject" maxlength="100" placeholder="Исходная тема"></label><button class="text-action" id="apply-subject">Сохранить тему</button></div>
      </article>
    </main>
  </div>
  <dialog id="settings"><form method="dialog" id="settings-form">
    <h2>Настройки почты</h2><p>Письма объединены по идентификатору ящика из данных. Названия, ярлыки и оформление сохраняются в этом браузере.</p>
    <label>Почтовый ящик<select id="edit-box"></select></label>
    <label>Название ящика<input id="box-name" maxlength="50"></label>
    <label>Цвет ящика<select id="box-color"></select></label>
    <label>Оформление<select id="theme"><option value="blue">Нейтральное</option><option value="sage">Шалфей</option><option value="plum">Слива</option></select></label>
    <label>Ярлык<select id="edit-label"><option value="">Новый ярлык</option></select></label>
    <label>Название ярлыка<input id="label-name" maxlength="45"></label><label>Цвет ярлыка<select id="label-color"></select></label>
    <div class="dialog-actions"><button type="button" id="cancel-settings">Отмена</button><button class="primary" type="submit">Сохранить</button></div>
  </form></dialog>
  <dialog id="new-box"><form method="dialog" id="new-box-form">
    <h2>Новый ящик</h2><p id="new-box-hint"></p>
    <label>Название<input id="new-box-name" maxlength="50" required autofocus></label>
    <div class="dialog-actions"><button type="button" id="cancel-new-box">Отмена</button><button class="primary" type="submit">Добавить</button></div>
  </form></dialog>
</body>
</html>
'''


CSS = r'''* { box-sizing: border-box; }
:root { color-scheme: light; --ink: #202321; --muted: #5a625e; --blue: #364640; --tint: #e7ebe8; --line: #e1e4e2; font: 16px/1.5 "Segoe UI", Arial, sans-serif; }
body { margin: 0; color: var(--ink); background: #f5f6f5; }
button, input { font: inherit; color: inherit; }
button { border: 0; cursor: pointer; background: transparent; }
button:focus-visible, input:focus-visible { outline: 3px solid #487ee4; outline-offset: 2px; }
button:disabled { opacity: .35; cursor: default; }
[hidden], .symbols { display: none !important; }
svg { width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; flex: 0 0 auto; }
.topbar { display: grid; grid-template-columns: 238px minmax(0, 720px); align-items: center; min-height: 76px; gap: 20px; padding: 10px 30px 10px 22px; background: #fff; border-bottom: 1px solid var(--line); }
.identity, .brand, .icon-button, .nav-row, .letter-meta, .letter-tools, .pagination, .filters, .modes, .results-head { display: flex; align-items: center; }
.brand { padding: 6px 10px; gap: 10px; font-size: 21px; font-weight: 650; border-radius: 5px; }
.brand:hover { background: var(--tint); }
.icon-button, .pagination button { width: 42px; height: 42px; justify-content: center; border-radius: 8px; color: #4a596c; }
.icon-button:hover, .pagination button:enabled:hover, .text-action:hover { background: #e6ebf2; }
.menu { display: none; }
.search { display: flex; align-items: center; gap: 5px; padding: 2px 6px; min-width: 0; border: 1px solid var(--line); background: #f6f7f6; border-radius: 6px; }
.search:focus-within { background: white; border-color: #87a7dc; }
.search input { flex: 1; width: 0; border: 0; background: transparent; font-size: 18px; padding: 7px 0; outline: none; }
.search input:focus-visible { outline: none; }
.search input::placeholder { color: #536072; opacity: 1; }
.filters { margin: 0 30px 14px 280px; gap: 18px; flex-wrap: wrap; }
.filters label { font-size: 14px; color: var(--muted); }
.filters input { display: block; border: 1px solid #c7d0db; background: white; border-radius: 7px; padding: 5px; min-width: 0; }
.layout { display: grid; grid-template-columns: 238px minmax(0, 1fr); gap: 20px; padding: 18px 22px 22px; }
.sidebar { display: flex; flex-direction: column; padding: 12px 0 18px; align-self: start; min-height: calc(100dvh - 112px); }
.nav-row { gap: 13px; width: 100%; min-height: 45px; padding: 7px 16px; border-radius: 8px; text-align: left; overflow-wrap: anywhere; font-size: 16px; }
.nav-row:hover { background: #eaedea; }
.nav-row.is-selected { background: var(--tint); color: var(--blue); font-weight: 650; }
.nav-count { margin-left: auto; font-size: 14px; color: var(--muted); font-variant-numeric: tabular-nums; }
.is-selected .nav-count { color: inherit; }
.sidebar h2 { font-size: 14px; font-weight: 600; color: var(--muted); margin: 32px 16px 8px; }
.mailboxes .nav-row::before { content: ''; width: 9px; height: 9px; border: 2px solid #8292a8; border-radius: 50%; margin: 0 5px; flex-shrink: 0; }
.sidebar small { color: var(--muted); font-size: 13px; margin: auto 16px 0; padding-top: 35px; }
.surface { border: 1px solid var(--line); border-radius: 6px; background: white; min-height: calc(100dvh - 116px); min-width: 0; overflow: clip; }
.results-head { gap: 12px; justify-content: space-between; padding: 30px 36px 21px; border-bottom: 1px solid var(--line); }
h1 { font-size: clamp(28px, 2.5vw, 38px); line-height: 1.2; margin: 0; letter-spacing: -.8px; font-weight: 620; overflow-wrap: anywhere; }
#result-detail { color: var(--muted); margin: 5px 0 0; font-size: 15px; overflow-wrap: anywhere; }
.pagination { gap: 2px; flex-shrink: 0; color: var(--muted); font-size: 14px; }
.pagination button { font-size: 27px; line-height: 1; }
.pagination span { margin-right: 7px; }
.mail-item { width: 100%; display: grid; grid-template-columns: 44px minmax(120px, .8fr) minmax(200px, 2.35fr) auto; gap: 18px; align-items: center; text-align: left; padding: 17px 35px; border-bottom: 1px solid var(--line); min-height: 81px; }
.mail-item:hover { background: #f6f8f6; }
.mail-item:focus-visible { outline-offset: -3px; }
.avatar { display: inline-grid; place-items: center; flex-shrink: 0; width: 38px; height: 38px; background: #eef0ee; color: #4c5751; border-radius: 6px; font-size: 15px; font-weight: 650; }
.sender, .subject, .item-preview { display: block; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.sender { font-weight: 590; font-size: 16px; }
.subject { font-size: 18px; line-height: 1.45; font-weight: 580; }
.item-preview, .time { color: var(--muted); font-size: 15px; }
.time { white-space: nowrap; font-variant-numeric: tabular-nums; font-size: 14px; }
.modes { padding: 12px 32px; gap: 4px; flex-wrap: wrap; border-bottom: 1px solid var(--line); }
.modes button, .text-action { font-size: 15px; padding: 8px 12px; border-radius: 7px; }
.modes button[aria-pressed="true"], .text-action[aria-pressed="true"] { background: var(--tint); color: var(--blue); }
.modes button:hover { background: #eff3f8; }
.timing { margin-left: auto; color: var(--muted); font-size: 14px; }
.empty-items { padding: 48px 36px; color: var(--muted); }
.reading .results { display: none; }
.letter { max-width: 950px; margin: auto; padding: 25px clamp(18px,5vw,68px) 70px; }
.letter-tools { justify-content: space-between; margin: 0 -10px 28px; }
.text-action { color: var(--blue); }
.letter-meta { gap: 12px; margin: 25px 0; }
.letter-meta > div { min-width: 0; overflow-wrap: anywhere; }
.letter-meta time { margin-left: auto; color: var(--muted); font-size: 14px; }
#letter-box, .evidence small { color: var(--muted); font-size: 14px; }
.evidence { margin: 28px 0; padding: 3px 16px; border-left: 3px solid #96b7ed; }
.evidence h2 { font-size: 15px; color: #315a8c; margin: 0; }
.evidence p { margin: 6px 0; overflow-wrap: anywhere; }
mark { background: #ffebad; color: inherit; }
.letter-body { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 18px; line-height: 1.7; border-top: 1px solid var(--line); padding-top: 26px; }
@media (max-width: 1080px) {
  .topbar,.layout { grid-template-columns: 190px minmax(0,1fr); gap: 12px; }
  .filters { margin-left: 224px; }
  .mail-item { gap: 12px; padding-inline: 22px; grid-template-columns: 40px minmax(110px,.85fr) minmax(130px,1.8fr) auto; }
  .results-head { padding-inline: 22px; }
}
@media (max-width: 760px) {
  .topbar { grid-template-columns: auto minmax(0,1fr); padding: 12px; min-height: 68px; gap: 10px; }
  .brand { font-size: 0; padding: 5px; }
  .menu { display: flex; }.icon-button { width: 36px; }
  .layout { display: block; padding: 0; }.surface { min-height: calc(100dvh - 68px); border-radius: 0; border-inline: 0; }
  .sidebar { display: none; }.layout.nav-open .sidebar { display: flex; position: fixed; inset: 0 auto 0 0; width: min(285px,82vw); background: #f6f8fb; z-index: 3; padding: 28px 14px; overflow-y: auto; }
  .scrim { position: fixed; inset: 0; width: 100%; height: 100%; background: #1c283c55; z-index: 2; }
  .filters { margin: 0 14px 12px; gap: 12px; }.search input { font-size: 16px; }
  .results-head { padding: 24px 17px 16px; }.pagination span { display: none; }
  .mail-item { grid-template-columns: 38px minmax(0,1fr) auto; row-gap: 1px; column-gap: 12px; padding: 13px 17px; }
  .mail-item .avatar { grid-column: 1; grid-row: 1 / 3; width: 37px; height: 37px; font-size: 14px; }
  .mail-item .sender { grid-column: 2; grid-row: 1; font-size: 15px; }.mail-item .mail-text { grid-column: 2 / 4; grid-row: 2; }
  .mail-item .time { grid-column: 3; grid-row: 1; font-size: 13px; }.subject { font-size: 17px; }
  .modes { padding: 9px 12px; gap: 1px; }.modes button { padding: 7px 8px; }
  .letter { padding: 17px 20px 45px; }.letter-meta { flex-wrap: wrap; }.letter-meta time { font-size: 13px; }
}
@media (min-width: 761px) { .scrim { display: none; } }
body[data-theme="sage"] { --blue:#276149; --tint:#dfefe6; background:#f1f6f2; }
body[data-theme="plum"] { --blue:#773d7c; --tint:#f1e3f3; background:#f7f2f8; }
body[data-theme] .nav-row.is-selected { color:var(--blue); background:var(--tint); }
.color-blue { --mark:#4877b9; }.color-green { --mark:#388063; }.color-purple { --mark:#8253a4; }.color-orange { --mark:#b26924; }
.mailboxes .nav-row::before,.label-link::before { border-color:var(--mark); }
.label-link::before { content:''; display:block; width:11px; height:11px; margin:0 5px; border-radius:3px; background:var(--mark); }
.tag { display:inline-block; color:var(--mark); border:1px solid currentColor; border-radius:4px; font-size:13px; padding:0 5px; }
#preferences { margin-top:16px; color:var(--blue); }
#add-box { align-self:flex-start; margin:0 4px 6px; }
.mailboxes { max-height: 46vh; overflow-y: auto; }
dialog { width:min(480px,94vw); border:1px solid var(--line); border-radius:14px; padding:24px; color:var(--ink); max-height:90dvh; }
dialog::backdrop { background:#25314355; } dialog h2 { margin:0; } dialog p { color:var(--muted); font-size:14px; }
dialog label,.personalize label { display:grid; gap:3px; font-size:14px; margin:8px 0; }
dialog input,dialog select,.personalize input,.personalize select { font:inherit; background:white; border:1px solid #b7c2d0; border-radius:6px; padding:7px; }
.dialog-actions,.personalize { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-top:20px; }
.dialog-actions { justify-content:flex-end; }.primary { color:white; background:var(--blue); padding:8px 16px; border-radius:7px; }
'''


JS = r''''use strict';

const element = (id) => document.getElementById(id);
const list = element('items');
const queryInput = element('query');
const layout = element('layout');
const pageSize = 18;
const customMailboxLimit = 7;
const number = new Intl.NumberFormat('ru-RU');
const previewDate = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', timeZone: 'UTC' });
const longDate = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'long', timeZone: 'UTC' });
const state = { mailbox: '', saved: false, mode: 'smart', query: '', page: 0, open: null, openedRow: null, editedLetter: false, listRun: 0, letterRun: 0,
  meta: null, storage: '', shortlist: new Set() };

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text != null) item.textContent = text;
  return item;
}

function dateLabel(value, format) {
  if (!value) return '';
  const parsed = new Date(value.slice(0, 10));
  return Number.isFinite(+parsed) ? format.format(parsed) : value;
}

function senderOf(item) {
  return item.sender || 'Отправитель не указан';
}

function titleOf(item) {
  if (prefs.subjects[item.doc_id]) return prefs.subjects[item.doc_id];
  return item.subject || (item.preview || item.body || '').trim().split('\n')[0].slice(0, 84) || 'Письмо без темы';
}

function initials(name) {
  const parts = name.match(/[\p{L}\p{N}]+/gu) || ['П'];
  return parts.slice(0, 2).map(word => word[0]).join('').toUpperCase();
}

async function api(route, values = {}) {
  const params = new URLSearchParams(Object.entries(values).filter(([, value]) => value !== '' && value != null));
  const response = await fetch(`/api/${route}?${params}`);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Сервис недоступен.');
  return { body, timing: response.headers.get('Server-Timing') };
}

function refreshSelection() {
  for (const button of list.querySelectorAll('.mail-item')) {
    button.setAttribute('aria-pressed', String(button.dataset.doc === state.open));
  }
  const saved = state.shortlist.has(state.open);
  element('save').setAttribute('aria-pressed', String(saved));
  element('save').textContent = saved ? '★ В избранном' : '☆ В избранное';
  element('saved-total').textContent = state.shortlist.size ? number.format(state.shortlist.size) : '';
}

function closeLetter(returnFocus = false) {
  const lastOpen = state.open;
  state.letterRun++;
  state.open = null;
  state.openedRow = null;
  state.editedLetter = false;
  element('letter').hidden = true;
  layout.classList.remove('reading');
  refreshSelection();
  if (returnFocus) {
    const button = [...list.querySelectorAll('.mail-item')].find(item => item.dataset.doc === lastOpen);
    button?.focus();
  }
}

function returnToList() {
  if (!state.editedLetter) {
    closeLetter(true);
    return;
  }
  const docId = state.open;
  refresh().then(() => [...list.querySelectorAll('.mail-item')].find(item => item.dataset.doc === docId)?.focus());
}

function highlight(container, text, query) {
  const matching = new Set((query.match(/[\p{L}\p{N}]{3,}/gu) || []).map(word => word.toLowerCase().replaceAll('ё', 'е')));
  let from = 0;
  container.replaceChildren();
  for (const token of text.matchAll(/[\p{L}\p{N}]{3,}/gu)) {
    if (!matching.has(token[0].toLowerCase().replaceAll('ё', 'е'))) continue;
    container.append(document.createTextNode(text.slice(from, token.index)), node('mark', '', token[0]));
    from = token.index + token[0].length;
  }
  container.append(document.createTextNode(text.slice(from)));
}

async function openLetter(row, explicit = false) {
  const run = ++state.letterRun;
  try {
    const { body: letter } = await api('letter', { id: row.doc_id });
    if (run !== state.letterRun) return;
    state.open = letter.doc_id;
    state.openedRow = row;
    element('letter').hidden = false;
    element('letter-subject').textContent = titleOf({ ...letter, preview: row.subject ? '' : row.preview });
    element('letter-sender').textContent = senderOf(letter);
    element('letter-box').textContent = letter.sender ? letter.mailbox_id : '';
    element('letter-avatar').textContent = initials(senderOf(letter));
    element('letter-avatar').className = avatarClass(senderOf(letter));
    element('letter-date').textContent = dateLabel(letter.date, longDate) || 'Дата неизвестна';
    element('letter-date').dateTime = letter.date.slice(0, 10);
    element('letter-body').textContent = letter.body || 'В письме нет текста.';
    element('custom-subject').value = prefs.subjects[letter.doc_id] || '';
    element('custom-subject').placeholder = letter.subject || 'Исходная тема';
    fillLabels(element('message-label'), 'Без ярлыка', prefs.assigned[letter.doc_id] || '');
    element('letter-box').textContent = boxTitle(letter.mailbox_id);
    element('evidence').hidden = !row.evidence;
    if (row.evidence) {
      highlight(element('evidence-text'), row.evidence, state.query);
      element('signals').textContent = (row.signals || []).join(' · ');
    }
    layout.classList.add('reading');
    refreshSelection();
    if (explicit) {
      element('back').focus({preventScroll: true});
      window.scrollTo(0, 0);
    }
  } catch (error) {
    if (run === state.letterRun) element('result-detail').textContent = error.message;
  }
}

function avatarClass(name) {
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return 'avatar tone-' + hash % 5;
}

function render(rows, emptyMessage) {
  list.replaceChildren();
  if (!rows.length) {
    list.append(node('p', 'empty-items', emptyMessage));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of rows) {
    const button = node('button', 'mail-item');
    button.type = 'button';
    button.dataset.doc = item.doc_id;
    button.setAttribute('aria-pressed', String(state.open === item.doc_id));
    const avatar = node('span', avatarClass(senderOf(item)), initials(senderOf(item)));
    avatar.setAttribute('aria-hidden', 'true');
    const content = node('span', 'mail-text');
    content.append(node('span', 'subject', titleOf(item)),
      node('span', 'item-preview', item.evidence || item.preview || ''));
    const label = prefs.labels[prefs.assigned[item.doc_id]];
    if (label) content.prepend(node('span', 'tag color-' + label.color, label.name));
    button.append(avatar, node('span', 'sender', senderOf(item)), content, node('span', 'time', dateLabel(item.date, previewDate)));
    button.addEventListener('click', () => openLetter(item, true));
    fragment.append(button);
  }
  list.append(fragment);
}

function navSelection() {
  const buttons = [element('all-mail'), element('saved-mail'), ...element('mailboxes').children];
  for (const button of buttons) {
    const selected = !state.label && (button.id === 'saved-mail' ? state.saved : !state.saved && (button.dataset.box || '') === state.mailbox);
    button.setAttribute('aria-pressed', String(selected));
    button.classList.toggle('is-selected', selected);
  }
}

async function refresh() {
  const run = ++state.listRun;
  const searching = Boolean(state.query && !state.saved && !state.label);
  const offset = state.page * pageSize;
  closeLetter();
  navSelection();
  element('result-title').textContent = state.label ? prefs.labels[state.label].name : state.saved ? 'Избранное' : searching ? 'Результаты поиска' : boxTitle(state.mailbox) || 'Входящие';
  element('result-detail').textContent = 'Загрузка…';
  element('timing').hidden = true;
  element('modes').hidden = !searching || isCustomMailbox(state.mailbox);
  element('pagination').hidden = true;
  for (const button of element('modes').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.mode === state.mode));
  list.replaceChildren();
  try {
    const filters = { mailbox: state.mailbox, after: element('after').value, before: element('before').value, as_of: element('as-of').value || state.meta?.reference_day };
    let result;
    let total;
    if (state.saved || state.label) {
      const ids = state.label ? Object.keys(prefs.assigned).filter(id => prefs.assigned[id] === state.label) : [...state.shortlist].reverse();
      const items = await Promise.all(ids.slice(offset, offset + pageSize).map(async (id) => {
        try { const { body } = await api('letter', { id }); return { ...body, preview: body.body.slice(0, 260) }; }
        catch { return null; }
      }));
      result = { body: { items: items.filter(Boolean), total: ids.length } };
    } else if (isCustomMailbox(state.mailbox)) {
      result = { body: { items: [] } };
    } else if (searching) {
      result = await api('search', { ...filters, q: state.query, mode: state.mode, limit: 40 });
    } else {
      result = await api('letters', { ...filters, limit: pageSize, offset });
    }
    if (run !== state.listRun) return;
    const rows = result.body.items;
    total = result.body.total ?? rows.length;
    const timing = result.timing?.match(/dur=([\d.]+)/);
    if (timing) {
      element('timing').textContent = `${number.format(Math.round(10 * Number(timing[1])) / 10)} мс`;
      element('timing').title = 'Время обработки этого запроса на сервере';
      element('timing').hidden = false;
    }
    const ending = new Intl.PluralRules('ru').select(total);
    const noun = {one: 'письмо', few: 'письма', many: 'писем', other: 'письма'}[ending];
    element('result-detail').textContent = searching ? `«${state.query}» · в выдаче ${total}` : `${number.format(total)} ${noun}`;
    render(rows, state.saved ? 'Здесь появятся письма, которые вы добавите в избранное.' :
      searching ? 'Совпадений нет. Попробуйте другое описание или снимите фильтр.' : 'В этом разделе пока нет писем.');
    if (!searching && total > pageSize) {
      element('pagination').hidden = false;
      element('pages').textContent = `${number.format(offset + 1)}–${number.format(Math.min(offset + pageSize, total))} из ${number.format(total)}`;
      element('prev').disabled = state.page === 0;
      element('next').disabled = offset + pageSize >= total;
    }
  } catch (error) {
    if (run === state.listRun) {
      element('result-detail').textContent = 'Не удалось загрузить письма';
      render([], error.message);
    }
  }
}

function selectMailbox(mailbox = '', saved = false, clear = false) {
  state.label = '';
  toggleMenu(false);
  state.mailbox = mailbox;
  state.saved = saved;
  state.page = 0;
  if (clear) { state.query = ''; queryInput.value = ''; }
  refresh();
}

function submitSearch() {
  state.label = '';
  toggleMenu(false);
  state.query = queryInput.value.trim();
  state.saved = false;
  state.page = 0;
  refresh();
}

element('home').addEventListener('click', () => selectMailbox('', false, true));
element('all-mail').addEventListener('click', () => selectMailbox('', false, true));
element('saved-mail').addEventListener('click', () => selectMailbox('', true, true));
element('search-form').addEventListener('submit', (event) => { event.preventDefault(); submitSearch(); });
queryInput.addEventListener('search', () => { if (!queryInput.value && state.query) submitSearch(); });
element('filters-toggle').addEventListener('click', () => {
  const filters = element('filters');
  filters.hidden = !filters.hidden;
  element('filters-toggle').setAttribute('aria-expanded', String(!filters.hidden));
});
for (const id of ['after', 'before', 'as-of']) element(id).addEventListener('change', () => { state.page = 0; refresh(); });
element('reset-filters').addEventListener('click', () => {
  for (const id of ['after', 'before', 'as-of']) element(id).value = '';
  element('as-of').value = state.meta?.reference_day || '';
  state.page = 0;
  refresh();
});
for (const button of element('modes').querySelectorAll('button')) button.addEventListener('click', () => {
  if (state.mode !== button.dataset.mode) { state.mode = button.dataset.mode; refresh(); }
});
element('back').addEventListener('click', returnToList);
element('prev').addEventListener('click', () => { if (state.page > 0) { state.page--; refresh(); } });
element('next').addEventListener('click', () => { state.page++; refresh(); });
element('save').addEventListener('click', () => {
  if (state.open == null) return;
  if (state.shortlist.has(state.open)) state.shortlist.delete(state.open);
  else state.shortlist.add(state.open);
  try { localStorage.setItem(state.storage, JSON.stringify([...state.shortlist])); } catch { /* Selection remains available in this tab. */ }
  refreshSelection();
  if (state.saved) { state.page = 0; refresh(); }
});
function toggleMenu(open) {
  layout.classList.toggle('nav-open', open);
  element('menu').setAttribute('aria-expanded', String(open));
  element('scrim').hidden = !open;
}
element('menu').addEventListener('click', () => toggleMenu(!layout.classList.contains('nav-open')));
element('scrim').addEventListener('click', () => toggleMenu(false));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (layout.classList.contains('nav-open')) { toggleMenu(false); element('menu').focus(); }
    else if (state.open && !document.querySelector('dialog[open]')) returnToList();
  }
  if (event.key === '/' && !document.querySelector('dialog[open]') && !event.altKey && !event.ctrlKey && !event.metaKey && !event.target.matches('input, textarea, [contenteditable]')) {
    event.preventDefault();
    queryInput.focus();
  }
});

async function init() {
  try {
    const { body } = await api('meta');
    state.meta = body;
    loadPrefs(body);
    state.storage = `context-shortlist-v1-${body.demo ? 'demo' : 'corpus'}`;
    try {
      const saved = JSON.parse(localStorage.getItem(state.storage) || '[]');
      if (Array.isArray(saved)) state.shortlist = new Set(saved.filter(id => typeof id === 'string'));
    } catch { /* The browser may disable local storage. */ }
    element('mail-total').textContent = number.format(body.count);
    element('source-label').textContent = body.demo ? 'Демонстрационные письма' : 'Архив хакатона';
    element('as-of').value = body.reference_day || '';
    element('semantic-mode').textContent = body.semantic ? 'По смыслу' : 'По теме';
    renderFolders();
    await refresh();
  } catch (error) {
    element('source-label').textContent = 'Сервис недоступен';
    element('result-detail').textContent = error.message;
  }
}

const colors = {blue:'Синий', green:'Зелёный', purple:'Фиолетовый', orange:'Оранжевый'};
let prefs = {boxes:{},customBoxes:{},labels:{},assigned:{},subjects:{},theme:'blue'};
let prefsKey;
function loadPrefs(meta) {
  prefsKey = 'context-preferences-v1-' + JSON.stringify(meta.mailboxes.map(b => b.id));
  try {
    const saved = JSON.parse(localStorage.getItem(prefsKey));
    if (saved) Object.assign(prefs, saved);
  } catch { /* Defaults work when storage is unavailable. */ }
  document.body.dataset.theme = prefs.theme;
}
function persist() {
  try { localStorage.setItem(prefsKey, JSON.stringify(prefs)); } catch { /* Available for this session. */ }
}
function isCustomMailbox(id) { return Object.hasOwn(prefs.customBoxes, id); }
function boxTitle(id) { return prefs.boxes[id]?.name || prefs.customBoxes[id]?.name || id; }
function mailboxRows() {
  return [...state.meta.mailboxes, ...Object.keys(prefs.customBoxes).map(id => ({id, count:0}))];
}
function fillLabels(select, empty, selected) {
  select.replaceChildren(new Option(empty, ''));
  for (const [id,label] of Object.entries(prefs.labels)) select.add(new Option(label.name,id));
  select.value = selected;
}
function renderFolders() {
  const boxes = element('mailboxes'), labels = element('labels');
  boxes.replaceChildren(); labels.replaceChildren();
  const add = element('add-box');
  add.hidden = false;
  add.disabled = Object.keys(prefs.customBoxes).length >= customMailboxLimit;
  add.textContent = add.disabled ? 'Добавлено 7 ящиков' : 'Добавить ящик';
  for (const {id,count} of mailboxRows()) {
    const button = node('button', 'nav-row color-' + (prefs.boxes[id]?.color || 'blue'), boxTitle(id));
    button.dataset.box = id; button.title = id;
    button.append(node('span','nav-count',number.format(count)));
    button.onclick = () => selectMailbox(id);
    boxes.append(button);
  }
  for (const [id,label] of Object.entries(prefs.labels)) {
    const button = node('button','nav-row label-link color-' + label.color,label.name);
    button.onclick = () => {
      toggleMenu(false); state.label=id; state.saved=false; state.page=0; state.query=''; queryInput.value=''; state.mailbox=''; refresh();
    };
    labels.append(button);
  }
}
function showBoxSettings() {
  const id = element('edit-box').value;
  element('box-name').value = boxTitle(id);
  element('box-color').value = prefs.boxes[id]?.color || 'blue';
}
function showLabelSettings() {
  const label = prefs.labels[element('edit-label').value];
  element('label-name').value = label?.name || '';
  element('label-color').value = label?.color || 'blue';
}
for (const id of ['box-color','label-color']) {
  for (const [key,name] of Object.entries(colors)) element(id).add(new Option(name,key));
}
element('preferences').onclick = () => {
  if (!state.meta) return;
  toggleMenu(false);
  const select = element('edit-box');
  const mailboxes = mailboxRows();
  select.replaceChildren(...mailboxes.map(b => new Option(boxTitle(b.id),b.id)));
  select.value = state.mailbox || mailboxes[0]?.id || '';
  showBoxSettings(); fillLabels(element('edit-label'),'Новый ярлык',state.label || '');
  showLabelSettings(); element('theme').value = prefs.theme;
  element('settings').showModal();
};
element('edit-box').onchange = showBoxSettings;
element('edit-label').onchange = showLabelSettings;
element('cancel-settings').onclick = () => element('settings').close();
element('add-box').onclick = () => {
  const input = element('new-box-name');
  input.value = '';
  input.setCustomValidity('');
  element('new-box-hint').textContent = 'Ящик создаётся пустым и сохраняется в этом браузере.';
  element('new-box').showModal();
};
element('cancel-new-box').onclick = () => element('new-box').close();
element('new-box-name').oninput = event => event.target.setCustomValidity('');
element('new-box-form').onsubmit = event => {
  event.preventDefault();
  if (Object.keys(prefs.customBoxes).length >= customMailboxLimit) return;
  const input = element('new-box-name');
  const name = input.value.trim();
  if (!name) {
    input.setCustomValidity('Введите название ящика.');
    input.reportValidity();
    return;
  }
  let id = 'local-1';
  for (let index = 2; mailboxRows().some(box => box.id === id); index++) id = 'local-' + index;
  prefs.customBoxes[id] = {name};
  persist();
  renderFolders();
  element('new-box').close();
  selectMailbox(id, false, true);
};
element('settings-form').onsubmit = event => {
  event.preventDefault();
  const id=element('edit-box').value;
  if (id) prefs.boxes[id]={name:element('box-name').value.trim() || prefs.customBoxes[id]?.name || id,color:element('box-color').value};
  const name=element('label-name').value.trim();
  if (name) {
    const key=element('edit-label').value || 'label-' + Date.now().toString(36);
    prefs.labels[key]={name,color:element('label-color').value};
  }
  prefs.theme=element('theme').value; document.body.dataset.theme=prefs.theme;
  persist(); renderFolders(); element('settings').close(); refresh();
};
element('message-label').onchange = event => {
  if (event.target.value) prefs.assigned[state.open]=event.target.value;
  else delete prefs.assigned[state.open];
  persist();
  state.editedLetter = true;
};
element('apply-subject').onclick = () => {
  const text=element('custom-subject').value.trim();
  if (text) prefs.subjects[state.open]=text; else delete prefs.subjects[state.open];
  persist();
  state.editedLetter = true;
  if (text) element('letter-subject').textContent = text;
  else if (state.openedRow) openLetter(state.openedRow);
};
init();
'''


def main():
    import webbrowser
    parser = argparse.ArgumentParser(description='Context mail search')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--dataset', type=Path, help='Organizer data directory or original ZIP')
    source.add_argument('--demo', action='store_true', help='Open 30 built-in examples')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0, help='0 selects a free TCP port')
    add_model_args(parser)
    args = parser.parse_args()
    try:
        dataset = None if args.demo else args.dataset or locate_dataset()
        if dataset is None and not args.demo and (ROOT / 'documents.jsonl').is_file():
            dataset = ROOT
        if dataset:
            folder = load_dataset(dataset, args.cache_dir)
            letters = load_letters(folder / 'documents.jsonl')
        else:
            letters = [Letter(**row) for row in DEMO_LETTERS]
        encoder = E5Encoder(args.model_dir) if args.model_dir else None
        print('Indexing %s letters...' % len(letters), flush=True)
        engine = SearchEngine(letters, encoder, args.cache_dir)
        assets = {url: (text.encode('utf8'), PATHS[url][1])
                  for url, text in (('/', HTML), ('/styles.css', CSS), ('/app.js', JS))}
        with MailServer(('127.0.0.1', args.port), engine, dataset is None, assets) as server:
            url = 'http://127.0.0.1:%d/' % server.server_port
            print(url, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + chr(10))


if __name__ == '__main__':
    main()
