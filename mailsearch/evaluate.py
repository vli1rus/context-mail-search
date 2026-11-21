import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from time import perf_counter_ns

from .data import (DEMO_CORPUS, DEMO_QRELS, DEMO_QUERIES, check_day, field,
                   load_dataset, load_letters, locate_dataset, records)
from .engine import SearchEngine
from .semantic import E5Encoder, add_model_args


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


def main():
    parser = argparse.ArgumentParser(description='Evaluate and export ranked mail search results')
    parser.add_argument('--dataset', type=Path, help='Organizer ZIP or extracted directory')
    parser.add_argument('--demo', action='store_true', help='Force the built-in fixtures')
    parser.add_argument('--corpus', type=Path)
    parser.add_argument('--queries', type=Path)
    parser.add_argument('--qrels', type=Path)
    parser.add_argument('--mode', choices=['all', 'lexical', 'semantic', 'smart'], default='all')
    parser.add_argument('--k', type=int, nargs='+', default=[1, 5, 10])
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--export-k', type=int, default=10)
    add_model_args(parser)
    args = parser.parse_args()
    if any(number < 1 for number in (*args.k, args.repeat, args.export_k)):
        parser.error('K and repeat must be positive')
    if args.demo and (args.dataset or args.corpus or args.queries or args.qrels):
        parser.error('--demo cannot be combined with input paths')
    try:
        dataset = None if args.demo or args.corpus else args.dataset or locate_dataset()
        folder = load_dataset(dataset, args.cache_dir) if dataset else None
        corpus = args.corpus or (folder / 'documents.jsonl' if folder else DEMO_CORPUS)
        query_path = args.queries or (folder / 'queries_dev.jsonl' if folder else DEMO_QUERIES)
        qrels_path = args.qrels or (None if args.queries else (folder / 'qrels_dev.jsonl' if folder else DEMO_QRELS))
        letters = load_letters(corpus)
        queries = list(read_queries(query_path))
        if not queries:
            raise ValueError('No queries available')
        qrels = read_qrels(qrels_path) if qrels_path else None
        encoder = E5Encoder(args.model_dir) if args.model_dir else None
        engine = SearchEngine(letters, encoder, args.cache_dir)
        modes = ['lexical', 'semantic', 'smart'] if args.mode == 'all' else [args.mode]
        if args.output and args.mode == 'all':
            parser.error('Specify --mode to export a single ranking')
        results, rankings = evaluate(engine, queries, qrels, modes, args.k, args.repeat, args.export_k)
        if args.output:
            with args.output.open('w', encoding='utf-8', newline='\n') as output:
                for row in rankings[args.mode]:
                    row['doc_ids'] = row['doc_ids'][:args.export_k]
                    print(json.dumps(row, ensure_ascii=False), file=output)
        print(json.dumps({'source': 'demo' if corpus == DEMO_CORPUS else str(corpus), 'letters': len(letters),
                          'queries': len(queries), 'labeled_queries': sum(bool((qrels or {}).get(q[0])) for q in queries),
                          'semantic': 'E5 ONNX' if encoder else 'offline topic expansion',
                          'timing': 'ranking and evidence, excludes HTTP serialization', 'modes': results}, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
