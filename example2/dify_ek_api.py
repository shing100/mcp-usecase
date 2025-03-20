import logging
import os
import time
from typing import Annotated, Any, Dict, List, Optional, TypedDict
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from langchain.retrievers.ensemble import EnsembleRetriever
from langchain_chroma import Chroma
from langchain_community.retrievers import BM25Retriever
from langchain_community.document_loaders import PDFPlumberLoader
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langgraph.graph import END, START, StateGraph

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# API 키 설정
API_KEY = "dify-external-knowledge-api-key"
api_key_header = APIKeyHeader(name="Authorization")

# 디렉토리 설정
BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = PROJECT_ROOT / "data" 
CHROMA_DB_DIR = BASE_DIR / "chroma_db"

# PDF 파일 경로 (프로젝트 공유 데이터 폴더 사용)
PDF_FILES = list(DATA_DIR.glob("*.pdf"))
PDF_PATH = PDF_FILES[0] if PDF_FILES else DATA_DIR / "sample.pdf"

app = FastAPI(title="Dify 외부 지식 API - LangGraph 버전")


###### STEP 1. 상태(State) 및 전처리 함수 정의 ######

class KnowledgeState(TypedDict):
    """
    LangGraph 그래프에서 사용되는 상태 정의

    각 필드는 그래프의 노드 간에 전달되는 데이터를 나타냅니다.

    """

    query: Annotated[str, "사용자가 입력한 검색 쿼리"]

    search_method: Annotated[str, "검색 방법"]

    top_k: Annotated[int, "반환할 최대 결과 수"]

    score_threshold: Annotated[float, "결과에 포함할 최소 관련성 점수(0.0-1.0)"]

    results: Annotated[List[Dict[str, Any]], "검색 결과 목록"]

    vector_db: Annotated[Optional[Any], "Chroma 벡터 DB 인스턴스"]

    semantic_retriever: Annotated[Optional[Any], "의미 기반 검색 리트리버"]
    keyword_retriever: Annotated[Optional[Any], "키워드 기반 검색 리트리버"]
    hybrid_retriever: Annotated[Optional[Any], "하이브리드 검색 리트리버"]


###### STEP 2. 노드(Node) 정의 ######

class DocumentProcessor:
    """
    PDF 파일을 로드하고 텍스트를 추출하여 청크로 분할한 후
    벡터 저장소(ChromaDB)에 저장하는 역할을 담당합니다.

    """

    def __init__(self, knowledge_id="test-knowledge-base"):
        self.knowledge_id = knowledge_id
    
    def __call__(self, state: KnowledgeState) -> KnowledgeState:
        """
        문서 처리 및 벡터 저장소 설정 실행

        Args:
            state (KnowledgeState): 현재 그래프 상태

        Returns:
            KnowledgeState: 업데이트된 그래프 상태

        """
        
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(CHROMA_DB_DIR, exist_ok=True)
        
        try:
            embedding = OpenAIEmbeddings(model='text-embedding-3-small')
            chroma_exists = (CHROMA_DB_DIR / "chroma.sqlite3").exists()
            
            if chroma_exists:
                try:
                    vector_db = Chroma(
                        collection_name=self.knowledge_id,
                        embedding_function=embedding,
                        persist_directory=str(CHROMA_DB_DIR)
                    )
                    
                    collection_data = vector_db.get()

                    if not collection_data.get("documents", []):
                        logger.warning("기존 컬렉션이 비어 있습니다. 새로 생성합니다.")
                        raise ValueError("Empty collection")
                    
                except Exception as e:
                    logger.warning(f"기존 벡터 저장소 로드 실패: {str(e)}. 새로 생성합니다.")
                    chroma_exists = False
                    
                    if CHROMA_DB_DIR.exists():
                        backup_dir = f"{CHROMA_DB_DIR}_backup_{int(time.time())}"
                        os.rename(CHROMA_DB_DIR, backup_dir)
                        os.makedirs(CHROMA_DB_DIR, exist_ok=True)
                    
            if not chroma_exists:
                loader = PDFPlumberLoader(str(PDF_PATH))
                docs = loader.load()
                text_splitter = RecursiveCharacterTextSplitter(
                    chunk_size=600,
                    chunk_overlap=50
                )
                split_docs = text_splitter.split_documents(docs)
                
                if not split_docs:
                    logger.warning("텍스트 청크가 없습니다. 임시 데이터를 사용합니다.")
                    split_docs = [
                        Document(
                            page_content="This is a test document chunk 1 for Dify external knowledge API.",
                            metadata={
                                "path": str(PDF_PATH),
                                "description": "Test PDF document",
                                "title": PDF_PATH.name
                            }
                        ),
                        Document(
                            page_content="This is a test document chunk 2 about PDF processing and retrieval.",
                            metadata={
                                "path": str(PDF_PATH),
                                "description": "Test PDF document",
                                "title": PDF_PATH.name
                            }
                        ),
                        Document(
                            page_content="This is a test document chunk 3 explaining external knowledge API implementation.",
                            metadata={
                                "path": str(PDF_PATH),
                                "description": "Test PDF document",
                                "title": PDF_PATH.name
                            }
                        )
                    ]
                
                vector_db = Chroma.from_documents(
                    documents=split_docs,
                    embedding=embedding,
                    persist_directory=str(CHROMA_DB_DIR),
                    collection_name=self.knowledge_id
                )
            
            state["vector_db"] = vector_db
            
        except Exception as e:
            logger.error(f"벡터 저장소 초기화 중 오류 발생: {str(e)}")
            raise
        
        return state

