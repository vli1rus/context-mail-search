import heapq
import math
import re
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from threading import RLock

from .temporal import received_period, relative_range

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
            from .semantic import DenseIndex

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
