"""Embedding relevance benchmark harness.

Self-contained scoring of BM25-only vs embeddings-only vs BM25+embeddings
rerank over the code_index codebase. Not a pytest — run with
`python -m bench.embeddings.run --corpus self`.
"""
