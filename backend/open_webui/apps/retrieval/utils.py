import logging
import os
import uuid
from typing import Optional, Union
from collections import defaultdict

import requests

from huggingface_hub import snapshot_download
from langchain.retrievers import ContextualCompressionRetriever, EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document


from open_webui.apps.ollama.main import (
    GenerateEmbeddingsForm,
    generate_ollama_embeddings,
)
from open_webui.apps.retrieval.vector.connector import VECTOR_DB_CLIENT
from open_webui.config import VECTOR_DB
from open_webui.utils.misc import get_last_user_message

from open_webui.env import SRC_LOG_LEVELS


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["RAG"])


from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.retrievers import BaseRetriever


class VectorSearchRetriever(BaseRetriever):
    collection_name: Any
    embedding_function: Any
    top_k: int

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        result = VECTOR_DB_CLIENT.search(
            collection_name=self.collection_name,
            vectors=[self.embedding_function(query)],
            limit=self.top_k,
        )

        ids = result.ids[0]
        metadatas = result.metadatas[0]
        documents = result.documents[0]

        results = []
        for idx in range(len(ids)):
            results.append(
                Document(
                    metadata=metadatas[idx],
                    page_content=documents[idx],
                )
            )
        return results


def query_doc(
    collection_name: str,
    query_embedding: list[float],
    k: int,
):
    try:
        result = VECTOR_DB_CLIENT.search(
            collection_name=collection_name,
            vectors=[query_embedding],
            limit=k,
        )

        log.info(f"query_doc:result {result}")
        return result
    except Exception as e:
        print(e)
        raise e


def query_doc_with_hybrid_search(
    collection_name: str,
    query: str,
    embedding_function,
    k: int,
    reranking_function,
    r: float,
) -> dict:
    try:
        result = VECTOR_DB_CLIENT.get(collection_name=collection_name)

        bm25_retriever = BM25Retriever.from_texts(
            texts=result.documents[0],
            metadatas=result.metadatas[0],
        )
        bm25_retriever.k = k

        vector_search_retriever = VectorSearchRetriever(
            collection_name=collection_name,
            embedding_function=embedding_function,
            top_k=k,
        )

        ensemble_retriever = EnsembleRetriever(
            retrievers=[bm25_retriever, vector_search_retriever], weights=[0.5, 0.5]
        )
        compressor = RerankCompressor(
            embedding_function=embedding_function,
            top_n=k,
            reranking_function=reranking_function,
            r_score=r,
        )

        compression_retriever = ContextualCompressionRetriever(
            base_compressor=compressor, base_retriever=ensemble_retriever
        )

        result = compression_retriever.invoke(query)
        result = {
            "distances": [[d.metadata.get("score") for d in result]],
            "documents": [[d.page_content for d in result]],
            "metadatas": [[d.metadata for d in result]],
        }

        log.info(f"query_doc_with_hybrid_search:result {result}")
        return result
    except Exception as e:
        raise e


def merge_and_sort_query_results(
    query_results: list[dict], k: int, reverse: bool = False
) -> list[dict]:
    # Initialize lists to store combined data
    combined_distances = []
    combined_documents = []
    combined_metadatas = []

    for data in query_results:
        combined_distances.extend(data["distances"][0])
        combined_documents.extend(data["documents"][0])
        combined_metadatas.extend(data["metadatas"][0])

    # Create a list of tuples (distance, document, metadata)
    combined = list(zip(combined_distances, combined_documents, combined_metadatas))

    # Sort the list based on distances
    combined.sort(key=lambda x: x[0], reverse=reverse)

    # We don't have anything :-(
    if not combined:
        sorted_distances = []
        sorted_documents = []
        sorted_metadatas = []
    else:
        # Unzip the sorted list
        sorted_distances, sorted_documents, sorted_metadatas = zip(*combined)

        # Slicing the lists to include only k elements
        sorted_distances = list(sorted_distances)[:k]
        sorted_documents = list(sorted_documents)[:k]
        sorted_metadatas = list(sorted_metadatas)[:k]

    # Create the output dictionary
    result = {
        "distances": [sorted_distances],
        "documents": [sorted_documents],
        "metadatas": [sorted_metadatas],
    }

    return result


