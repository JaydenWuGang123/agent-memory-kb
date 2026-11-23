#!/usr/bin/env python3
"""Zero-cost, 100% local OpenAI-compatible /v1/embeddings server (Apple Silicon MPS / CUDA / CPU).

Default model: Qwen/Qwen3-Embedding-0.6B (Apache-2.0, 1024 dims, MRL 32-1024, 32K context).
Uses official left-padding + last-token pooling + L2 normalization.
Also supports lightweight CPU ONNX MiniLM models via --backend onnx.
"""
from __future__ import annotations

import argparse
import hmac
import os
import threading
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DEFAULT_RAG_DIR = Path(os.environ.get("WEKNORA_RAG_DIR", str(Path.home() / "weknora-rag"))).expanduser()
DEFAULT_QWEN_DIR = DEFAULT_RAG_DIR / "models" / "Qwen3-Embedding-0.6B"
DEFAULT_QWEN_NAME = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_ONNX_DIR = Path.home() / ".cache" / "agent-memory-kb" / "minilm-onnx"
MAX_BATCH_TEXTS = 64
MAX_TEXT_CHARS = 24000


class Qwen3EmbeddingEngine:
    """Official Transformers Qwen3-Embedding engine with MPS / CUDA / CPU acceleration."""

    def __init__(self, model_dir: Path, max_length: int = 4096):
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self._F = torch.nn.functional
        self.model_dir = Path(model_dir).expanduser().resolve()
        self.max_length = max_length
        self.native_dim = 1024
        self._lock = threading.Lock()

        if not (self.model_dir / "config.json").is_file() or not (self.model_dir / "model.safetensors").is_file():
            raise FileNotFoundError(f"Qwen3-Embedding model files not found in: {self.model_dir}")

        if torch.backends.mps.is_available():
            self.device = "mps"
            dtype = torch.bfloat16
        elif torch.cuda.is_available():
            self.device = "cuda"
            dtype = torch.bfloat16
        else:
            self.device = "cpu"
            dtype = torch.float32

        self._tok = AutoTokenizer.from_pretrained(str(self.model_dir), padding_side="left", local_files_only=True)
        try:
            self._model = AutoModel.from_pretrained(
                str(self.model_dir),
                dtype=dtype,
                local_files_only=True,
            ).to(self.device)
        except Exception:
            dtype = torch.float16 if self.device in ("mps", "cuda") else torch.float32
            self._model = AutoModel.from_pretrained(
                str(self.model_dir),
                dtype=dtype,
                local_files_only=True,
            ).to(self.device)
        self._model.eval()
        self.dtype_name = str(dtype).replace("torch.", "")

    def _last_token_pool(self, last_hidden_states, attention_mask):
        torch = self._torch
        left_padding = attention_mask[:, -1].sum() == attention_mask.shape[0]
        if left_padding:
            return last_hidden_states[:, -1]
        sequence_lengths = attention_mask.sum(dim=1) - 1
        batch_size = last_hidden_states.shape[0]
        return last_hidden_states[torch.arange(batch_size, device=last_hidden_states.device), sequence_lengths]

    def encode(self, texts: list[str], dimensions: int | None = None, batch_size: int = 8) -> tuple[list[list[float]], int]:
        torch = self._torch
        F = self._F
        if not texts:
            return [], 0
        out_dim = dimensions if dimensions is not None else self.native_dim
        if out_dim < 32 or out_dim > self.native_dim:
            raise ValueError(f"dimensions must be between 32 and {self.native_dim}, got {out_dim}")

        cleaned = [(t or " ")[:MAX_TEXT_CHARS] for t in texts]
        indexed = sorted(enumerate(cleaned), key=lambda x: len(x[1]))
        out_vecs = np.zeros((len(cleaned), out_dim), dtype=np.float32)
        total_tokens = 0

        with self._lock, torch.inference_mode():
            for start in range(0, len(indexed), batch_size):
                chunk = indexed[start : start + batch_size]
                batch_texts = [t for _, t in chunk]
                batch_dict = self._tok(
                    batch_texts,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                total_tokens += int(batch_dict["attention_mask"].sum().item())
                batch_dict = {k: v.to(self.device) for k, v in batch_dict.items()}
                outputs = self._model(**batch_dict)
                pooled = self._last_token_pool(outputs.last_hidden_state, batch_dict["attention_mask"]).float()
                if out_dim < self.native_dim:
                    pooled = pooled[:, :out_dim]
                normed = F.normalize(pooled, p=2, dim=1).cpu().numpy()
                for row_idx, (orig_idx, _) in enumerate(chunk):
                    out_vecs[orig_idx] = normed[row_idx]
            if self.device == "mps":
                torch.mps.empty_cache()

        return [row.tolist() for row in out_vecs], int(total_tokens)


class LocalOnnxEngine:
    """Optional lightweight CPU ONNX MiniLM engine (384 dims)."""

    def __init__(self, model_dir: Path, max_length: int = 256, threads: int = 4):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.model_dir = Path(model_dir).expanduser().resolve()
        self.max_length = max_length
        self.native_dim = 384
        self.device = "cpu"
        self.dtype_name = "int8"
        self._lock = threading.Lock()
        onnx_path = self.model_dir / "model_int8.onnx"
        tok_path = self.model_dir / "tokenizer.json"
        if not onnx_path.is_file() or not tok_path.is_file():
            raise FileNotFoundError(f"ONNX model files not found in: {self.model_dir}")
        self._tok = Tokenizer.from_file(str(tok_path))
        self._tok.enable_truncation(max_length=self.max_length)
        self._tok.enable_padding(length=None)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self._sess = ort.InferenceSession(str(onnx_path), sess_options=opts, providers=["CPUExecutionProvider"])
        self._input_names = [i.name for i in self._sess.get_inputs()]

    def encode(self, texts: list[str], dimensions: int | None = None, batch_size: int = 32) -> tuple[list[list[float]], int]:
        if dimensions is not None and dimensions != self.native_dim:
            raise ValueError(f"fixed ONNX dimensions={self.native_dim}, got {dimensions}")
        if not texts:
            return [], 0
        cleaned = [(t or " ")[:MAX_TEXT_CHARS] for t in texts]
        indexed = sorted(enumerate(cleaned), key=lambda x: len(x[1]))
        out_vecs = np.zeros((len(cleaned), self.native_dim), dtype=np.float32)
        total_tokens = 0

        with self._lock:
            for start in range(0, len(indexed), batch_size):
                chunk = indexed[start : start + batch_size]
                encs = self._tok.encode_batch([t for _, t in chunk])
                total_tokens += sum(sum(e.attention_mask) for e in encs)
                ids = np.array([e.ids for e in encs], dtype=np.int64)
                mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
                feed = {}
                for name in self._input_names:
                    if "input_ids" in name:
                        feed[name] = ids
                    elif "attention_mask" in name:
                        feed[name] = mask
                    elif "token_type" in name:
                        feed[name] = np.zeros_like(ids)
                hidden = self._sess.run(None, feed)[0]
                m_exp = mask[:, :, None].astype(np.float32)
                pooled = (hidden * m_exp).sum(axis=1) / np.clip(m_exp.sum(axis=1), 1e-9, None)
                norms = np.linalg.norm(pooled, axis=1, keepdims=True)
                normed = (pooled / np.clip(norms, 1e-9, None)).astype(np.float32)
                for row_idx, (orig_idx, _) in enumerate(chunk):
                    out_vecs[orig_idx] = normed[row_idx]

        return [row.tolist() for row in out_vecs], int(total_tokens)


class EmbeddingRequest(BaseModel):
    model: str = Field(default=DEFAULT_QWEN_NAME)
    input: str | list[str]
    encoding_format: str | None = None
    dimensions: int | None = None


def create_app(engine: Any, api_key: str, model_name: str) -> FastAPI:
    app = FastAPI(title="Local OpenAI-Compatible Embedding Server", docs_url=None, redoc_url=None)
    created_ts = int(time.time())

    def _verify_auth(authorization: str | None) -> None:
        if not api_key:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")
        token = authorization[7:].strip()
        if not hmac.compare_digest(token, api_key):
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "model": model_name,
            "dimensions": engine.native_dim,
            "device": engine.device,
            "dtype": engine.dtype_name,
        }

    @app.get("/v1/models")
    def list_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _verify_auth(authorization)
        return {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": created_ts,
                    "owned_by": f"local-{engine.device}",
                }
            ],
        }

    def _handle_embeddings(req: EmbeddingRequest, authorization: str | None) -> dict[str, Any]:
        _verify_auth(authorization)
        texts = [req.input] if isinstance(req.input, str) else list(req.input)
        if not texts:
            raise HTTPException(status_code=400, detail="input must not be empty")
        if len(texts) > MAX_BATCH_TEXTS:
            raise HTTPException(status_code=400, detail=f"batch size exceeds limit ({MAX_BATCH_TEXTS})")
        try:
            vectors, total_tokens = engine.encode(texts, dimensions=req.dimensions)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": idx, "embedding": vec}
                for idx, vec in enumerate(vectors)
            ],
            "model": req.model or model_name,
            "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
        }

    @app.post("/v1/embeddings")
    def v1_embeddings(req: EmbeddingRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return _handle_embeddings(req, authorization)

    @app.post("/embeddings")
    def root_embeddings(req: EmbeddingRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return _handle_embeddings(req, authorization)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Local OpenAI-compatible Embedding API (Qwen3-Embedding-0.6B)")
    parser.add_argument("--backend", choices=["qwen3", "onnx"], default=os.environ.get("LOCAL_EMBED_BACKEND", "qwen3"))
    parser.add_argument("--host", default=os.environ.get("LOCAL_EMBED_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LOCAL_EMBED_PORT", "8001")))
    parser.add_argument("--model-dir", type=Path, default=None)
    parser.add_argument("--model-name", default=os.environ.get("LOCAL_EMBED_MODEL_NAME", DEFAULT_QWEN_NAME))
    parser.add_argument("--api-key", default=os.environ.get("LOCAL_EMBED_API_KEY", ""))
    args = parser.parse_args()

    if args.backend == "onnx":
        model_dir = args.model_dir or Path(os.environ.get("LOCAL_EMBED_MODEL_DIR", str(DEFAULT_ONNX_DIR)))
        engine = LocalOnnxEngine(model_dir=model_dir)
    else:
        model_dir = args.model_dir or Path(os.environ.get("LOCAL_EMBED_MODEL_DIR", str(DEFAULT_QWEN_DIR)))
        engine = Qwen3EmbeddingEngine(model_dir=model_dir)

    app = create_app(engine=engine, api_key=args.api_key.strip(), model_name=args.model_name)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
