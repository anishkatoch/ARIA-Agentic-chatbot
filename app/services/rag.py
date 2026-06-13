import logging
import os
import time
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)


def get_llm() -> ChatGroq:
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    return ChatGroq(
        model=model,
        groq_api_key=os.getenv("GROQ_API_KEY"),
        temperature=0.2,
    )


def chunk_text(text: str) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = splitter.split_text(text)
    logger.debug(f"[CHUNK] Split into {len(chunks)} chunks (size=1000, overlap=200)")
    return chunks


def chunk_text_with_offsets(text: str) -> list[tuple[str, int]]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200, add_start_index=True)
    docs = splitter.create_documents([text])
    return [(doc.page_content, doc.metadata["start_index"]) for doc in docs]


def find_page(char_offset: int, page_spans: list[dict]) -> tuple[int, float | None]:
    for span in page_spans:
        if span["start"] <= char_offset < span["end"]:
            return span["page_number"], span["confidence"]
    last = page_spans[-1]
    return last["page_number"], last["confidence"]


def answer_question(vectorstore, question: str) -> tuple[str, int, list[dict]]:
    t0 = time.time()
    k           = int(os.getenv("RETRIEVAL_K", "3"))
    fetch_k     = int(os.getenv("RETRIEVAL_FETCH_K", "10"))
    lambda_mult = float(os.getenv("RETRIEVAL_LAMBDA", "0.7"))

    logger.info(f"[RETRIEVE] Question: '{question[:120]}{'...' if len(question) > 120 else ''}'")
    logger.info(f"[RETRIEVE] MMR — k={k}, fetch_k={fetch_k}, lambda={lambda_mult}")

    retriever = vectorstore.as_retriever(
        search_type="mmr",
        search_kwargs={"k": k, "fetch_k": fetch_k, "lambda_mult": lambda_mult},
    )

    docs = retriever.invoke(question)
    logger.info(f"[RETRIEVE] Got {len(docs)} docs")

    context = "\n\n".join(doc.page_content for doc in docs)

    citations = [
        {
            "source": doc.metadata.get("source", "unknown"),
            "chunk_index": doc.metadata.get("chunk_index", i),
            "page_number": doc.metadata.get("page_number"),
            "confidence": doc.metadata.get("confidence"),
            "preview": doc.page_content[:150].strip(),
        }
        for i, doc in enumerate(docs)
    ]
    for c in citations:
        logger.debug(f"[CITE] source={c['source']}, chunk={c['chunk_index']}")

    prompt = ChatPromptTemplate.from_template(
        "Answer the question based only on the context below.\n\n"
        "Context:\n{context}\n\n"
        "Question: {question}"
    )

    logger.info(f"[LLM] Calling {os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')} via Groq...")
    chain = prompt | get_llm() | StrOutputParser()
    answer = chain.invoke({"context": context, "question": question})

    elapsed_ms = int((time.time() - t0) * 1000)
    logger.info(f"[LLM] Done — answer_length={len(answer)} chars, citations={len(citations)}, total_time={elapsed_ms}ms")
    return answer, elapsed_ms, citations
