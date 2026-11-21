"""Build the readable one-file application from the normal Python, HTML, CSS and JS sources."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def source_module(name):
    text = (ROOT / 'mailsearch' / (name + '.py')).read_text(encoding='utf8')
    lines = text.splitlines(keepends=True)
    strip = set()
    for node in ast.walk(ast.parse(text)):
        imported_here = isinstance(node, ast.ImportFrom) and node.level
        other_entry = isinstance(node, ast.FunctionDef) and node.name == 'main'
        main_guard = isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"
        if imported_here or other_entry or main_guard:
            strip.update(range(node.lineno - 1, node.end_lineno))
    text = ''.join(line for i, line in enumerate(lines) if i not in strip)
    if name == 'data':
        text = text.replace('Path(__file__).resolve().parent.parent', 'Path(__file__).resolve().parent')
    return text.rstrip()


def multiline(value):
    if "'''" in value or value.endswith('\\'):
        raise ValueError('Embedded source needs another literal delimiter')
    return "r'''" + value + "'''"


def build():
    sections = ['"""Почта · Контекст. Python 3.11+. Run directly to open the inbox."""']
    for name in ('data', 'temporal', 'semantic', 'engine', 'evaluate', 'server'):
        sections += ['# ' + name + '.py', source_module(name)]
    rows = [json.loads(row) for row in (ROOT/'fixtures/letters.jsonl').read_text(encoding='utf8').splitlines()]
    sections += ['DEMO_LETTERS = [\n' + ''.join('    ' + repr(obj) + ',\n' for obj in rows) + ']']
    for key,filename in [('HTML','index.html'),('CSS','styles.css'),('JS','app.js')]:
        sections.append(key + ' = ' + multiline((ROOT/'web'/filename).read_text(encoding='utf8')))
    sections.append("""def main():
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
    main()""")
    out = ROOT/'START_CONTEXT.py'
    out.write_bytes(('\n\n\n'.join(sections) + '\n').encode('utf8'))
    return out


if __name__ == '__main__':
    print(build())