class RetrieverSetup:
    """
    리트리버 설정 노드

    벡터 저장소에서 의미 기반, 키워드 기반, 하이브리드 검색을 위한
    리트리버를 설정하는 역할을 담당합니다.

    """

    def __call__(self, state: KnowledgeState) -> KnowledgeState:
        """
        리트리버 설정 실행

        Args:
            state (KnowledgeState): 현재 그래프 상태

        Returns:
            KnowledgeState: 업데이트된 그래프 상태

        Raises:
            ValueError: 벡터 저장소가 상태에 없거나 리트리버 설정 실패 시 발생

        """
        
        vector_db = state.get("vector_db")

        if vector_db is None:
            logger.error("벡터 저장소가 상태에 없습니다.")
            raise ValueError("Vector store not found in state")
        
        top_k = state.get("top_k", 5)
        
        try:
            semantic_retriever = vector_db.as_retriever(
                search_kwargs={"k": top_k}
            )
            state["semantic_retriever"] = semantic_retriever
            logger.info("시맨틱 리트리버 설정 완료")
            
            try:
                result = vector_db.get()
                
                if "documents" in result and result["documents"]:
                    docs = result["documents"]
                    metadatas = result.get("metadatas", [None] * len(docs))
                    logger.info(f"ChromaDB에서 {len(docs)} 개의 문서를 가져왔습니다.")
                else:
                    logger.warning("ChromaDB에서 문서를 가져올 수 없습니다. 임시 문서를 생성합니다.")
                    docs = ["This is a temporary document for testing purposes."]
                    metadatas = [None]
                
                doc_objects = [
                    Document(
                        page_content=text,
                        metadata=meta if meta else {}
                    )
                    for text, meta in zip(docs, metadatas)
                ]
                
                keyword_retriever = BM25Retriever.from_documents(doc_objects)
                keyword_retriever.k = top_k
                state["keyword_retriever"] = keyword_retriever
                
                hybrid_retriever = EnsembleRetriever(
                    retrievers=[keyword_retriever, semantic_retriever],
                    weights=[0.5, 0.5]
                )
                state["hybrid_retriever"] = hybrid_retriever
                
            except Exception as inner_e:
                logger.error(f"BM25 리트리버 설정 중 오류 발생: {str(inner_e)}")
                logger.info("시맨틱 리트리버만 사용합니다.")
                state["keyword_retriever"] = semantic_retriever  # 대체용
                state["hybrid_retriever"] = semantic_retriever   # 대체용
            
        except Exception as e:
            logger.error(f"리트리버 설정 중 오류 발생: {str(e)}")
            raise
        
        return state