def query_collection(
    collection_names: list[str],
    query: str,
    embedding_function,
    k: int,
) -> dict:

    results = []
    query_embedding = embedding_function(query)

    for collection_name in collection_names:
        if collection_name:
            try:
                result = query_doc(
                    collection_name=collection_name,
                    k=k,
                    query_embedding=query_embedding,
                )
                results.append(result.model_dump())
            except Exception as e:
                log.exception(f"Error when querying the collection: {e}")
        else:
            pass

    return merge_and_sort_query_results(results, k=k)


def query_collection_with_hybrid_search(
    collection_names: list[str],
    query: str,
    embedding_function,
    k: int,
    reranking_function,
    r: float,
) -> dict:
    results = []
    error = False
    for collection_name in collection_names:
        try:
            result = query_doc_with_hybrid_search(
                collection_name=collection_name,
                query=query,
                embedding_function=embedding_function,
                k=k,
                reranking_function=reranking_function,
                r=r,
            )
            results.append(result)
        except Exception as e:
            log.exception(
                "Error when querying the collection with " f"hybrid_search: {e}"
            )
            error = True

    if error:
        raise Exception(
            "Hybrid search failed for all collections. Using Non hybrid search as fallback."
        )

    return merge_and_sort_query_results(results, k=k, reverse=True)


def rag_template(template: str, context: str, query: str):
    count = template.count("[context]")
    assert "[context]" in template, "RAG template does not contain '[context]'"

    if "<context>" in context and "</context>" in context:
        log.debug(
            "WARNING: Potential prompt injection attack: the RAG "
            "context contains '<context>' and '</context>'. This might be "
            "nothing, or the user might be trying to hack something."
        )

    if "[query]" in context:
        query_placeholder = f"[query-{str(uuid.uuid4())}]"
        template = template.replace("[query]", query_placeholder)
        template = template.replace("[context]", context)
        template = template.replace(query_placeholder, query)
    else:
        template = template.replace("[context]", context)
        template = template.replace("[query]", query)
    return template


def get_embedding_function(
    embedding_engine,
    embedding_model,
    embedding_function,
    openai_key,
    openai_url,
    batch_size,
):
    if embedding_engine == "":
        return lambda query: embedding_function.encode(query).tolist()
    elif embedding_engine in ["ollama", "openai"]:
        if embedding_engine == "ollama":
            func = lambda query: generate_ollama_embeddings(
                GenerateEmbeddingsForm(
                    **{
                        "model": embedding_model,
                        "prompt": query,
                    }
                )
            )
        elif embedding_engine == "openai":
            func = lambda query: generate_openai_embeddings(
                model=embedding_model,
                text=query,
                key=openai_key,
                url=openai_url,
            )

        def generate_multiple(query, f):
            if isinstance(query, list):
                if embedding_engine == "openai":
                    embeddings = []
                    for i in range(0, len(query), batch_size):
                        embeddings.extend(f(query[i : i + batch_size]))
                    return embeddings
                else:
                    return [f(q) for q in query]
            else:
                return f(query)

        return lambda query: generate_multiple(query, func)


