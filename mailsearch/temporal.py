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