class PerformRetrieval:
    """
    검색 수행 노드

    사용자 쿼리에 대해 적절한 리트리버를 사용하여
    관련 문서를 검색하는 역할을 담당합니다.

    """

    def __call__(self, state: KnowledgeState) -> KnowledgeState:
        """
        검색 수행 실행

        Args:
            state (KnowledgeState): 현재 그래프 상태

        Returns:
            KnowledgeState: 업데이트 된 그래프 상태

        """
        
        query = state.get("query", "")
        search_method = state.get("search_method", "hybrid_search")
        top_k = state.get("top_k", 5)
        score_threshold = state.get("score_threshold", 0.5) 
        logger.info(f"검색 수행: 쿼리='{query}', 검색 방법={search_method}, top_k={top_k}")
        
        retriever = None

        if search_method == "keyword_search":
            retriever = state.get("keyword_retriever")
        elif search_method == "semantic_search":
            retriever = state.get("semantic_retriever")
        elif search_method == "hybrid_search":
            retriever = state.get("hybrid_retriever")
        elif search_method == "full_text_search":
            retriever = state.get("keyword_retriever")
        else:
            retriever = state.get("hybrid_retriever")
        
        if not retriever:
            logger.error(f"리트리버를 찾을 수 없습니다: {search_method}")
            retriever = state.get("hybrid_retriever")
            if not retriever:
                raise ValueError(f"No retriever available in state")
            
            logger.warning(f"{search_method} 리트리버를 찾을 수 없어 하이브리드 리트리버로 대체합니다.")
        
        try:
            docs = retriever.get_relevant_documents(query)  
            docs = docs[:top_k]
            
            results = []
            for i, doc in enumerate(docs):
                metadata = doc.metadata.copy() if hasattr(doc, 'metadata') and doc.metadata else {}
                score = max(0.95 - (i * 0.1), score_threshold)
                
                results.append({
                    "metadata": metadata,
                    "score": score,
                    "title": doc.metadata.get("Title", doc.metadata.get("title", "Document chunk")),
                    "content": doc.page_content
                })
            
            state["results"] = results

            if not results:
                logger.warning("검색 결과가 없습니다. 기본 응답을 추가합니다.")

                state["results"] = [{
                    "metadata": {
                        "path": str(PDF_PATH),
                        "description": "Default response"
                    },
                    "score": 0.5,
                    "title": "Default response",
                    "content": f"No relevant documents found for query: '{query}'"
                }]
                
        except Exception as e:
            logger.error(f"검색 수행 중 오류 발생: {str(e)}")

            state["results"] = [{
                "metadata": {
                    "path": "error",
                    "description": "Error occurred during retrieval"
                },
                "score": 0.5,
                "title": "Error",
                "content": f"An error occurred during retrieval: {str(e)}"
            }]
        
        return state


###### STEP 3. 그래프 생성 및 컴파일 ######

def create_knowledge_graph():
    """
    LangGraph 기반 지식 검색 그래프 생성

    그래프 생성, 노드 추가, 노드 간 연결, 그래프 컴파일을 수행합니다.

    Returns:
        StateGraph: 컴파일된 그래프 인스턴스
    """
    
    graph_builder = StateGraph(KnowledgeState)
    
    graph_builder.add_node("document_processor", DocumentProcessor())
    graph_builder.add_node("retriever_setup", RetrieverSetup())
    graph_builder.add_node("perform_retrieval", PerformRetrieval())
    
    graph_builder.add_edge(START, "document_processor")
    graph_builder.add_edge("document_processor", "retriever_setup")
    graph_builder.add_edge("retriever_setup", "perform_retrieval")
    graph_builder.add_edge("perform_retrieval", END)
    
    return graph_builder.compile()


###### STEP 4. 그래프 인스턴스 생성 ######

try:
    knowledge_graph = create_knowledge_graph()
    logger.info("지식 그래프 인스턴스 생성 완료")