def extract_relevant_contexts_single_collection(
    files,
    messages,
    embedding_function,
    k,
    reranking_function,
    r,
    hybrid_search,
):
    collections = []
    file_ids = []
    relevant_contexts = []
    files_mapping = {}

    for file in files:
        try:
            if file.get("context") == "full":
                context = {
                    "documents": [[file.get("file").get("data", {}).get("content")]],
                    "metadatas": [[{"file_id": file.get("id"), "name": file.get("name")}]],
                }
                relevant_contexts.append(context)
            else:

                collections.append(file["id"])
                file_ids.extend([file_id for file_id in file["data"]["file_ids"]])

            files_mapping.update({
                file_id: file
                for file_id in file["data"]["file_ids"]
            })

        except Exception as e:
            log.exception(e)

    try:
        rewritten_queries = VECTOR_DB_CLIENT.rewrite_query(messages)
        search_results = VECTOR_DB_CLIENT.compute_rrf(
            rewritten_queries,
            collections,
            file_ids,
            k
        )

    except Exception as e:
        log.exception(e)

    relevant_contexts = [
        {**context.model_dump(), "file": files_mapping[collection]}
        for collection, context in search_results.items()
    ]

    return relevant_contexts


def extract_relevant_contexts_multiple_collections(
    files,
    messages,
    embedding_function,
    k,
    reranking_function,
    r,
    hybrid_search,
):
    query = get_last_user_message(messages)

    extracted_collections = []
    relevant_contexts = []

    for file in files:
        if file.get("context") == "full":
            context = {
                "documents": [[file.get("file").get("data", {}).get("content")]],
                "metadatas": [[{"file_id": file.get("id"), "name": file.get("name")}]],
            }
        else:
            context = None

            collection_names = []
            if file.get("type") == "collection":
                if file.get("legacy"):
                    collection_names = file.get("collection_names", [])
                else:
                    collection_names.append(file["id"])
            elif file.get("collection_name"):
                collection_names.append(file["collection_name"])
            elif file.get("id"):
                if file.get("legacy"):
                    collection_names.append(f"{file['id']}")
                else:
                    collection_names.append(f"file-{file['id']}")

            collection_names = set(collection_names).difference(extracted_collections)
            if not collection_names:
                log.debug(f"skipping {file} as it has already been extracted")
                continue

            try:
                context = None
                if file.get("type") == "text":
                    context = file["content"]
                else:
                    if hybrid_search:
                        try:
                            context = query_collection_with_hybrid_search(
                                collection_names=collection_names,
                                query=query,
                                embedding_function=embedding_function,
                                k=k,
                                reranking_function=reranking_function,
                                r=r,
                            )
                        except Exception as e:
                            log.debug(
                                "Error when using hybrid search, using"
                                " non hybrid search as fallback."
                            )

                    if (not hybrid_search) or (context is None):
                        context = query_collection(
                            collection_names=collection_names,
                            query=query,
                            embedding_function=embedding_function,
                            k=k,
                        )
            except Exception as e:
                log.exception(e)

            extracted_collections.extend(collection_names)

        if context:
            relevant_contexts.append({**context, "file": file})
    
    return relevant_contexts


def extract_relevant_contexts(
    files,
    messages,
    embedding_function,
    k,
    reranking_function,
    r,
    hybrid_search,
):
    extract_relevant_contexts_method = None
    if VECTOR_DB in ['chroma', 'milvus']:
        extract_relevant_contexts_method = extract_relevant_contexts_multiple_collections
    elif VECTOR_DB == 'azure-search':
        extract_relevant_contexts_method = extract_relevant_contexts_single_collection
    else:
        raise ValueError(f"Unsupported VECTOR_DB: {VECTOR_DB}")
    
    return extract_relevant_contexts_method(
        files,
        messages,
        embedding_function,
        k,
        reranking_function,
        r,
        hybrid_search,
    )



def get_rag_context(
    files,
    messages,
    embedding_function,
    k,
    reranking_function,
    r,
    hybrid_search,
):
    log.debug(f"files: {files} {messages} {embedding_function} {reranking_function}")
    query = get_last_user_message(messages)

    relevant_contexts = extract_relevant_contexts(
        files,
        messages,
        embedding_function,
        k,
        reranking_function,
        r,
        hybrid_search
    )

    contexts = []
    citations = []
    for context in relevant_contexts:
        try:
            if "documents" in context:
                contexts.append(
                    "\n\n".join(
                        [text for text in context["documents"][0] if text is not None]
                    )
                )

                if "metadatas" in context:
                    citations.append(
                        {
                            "source": context["file"],
                            "document": context["documents"][0],
                            "metadata": context["metadatas"][0],
                        }
                    )
        except Exception as e:
            log.exception(e)

    return contexts, citations


