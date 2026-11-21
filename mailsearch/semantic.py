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


def main():
    parser = argparse.ArgumentParser(description='Download and verify the local E5 ONNX model')
    parser.add_argument('command', choices=['download'])
    parser.add_argument('--model-dir', type=Path, default=Path('.models'))
    args = parser.parse_args()
    try:
        download(args.model_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')
    print(f'Model ready in {args.model_dir}')


if __name__ == '__main__':
    main()