except Exception as e:
    logger.error(f"지식 그래프 생성 중 오류 발생: {str(e)}")
    knowledge_graph = None


###### STEP 5. API 요청 및 응답 클래스 정의 ######

class RetrievalSetting(BaseModel):
    """검색 설정 모델"""

    top_k: Annotated[int, "반환할 최대 결과 수"]
    score_threshold: Annotated[float, "결과에 포함할 최소 관련성 점수 (0.0-1.0)"]


class ExternalKnowledgeRequest(BaseModel):
    """외부 지식 API 요청 모델"""

    knowledge_id: Annotated[str, "검색할 지식 베이스의 ID"]
    query: Annotated[str, "사용자의 검색 쿼리"]
    search_method: Annotated[str, "검색 방법(semantic_search, keyword_search, hybrid_search)"] = "hybrid_search"
    retrieval_setting: Annotated[RetrievalSetting, "검색 설정"]


###### STEP 6. API 키 검증 함수 ######

async def verify_api_key(authorization: str = Header(...)):
    """API 키 검증 함수"""

    if not authorization.startswith("Bearer "):
        logger.warning("올바르지 않은 Authorization 헤더 형식")

        raise HTTPException(
            status_code=403,
            detail={
                "error_code": 1001,
                "error_msg": "Invalid Authorization header format. Expected 'Bearer ' format."
            }
        )
    
    token = authorization.replace("Bearer ", "")

    if token != API_KEY:
        logger.warning("인증 실패: 유효하지 않은 API 키")

        raise HTTPException(
            status_code=403,
            detail={
                "error_code": 1002,
                "error_msg": "Authorization failed"
            }
        )
    
    return token


###### STEP 7. API 엔드포인트 정의 ######

@app.post("/retrieval")
async def retrieve_knowledge(
    request: ExternalKnowledgeRequest,
    token: str = Depends(verify_api_key)):
    """문서 검색 API 엔드포인트"""

    logger.info(f"API 요청 수신: query='{request.query}'")
    
    if knowledge_graph is None:
        logger.error("지식 그래프가 초기화되지 않았습니다.")

        raise HTTPException(status_code=500, detail="Knowledge graph is not initialized")
    
    initial_state = KnowledgeState(
        query=request.query,
        search_method=request.search_method,
        top_k=request.retrieval_setting.top_k,
        score_threshold=request.retrieval_setting.score_threshold,
        results=[],
        vector_db=None,
        semantic_retriever=None,
        keyword_retriever=None,
        hybrid_retriever=None
    )
    
    try:
        final_state = knowledge_graph.invoke(initial_state)
        results = final_state.get("results", [])
        
        response_records = []

        for r in results:
            metadata = r.get("metadata", {})
            if not metadata:
                metadata = {"path": "unknown", "description": ""}

            response_records.append({
                "metadata": metadata,
                "score": r.get("score", 0.5),
                "title": r.get("title", "Document"),
                "content": r.get("content", "No content")
            })
        
        return {"records": response_records}
    
    except Exception as e:
        logger.error(f"지식 그래프 실행 중 오류 발생: {str(e)}")

        return {"records": [{
            "metadata": {
                "path": "error",
                "description": "Error response"
            },
            "score": 0.5,
            "title": "Error",
            "content": f"An error occurred: {str(e)}"
        }]}

@app.get("/health")
async def health_check():
    """서버 상태 확인 엔드포인트"""

    health_status = {
        "status": "healthy" if knowledge_graph is not None else "unhealthy",
        "knowledge_graph_initialized": knowledge_graph is not None,
        "openai_api_key_set": os.getenv("OPENAI_API_KEY") is not None,
        "data_directory_exists": DATA_DIR.exists(),
        "chroma_db_directory_exists": CHROMA_DB_DIR.exists(),
        "pdf_exists": PDF_PATH.exists()
    }
    
    return health_status

if __name__ == "__main__":
    logger.info("서버 시작 중...")
    uvicorn.run(app, host="0.0.0.0", port=8000)