def get_model_path(model: str, update_model: bool = False):
    # Construct huggingface_hub kwargs with local_files_only to return the snapshot path
    cache_dir = os.getenv("SENTENCE_TRANSFORMERS_HOME")

    local_files_only = not update_model

    snapshot_kwargs = {
        "cache_dir": cache_dir,
        "local_files_only": local_files_only,
    }

    log.debug(f"model: {model}")
    log.debug(f"snapshot_kwargs: {snapshot_kwargs}")

    # Inspiration from upstream sentence_transformers
    if (
        os.path.exists(model)
        or ("\\" in model or model.count("/") > 1)
        and local_files_only
    ):
        # If fully qualified path exists, return input, else set repo_id
        return model
    elif "/" not in model:
        # Set valid repo_id for model short-name
        model = "sentence-transformers" + "/" + model

    snapshot_kwargs["repo_id"] = model

    # Attempt to query the huggingface_hub library to determine the local path and/or to update
    try:
        model_repo_path = snapshot_download(**snapshot_kwargs)
        log.debug(f"model_repo_path: {model_repo_path}")
        return model_repo_path
    except Exception as e:
        log.exception(f"Cannot determine model snapshot path: {e}")
        return model


def generate_openai_embeddings(
    model: str,
    text: Union[str, list[str]],
    key: str,
    url: str = "https://api.openai.com/v1",
):
    if isinstance(text, list):
        embeddings = generate_openai_batch_embeddings(model, text, key, url)
    else:
        embeddings = generate_openai_batch_embeddings(model, [text], key, url)

    return embeddings[0] if isinstance(text, str) else embeddings


def generate_openai_batch_embeddings(
    model: str, texts: list[str], key: str, url: str = "https://api.openai.com/v1"
) -> Optional[list[list[float]]]:
    try:
        r = requests.post(
            f"{url}/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            json={"input": texts, "model": model},
        )
        r.raise_for_status()
        data = r.json()
        if "data" in data:
            return [elem["embedding"] for elem in data["data"]]
        else:
            raise "Something went wrong :/"
    except Exception as e:
        print(e)
        return None


import operator
from typing import Optional, Sequence

from langchain_core.callbacks import Callbacks
from langchain_core.documents import BaseDocumentCompressor, Document


class RerankCompressor(BaseDocumentCompressor):
    embedding_function: Any
    top_n: int
    reranking_function: Any
    r_score: float

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = True

    def compress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Optional[Callbacks] = None,
    ) -> Sequence[Document]:
        reranking = self.reranking_function is not None

        if reranking:
            scores = self.reranking_function.predict(
                [(query, doc.page_content) for doc in documents]
            )
        else:
            from sentence_transformers import util

            query_embedding = self.embedding_function(query)
            document_embedding = self.embedding_function(
                [doc.page_content for doc in documents]
            )
            scores = util.cos_sim(query_embedding, document_embedding)[0]

        docs_with_scores = list(zip(documents, scores.tolist()))
        if self.r_score:
            docs_with_scores = [
                (d, s) for d, s in docs_with_scores if s >= self.r_score
            ]

        result = sorted(docs_with_scores, key=operator.itemgetter(1), reverse=True)
        final_results = []
        for doc, doc_score in result[: self.top_n]:
            metadata = doc.metadata
            metadata["score"] = doc_score
            doc = Document(
                page_content=doc.page_content,
                metadata=metadata,
            )
            final_results.append(doc)
        return final_results
