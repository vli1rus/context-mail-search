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


ROOT = Path(__file__).resolve().parent.parent
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
