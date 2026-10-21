"""Run isolated public-data retrieval benchmarks, optionally using configured model services."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crew.wiki.benchmark import (
    DEFAULT_VARIANTS,
    VARIANTS,
    load_beir,
    load_cmrc2018,
    run_benchmark,
    write_report,
)
from crew.wiki.benchmark_models import (
    CachedEmbeddingProvider,
    CachedRerankingProvider,
    ModelOutputCache,
    PacedRerankingProvider,
)
from crew.wiki.config import WikiRerankConfig, WikiSemanticConfig
from crew.wiki.embedding import build_embedding_provider
from crew.wiki.rerank import HTTPRerankingProvider
from crew.wiki.schemas import WikiPage

SCIFACT_URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip"
SCIFACT_MD5 = "5f7d1de60b170fc8027bb7898e2efca1"
CMRC_URL = "https://raw.githubusercontent.com/ymcui/cmrc2018/master/squad-style-data/cmrc2018_dev.json"


def download(url: str, path: Path) -> None:
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def prepare_scifact(data_dir: Path, allow_download: bool, split: str = "test") -> Path:
    root = data_dir / "scifact"
    names = ("corpus.jsonl", "queries.jsonl", f"qrels/{split}.tsv")
    if all((root / name).is_file() for name in names):
        return root
    archive = data_dir / "scifact.zip"
    if allow_download:
        print("Downloading SciFact (BEIR)…", flush=True)
        download(SCIFACT_URL, archive)
    if not archive.is_file():
        raise FileNotFoundError("SciFact data missing; pass --download or provide --data-dir")
    if hashlib.md5(archive.read_bytes()).hexdigest() != SCIFACT_MD5:
        raise ValueError("SciFact archive checksum differs from the BEIR published checksum")
    with zipfile.ZipFile(archive) as zipped:
        for name in names:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".partial")
            with zipped.open(f"scifact/{name}") as source, partial.open("wb") as output:
                shutil.copyfileobj(source, output)
            partial.replace(target)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("all", "scifact", "cmrc2018"), default="all")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "outputs/wiki-benchmark/data")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/wiki-benchmark/results")
    parser.add_argument("--download", action="store_true", help="Download public dataset files")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cmrc-limit", type=int, default=200)
    parser.add_argument("--scifact-limit", type=int, help="By default use all labeled test queries")
    parser.add_argument("--scifact-split", choices=("train", "test"), default="test")
    parser.add_argument("--candidate-limit", type=int, default=50)
    parser.add_argument("--index-weight", type=float, default=0.0,
                        help="0 uses navigation as fallback; positive values enable joint RRF")
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=list(DEFAULT_VARIANTS))
    parser.add_argument("--fuse-weight", type=float, default=0.9)
    parser.add_argument("--rerank-limit", type=int, default=50)
    parser.add_argument("--model-config", type=Path, help="Read an existing Ace YAML config; no writes")
    parser.add_argument("--model-env", type=Path, help="Read existing credential env file; never copied to reports")
    parser.add_argument("--model-cache", type=Path, default=ROOT / "outputs/wiki-benchmark/model-cache")
    parser.add_argument("--rerank-model", default="BAAI/bge-reranker-v2-m3")
    parser.add_argument("--rerank-tpm-budget", type=int, default=300_000,
                        help="Conservative offline rerank token budget per minute; 0 disables token pacing")
    parser.add_argument("--cutoffs", nargs="+", type=int, default=[5, 10])
    parser.add_argument("--pages", type=Path, help="Exported WikiPage JSONL snapshot; single dataset only")
    parser.add_argument("--source-map", type=Path, help="JSON object: RawSource ID -> corpus document ID")
    args = parser.parse_args(argv)
    if args.pages and (args.dataset == "all" or not args.source_map):
        parser.error("--pages requires a single --dataset and --source-map")
    if args.source_map and not args.pages:
        parser.error("--source-map requires --pages")
    if args.cmrc_limit <= 0 or (args.scifact_limit is not None and args.scifact_limit <= 0):
        parser.error("Query limits must be positive")
    if not args.cutoffs or min(args.cutoffs) <= 0:
        parser.error("Cutoffs must be positive")
    if args.candidate_limit <= 0 or args.rerank_limit <= 0 or args.rerank_tpm_budget < 0 or any(
        not math.isfinite(weight) or weight < 0 for weight in (args.index_weight, args.fuse_weight)
    ):
        parser.error("Candidate limit must be positive and index weight nonnegative")
    needs_embedding = any(v.startswith("wiki-hybrid") or v == "wiki-vector" for v in args.variants)
    needs_rerank = any(v.endswith("-rerank") for v in args.variants)
    if (needs_embedding or needs_rerank) and not args.model_config:
        parser.error("Model variants require --model-config pointing to existing Ace settings")
    embedding = reranker = None
    resources = []
    try:
        code_sha256 = hashlib.sha256(b"".join(
            (ROOT / filename).read_bytes() for filename in (
                "crew/wiki/benchmark.py", "crew/wiki/query.py", "crew/wiki/search.py",
                "crew/wiki/store/_filesystem.py", "crew/wiki/_utils.py",
                "scripts/run_wiki_benchmark.py", "crew/wiki/schemas.py",
                "crew/wiki/rerank.py", "crew/wiki/embedding.py", "crew/wiki/vector.py",
                "crew/wiki/benchmark_models.py",
            )
        )).hexdigest()
        model_metadata = {}
        if needs_embedding or needs_rerank:
            import yaml
            from dotenv import dotenv_values

            raw = yaml.safe_load(args.model_config.read_text(encoding="utf-8")) or {}
            wiki = raw.get("wiki") or {}
            semantic = WikiSemanticConfig.from_raw(wiki.get("semantic"))
            semantic.enabled = True  # Opt-in for this isolated experiment only.
            rerank_config = WikiRerankConfig.from_raw(wiki.get("rerank"))
            env_path = args.model_env or args.model_config.parent / ".env"
            env = dotenv_values(env_path) if env_path.is_file() else {}
            def key_for(name):
                name = name or "CREW_API_KEY"
                return str(env.get(name) or os.environ.get(name) or "")

            if needs_embedding:
                provider = build_embedding_provider(semantic, api_key=key_for(semantic.api_key_env))
                if provider is None:
                    raise ValueError("Configured embedding provider is unavailable")
                cache = ModelOutputCache(args.model_cache / "embedding.sqlite", semantic.base_url + provider.model)
                resources.extend([provider, cache])
                embedding = CachedEmbeddingProvider(provider, cache, progress=lambda msg: print(msg, flush=True))
            if needs_rerank:
                provider = HTTPRerankingProvider(
                    api_key=key_for(rerank_config.api_key_env or semantic.api_key_env),
                    base_url=rerank_config.base_url or semantic.base_url, model=args.rerank_model,
                    timeout=rerank_config.timeout, max_chunks_per_doc=rerank_config.max_chunks_per_doc,
                    max_retries=rerank_config.max_retries,
                )
                identity = f"{rerank_config.base_url or semantic.base_url}:{provider.model}:chunks={rerank_config.max_chunks_per_doc}"
                cache = ModelOutputCache(args.model_cache / "rerank.sqlite", identity)
                resources.extend([provider, cache])
                paced = PacedRerankingProvider(provider, tokens_per_minute=args.rerank_tpm_budget,
                                              progress=lambda msg: print(msg, flush=True))
                reranker = CachedRerankingProvider(paced, cache)
            model_metadata = {
                "embedding_model": embedding.model if embedding else None,
                "rerank_model": reranker.model if reranker else None,
                "rerank_max_chunks_per_doc": rerank_config.max_chunks_per_doc,
                "rerank_tpm_budget": args.rerank_tpm_budget,
                "model_revision": "remote service does not expose immutable weight revision",
                "cache_scope": "endpoint/model/query-or-document text; rerank also includes chunk policy",
                "embedding_query_instruction": "provider default; none for configured BGE-M3 service",
            }
        pages = None
        source_map = None
        if args.pages:
            pages = [WikiPage.from_dict(json.loads(line))
                     for line in args.pages.read_text(encoding="utf-8").splitlines() if line.strip()]
            source_map = json.loads(args.source_map.read_text(encoding="utf-8"))
        names = ("scifact", "cmrc2018") if args.dataset == "all" else (args.dataset,)
        for name in names:
            if name == "scifact":
                root = prepare_scifact(args.data_dir, args.download, args.scifact_split)
                dataset = load_beir(root, split=args.scifact_split).sample(args.scifact_limit, args.seed)
                source_url = SCIFACT_URL
            else:
                path = args.data_dir / "cmrc2018_dev.json"
                if args.download:
                    print("Downloading CMRC2018 dev…", flush=True)
                    download(CMRC_URL, path)
                dataset = load_cmrc2018(path).sample(args.cmrc_limit, args.seed)
                source_url = CMRC_URL
            dataset.metadata["source_url"] = source_url
            print(f"Loaded {name}: {len(dataset.documents)} documents, {len(dataset.queries)} queries", flush=True)
            before = {"embedding_live_inputs": embedding.live_inputs if embedding else 0,
                      "rerank_live_pairs": reranker.live_pairs if reranker else 0,
                      "rerank_rate_waits": paced.rate_limit_waits if reranker else 0,
                      "rerank_wait_seconds": paced.wait_seconds if reranker else 0,
                      "rerank_tokens": paced.input_tokens if reranker else 0}
            report, rows = run_benchmark(
                dataset, variants=args.variants, cutoffs=args.cutoffs,
                pages=pages, source_map=source_map, progress=lambda message: print(message, flush=True),
                candidate_limit=args.candidate_limit, index_weight=args.index_weight,
                fuse_weight=args.fuse_weight, embedding_provider=embedding,
                reranker=reranker, rerank_limit=args.rerank_limit,
            )
            report["model_metadata"] = model_metadata
            report["live_model_inputs"] = {
                "embedding": (embedding.live_inputs if embedding else 0) - before["embedding_live_inputs"],
                "rerank_pairs": (reranker.live_pairs if reranker else 0) - before["rerank_live_pairs"],
            }
            report["model_cache_sha256"] = {
                "embedding": embedding.cache.fingerprint() if embedding else None,
                "rerank": reranker.cache.fingerprint() if reranker else None,
            }
            if reranker:
                report["rerank_pacing"] = {
                    "rate_limit_waits": paced.rate_limit_waits - before["rerank_rate_waits"],
                    "wait_seconds": paced.wait_seconds - before["rerank_wait_seconds"],
                    "input_tokens_actual_or_estimated": paced.input_tokens - before["rerank_tokens"],
                }
            report["runtime"] = {
                "python": platform.python_version(), "system": platform.system(),
                "packages": {name: version(name) for name in ("openai", "httpx")},
            }
            report["created_at_utc"] = datetime.now(UTC).isoformat()
            report["code_sha256"] = code_sha256
            output = args.output / name
            write_report(output, report, rows)
            print(f"Report: {output / 'report.md'}", flush=True)
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for resource in reversed(resources):
            resource.close()


if __name__ == "__main__":
    raise SystemExit(main())
