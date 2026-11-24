"""chromadb 本地向量检索源。

PersistentClient 本地持久化 + bge-small-zh-v1.5 本地 embedding（CPU 可跑），
经 register_vendor("search_corpus", "chroma", ...) 注册，与 local_corpus 同签名可互换。
"""
